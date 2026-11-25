"""Three-step guard decision: hard rules, envelope check, graded score.

1. ``hard_rules``      BLOCK when the task policy failed (forbidden verb, protected
                       object, namespace, untrusted argument, expired intent).
2. ``envelope_check``  ESCALATE on any breach of the impact envelope, or when the
                       impact is unknown.
3. ``graded_score``    the weighted risk score, which only decides inside the envelope.

``decide_with_impact`` is the single-action decision every runner uses. It keeps
the v1.0 reason codes (``impact_degradation``, ``impact_uncertain``,
``impact_unavailable``) so existing results and defences stay comparable.

``EnvelopeDecider`` is the Stage 2 decision for a signed intent. It checks the
intent's ``ImpactEnvelope`` against the action alone and against the episode's
cumulative predicted state, with explicit reason codes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Optional

from opsguard.impact.decision import GuardDecision, PolicyOutcome, RiskSettings, decide_risk
from opsguard.impact.effects import (ConfigEffect, ImpactEstimate, _active_managed_backends, _backends, _index,
                                     _replicas, _template, predict_impact)
from opsguard.impact.graph import ResourceRef

_SEVERITY = {"ALLOW": 0, "ESCALATE": 1, "BLOCK": 2}

# Reason codes of the envelope step. Each names the dimension that was breached.
ENVELOPE_REASONS = (
    "IMPACT_UNKNOWN",          # no estimate, or the estimate is uncertain
    "ENVELOPE_DEGRADATION",    # a Service is predicted to lose every backend
    "ENVELOPE_SERVICES",       # more Services affected than the intent allows
    "ENVELOPE_NAMESPACES",     # more namespaces affected than the intent allows
    "ENVELOPE_IRREVERSIBLE",   # the intent requires reversible actions
    "ENVELOPE_DELETION",       # deletion is not allowed by the intent
    "ENVELOPE_CUMULATIVE",     # only the combination with earlier approved changes breaks the envelope
)
# No typed operation deletes, grants privileges or touches Secrets, and each
# one can be undone by a further typed write. Stage 3 fills these from the
# full effect vector; the checks are wired so that work only changes the inputs.
IRREVERSIBLE_VERBS: frozenset[str] = frozenset()
DELETING_VERBS: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# The three steps
# ---------------------------------------------------------------------------

def hard_rules(*, policy_outcome: PolicyOutcome, provenance_risk: float = 0, irreversibility: float = 0,
               degraded=(), settings: RiskSettings = RiskSettings()) -> Optional[GuardDecision]:
    """Step 1. A failed policy decides on its own; None means continue."""
    if policy_outcome == "PASS":
        return None
    return decide_risk(policy_outcome=policy_outcome, provenance_risk=provenance_risk, blast_radius=0,
                       irreversibility=irreversibility, predicted_affected_services=degraded, settings=settings)


def envelope_check(impact: Optional[ImpactEstimate], *, escalate_on_degradation: bool = True) -> Optional[GuardDecision]:
    """Step 2 for a single action with today's estimate. None means inside the envelope."""
    if impact is None:
        return GuardDecision("ESCALATE", "impact_unavailable", None, ())
    if impact.degraded_services and escalate_on_degradation:
        return GuardDecision("ESCALATE", "impact_degradation", None, impact.degraded_services)
    if impact.uncertain_services:
        return GuardDecision("ESCALATE", "impact_uncertain", None, impact.degraded_services)
    return None


def graded_score(impact: ImpactEstimate, *, provenance_risk: float, irreversibility: float,
                 settings: RiskSettings = RiskSettings()) -> GuardDecision:
    """Step 3. Normalised blast radius plus provenance and irreversibility, against the thresholds."""
    at_risk = set(impact.degraded_services).union(impact.uncertain_services)
    blast_radius = len(at_risk) / impact.service_count if impact.service_count else 0
    return decide_risk(policy_outcome="PASS", provenance_risk=provenance_risk, blast_radius=blast_radius,
                       irreversibility=irreversibility, predicted_affected_services=impact.degraded_services,
                       settings=settings)


def _combine(breach: Optional[GuardDecision], graded: GuardDecision) -> GuardDecision:
    """The stricter decision wins. On a tie the graded reason is kept, as in v1.0."""
    if breach is None or _SEVERITY[graded.decision] >= _SEVERITY[breach.decision]:
        return graded
    return replace(breach, risk_score=graded.risk_score)


def decide_with_impact(
    *,
    policy_outcome: PolicyOutcome,
    provenance_risk: float,
    irreversibility: float,
    impact: Optional[ImpactEstimate] = None,
    settings: RiskSettings = RiskSettings(),
    escalate_on_degradation: bool = True,
) -> GuardDecision:
    """Hard rules, then the envelope, then the graded score.

    A predicted Service outage is never allowed automatically, whatever the
    weighted score: an authorized action that takes a Service down still needs
    a person to approve it. ``escalate_on_degradation=False`` reproduces the
    v1.0-reference behaviour, where only the weighted score and uncertainty
    could withhold a write, for comparison runs.
    """
    degraded = impact.degraded_services if impact is not None else ()
    stopped = hard_rules(policy_outcome=policy_outcome, provenance_risk=provenance_risk,
                         irreversibility=irreversibility, degraded=degraded, settings=settings)
    if stopped is not None:
        return stopped
    breach = envelope_check(impact, escalate_on_degradation=escalate_on_degradation)
    if impact is None:
        return breach
    return _combine(breach, graded_score(impact, provenance_risk=provenance_risk,
                                         irreversibility=irreversibility, settings=settings))


# ---------------------------------------------------------------------------
# Episode state: the starting snapshot plus every approved change
# ---------------------------------------------------------------------------

def _ref_of(obj: dict, kind: str) -> ResourceRef:
    meta = obj["metadata"]
    return ResourceRef(kind, meta["namespace"], meta["name"])


@dataclass
class _State:
    deployments: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
    configmaps: dict = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot) -> _State:
        state = cls()
        for pool, kind, items in (("deployments", "workload", snapshot.deployments),
                                  ("services", "service", snapshot.services),
                                  ("configmaps", "configmap", snapshot.configmaps)):
            for obj in items or ():
                getattr(state, pool)[_ref_of(obj, kind)] = deepcopy(obj)
        return state

    def copy(self) -> _State:
        return _State(dict(self.deployments), dict(self.services), dict(self.configmaps))

    def apply(self, verb: str, obj: dict) -> _State:
        """This state with one previewed object in place of the stored one."""
        state = self.copy()
        if verb == "set_selector":
            state.services[_ref_of(obj, "service")] = deepcopy(obj)
        elif verb == "set_config":
            state.configmaps[_ref_of(obj, "configmap")] = deepcopy(obj)
        else:
            ref = _ref_of(obj, "workload")
            if _template(obj) or ref not in state.deployments:
                state.deployments[ref] = deepcopy(obj)
            else:
                # A Scale response carries the replica count but no Pod template.
                merged = deepcopy(state.deployments[ref])
                merged.setdefault("spec", {})["replicas"] = _replicas(obj)
                state.deployments[ref] = merged
        return state

    def serving(self) -> set[ResourceRef]:
        """Services with at least one selected workload that has replicas (Pod-template approximation)."""
        backends = _backends(_index(self.deployments.values(), "workload"), _index(self.services.values(), "service"))
        replicas = {ref: _replicas(obj) for ref, obj in self.deployments.items()}
        active = _active_managed_backends(self.services, backends, replicas)
        return {service for service, workloads in active.items() if workloads}


class EpisodeState:
    """Per-task record the gateway keeps: start snapshot, approved changes, predicted state."""

    def __init__(self, task_id: str = ""):
        self.task_id = task_id
        self.start: Optional[_State] = None
        self.approved: list[dict] = []
        self._hints: dict[tuple, tuple[ResourceRef, ...]] = {}

    def begin(self, snapshot) -> None:
        """Take the starting snapshot once, on the task's first proposal."""
        if self.start is None:
            self.start = _State.from_snapshot(snapshot)

    def predicted(self) -> _State:
        """The start snapshot with every approved change applied, in order."""
        if self.start is None:
            raise ValueError("the episode has not started")
        state = self.start
        for change in self.approved:
            state = state.apply(change["verb"], change["object"])
        return state

    def affected(self) -> set[ResourceRef]:
        return {service for change in self.approved for service in change["reachable"]}

    def hint(self, request, reachable) -> None:
        """The decider leaves the services an allowed action reaches, for ``record``."""
        self._hints[(request.verb, request.name)] = tuple(reachable)

    def record(self, request, obj: dict) -> None:
        """Called by the gateway after a write is applied."""
        self.approved.append({"verb": request.verb, "name": request.name, "object": deepcopy(obj),
                              "reachable": self._hints.pop((request.verb, request.name), ())})


# ---------------------------------------------------------------------------
# The Stage 2 decision for a signed intent
# ---------------------------------------------------------------------------

class EnvelopeDecider:
    """Impact decider for the gateway: three steps against the intent's envelope and the episode.

    The gateway already ran the hard rules (task policy, provenance, intent
    signature) before calling this, so step 1 here is the policy outcome PASS.
    """

    uses_episode = True

    def __init__(self, envelope, *, config_effects: tuple[ConfigEffect, ...] = (), trusted_edges=(),
                 settings: RiskSettings = RiskSettings(), provenance_risk: float = 0.0):
        self.envelope, self.config_effects, self.trusted_edges = envelope, tuple(config_effects), tuple(trusted_edges)
        self.settings, self.provenance_risk = settings, provenance_risk
        self.episode: Optional[EpisodeState] = None
        self.audit: list[dict] = []

    async def __call__(self, request, before, current, preview):
        return self.decide(request, current, preview)

    def _result(self, request, status, reason, step, services=(), **detail):
        entry = {"verb": request.verb, "name": request.name, "status": status, "reason": reason, "step": step,
                 "predicted_services": [s.name for s in services], **detail}
        self.audit.append(entry)
        return {"status": status, "reason": reason, "predicted_services": tuple(s.name for s in services)}

    def decide(self, request, current, preview):
        if self.episode is None:
            raise ValueError("the gateway did not attach an episode")
        self.episode.begin(current)
        irreversible = 1.0 if request.verb in IRREVERSIBLE_VERBS else 0.0
        try:
            # "Alone" means this action applied to the episode's starting snapshot; "cumulative"
            # means applied after every approved change. Live state already contains the
            # approved writes, so it is not used for the alone estimate.
            start, cumulative = self.episode.start, self.episode.predicted()
            kind = {"service": "service", "configmap": "configmap", "deployment": "workload"}[request.resource]
            pool = {"service": start.services, "configmap": start.configmaps, "deployment": start.deployments}
            obj = pool[request.resource][ResourceRef(kind, current.namespace, request.name)]
            impact = predict_impact(verb=request.verb, deployments=start.deployments.values(),
                                    services=start.services.values(), configmaps=start.configmaps.values(),
                                    before=obj, preview=preview,
                                    config_effects=self.config_effects, trusted_edges=self.trusted_edges)
            baseline = start.serving()
            lost_alone = baseline - start.apply(request.verb, preview).serving()
            lost_cumulative = baseline - cumulative.apply(request.verb, preview).serving()
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            return self._result(request, "ESCALATE", "IMPACT_UNKNOWN", "envelope", error=type(error).__name__)

        # Step 2: the envelope, for this action alone first, then with the episode.
        envelope = self.envelope
        reachable = set(impact.reachable_services)
        # Scale and selector changes are settled by the exact serving check above;
        # rollouts and configuration changes keep the estimate's uncertainty.
        unknown = set(impact.uncertain_services) if request.verb in ("restart", "set_image", "set_config") else set()
        degraded = set(impact.degraded_services) | lost_alone
        namespaces = {ref.namespace for ref in reachable}
        breach = None
        if degraded:
            breach = ("ENVELOPE_DEGRADATION", degraded)
        elif unknown:
            breach = ("IMPACT_UNKNOWN", unknown)
        elif len(reachable) > envelope.max_services_affected:
            breach = ("ENVELOPE_SERVICES", reachable)
        elif len(namespaces) > envelope.max_namespaces:
            breach = ("ENVELOPE_NAMESPACES", reachable)
        elif envelope.require_reversible and request.verb in IRREVERSIBLE_VERBS:
            breach = ("ENVELOPE_IRREVERSIBLE", reachable)
        elif not envelope.allow_deletion and request.verb in DELETING_VERBS:
            breach = ("ENVELOPE_DELETION", reachable)
        if breach:
            return self._result(request, "ESCALATE", breach[0], "envelope", tuple(sorted(breach[1], key=str)))
        combined = self.episode.affected() | reachable
        if lost_cumulative or len(combined) > envelope.max_services_affected:
            # Each action alone fits; only the sequence breaks the envelope.
            services = lost_cumulative or combined
            return self._result(request, "BLOCK", "ENVELOPE_CUMULATIVE", "envelope",
                                tuple(sorted(services, key=str)), approved_steps=len(self.episode.approved))

        # Step 3: graded score, inside the envelope only.
        inside = ImpactEstimate(impact.reachable_services, (), (), impact.service_count)
        graded = graded_score(inside, provenance_risk=self.provenance_risk, irreversibility=irreversible,
                              settings=self.settings)
        if graded.decision == "ALLOW":
            self.episode.hint(request, impact.reachable_services)
        return self._result(request, graded.decision, graded.reason_code, "graded", impact.reachable_services,
                            risk_score=graded.risk_score)
