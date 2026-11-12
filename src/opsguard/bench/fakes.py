"""Fakes for developing state predicates and runner logic without a cluster.

`FakeOracle` replays scripted observations, so an owner can unit-test a
predicate, a transient forbidden state, or a slow rollout before their fixture
exists on kind. `StaticGateway` answers proposals with fixed decisions.
"""

from __future__ import annotations

from collections.abc import Iterable

from opsguard.agent.interfaces import GatewayResult
from opsguard.bench.predicates import DeploymentView, Observation, ServiceView


def service(selector: dict | None = None, backends: Iterable[str] = (), responders: Iterable[str] | None = None) -> ServiceView:
    """A Service view. Responders default to one HTTP identity per ready backend."""
    backends = frozenset(backends)
    return ServiceView(selector=dict(selector or {}), ready_backends=backends,
                       responders=tuple(sorted(backends)) if responders is None else tuple(responders))


def deployment(replicas: int = 1, ready: int | None = None, **fields) -> DeploymentView:
    return DeploymentView(replicas=replicas, ready_replicas=replicas if ready is None else ready, **fields)


def observation(services: dict | None = None, deployments: dict | None = None, *, settled: bool = True,
                configmaps: dict | None = None) -> Observation:
    return Observation(services=dict(services or {}), deployments=dict(deployments or {}),
                       configmaps=dict(configmaps or {}), settled=settled)


class FakeOracle:
    """Return each scripted observation once, then repeat the last one."""

    def __init__(self, observations: Iterable[Observation]):
        self._observations = list(observations)
        if not self._observations:
            raise ValueError("a fake oracle needs at least one observation")
        self.reads = 0

    async def observe(self, *, timeout_seconds: float) -> Observation:
        index = min(self.reads, len(self._observations) - 1)
        self.reads += 1
        return self._observations[index]


class StaticGateway:
    """A gateway fake with fixed read and proposal results, recording every request."""

    def __init__(self, *, decision: str = "ALLOW", reason: str = "FAKE_DECISION"):
        self.decision, self.reason = decision, reason
        self.reads, self.proposals = [], []

    async def read(self, request, *, task_id, timeout_seconds):
        self.reads.append(request)
        return GatewayResult(status="OK", reason="FAKE_READ", data={"name": request.name})

    async def propose(self, request, *, task_id, timeout_seconds, observation_ids=()):
        self.proposals.append(request)
        return GatewayResult(status=self.decision, reason=self.reason, decision_id="fake")
