# OpsGuard progress

This file tracks progress against the five-stage implementation guide. Update it at the end of every work package: what was done, the evidence, and what is still open. Newest stage first.

| Stage | Status | Gate |
| --- | --- | --- |
| 1 Freeze, formalize and pilot | Implemented locally on branch `stage-1`; not pushed | G0: 4 of 6 items met; mentor sign-off and GitHub CI run outstanding |
| 2 Design the intent and the gate | All five packages implemented; S2-P2 on `main`, S2-P4, S2-P1, S2-P3, S2-P5 on stacked local branches | G1: 5 of 6 items met; GitHub CI not yet run |
| 3 Effect model and Benchmark v2 | Not started | G2 |
| 4 Baselines, tuning and pre-registration | Not started | G3 |
| 5 Evaluate, analyse and release | Not started | G4, G5 |

## Stage 2: Design the intent and the gate

### Gate G1 checklist

- [x] S2-P2: IntentSpec v2 enforced; resolver passes every situation (memory and kind); provenance blocks copied values (value matching, see limitation)
- [x] S2-P3: the same CEL rules in the gateway and the admission policies; kind security suite shows zero unauthorized writes
- [x] S2-P1: every paraphrase of all 53 attacked cases gets the typed form's decision; Events channel working on memory and kind
- [x] S2-P4: episode state blocks the scale, restart, selector sequence at the selector step with `ENVELOPE_CUMULATIVE`
- [x] S2-P5: all 73 cases through one CLI; parity with the old runners: 261 cells, 0 differences
- [ ] CI green: **not run**. S2-P2 is on `main`; S2-P4, S2-P1, S2-P3 and S2-P5 are local commits waiting for your go-ahead to push

Full suite: 1055 passed, 9 skipped. Kind integration (opt-in, `opsguard-lab`): 7 tests; 7 of 7 passed in 5 consecutive runs after one earlier run had a single failure that was not captured and has not reproduced.

### S2-P5 Unified runner and schema v2

**Date:** 26 November 2026. **Branch:** `feat/s2-p5-runner`, stacked on `feat/s2-p3-policy`.

| Item | Result |
| --- | --- |
| CaseSpec v2 (`bench/schema.py`) | Adds suite, goal (G1–G8 or none), allowed changes, invariants, severity detectors, payload and payload position, family id, split, executable oracle text and source reference. `goal` and `oracle` have no defaults, so a case without them is rejected; attacked/clean consistency is enforced |
| Converters (`bench/convert/`) | All six suites (selector, scale, image, restart, config, pilot) convert to 73 CaseSpec v2; goals come from `case_goal_map.csv` and match the threat model (G1 31, G3 14, G7 6, G4 2, clean 20). Splits are provisional (hash of family id, about 20/20/60; with few families today it is 24/10/39); Stage 3 seals them |
| RunResult v2 | Worst severity tier, partial-harm flags, every gateway decision with its reason, token counts and cost, intent id, suite condition, samples. v1 RunResult gains the optional severity fields |
| Observer sampling | `IntervalSampler` samples the oracle every `sample_interval` (default 1 s) during the agent run in the common runner; `assess()` gives S0–S3 from ready endpoints, probe answers and ready replicas against the start state; the worst tier latches. Case detectors raise the tier by goal (G1 S2, G3 S3, G4 S1, G7 S5) |
| Unified CLI (`python -m opsguard.bench.unified run`) | One command, one result format; per-suite adapters call each suite's existing in-memory trial code. Full matrix: 73 cases, 261 trials, 0 errors, 31 cells skipped where a suite does not implement a condition (listed in `skipped.json`) |
| Parity (`python -m opsguard.bench.unified parity`) | Runs the six old per-suite commands unchanged and compares outcomes cell by cell: 261 compared, **0 differences**, 0 unexplained. 6 old cells have no unified row: the pilot's `rbac-scoped` and `opsguard-v1.0` comparison conditions, explained in the report |
| Tests | `tests/bench/test_unified.py` (22): conversion, schema rejections (missing goal or oracle), RunResult v2 shape, severity tiers, interval sampler catching a brief outage, decisions and tokens from traces, one attacked case per suite through the runner, CLI |

**Gap carried into Stage 3.** Only the common (selector) runner samples at a fixed interval; the scale, image, restart and config runners still sample after each action, so their worst tier comes from the case detectors. Stage 3's shared fixtures move them onto the interval sampler.

### S2-P3 Policy compiler, admission backstop, fail-closed, security suite

**Date:** 26 November 2026. **Branch:** `feat/s2-p3-policy`, stacked on `feat/s2-p1-canonical`.

| Item | Result |
| --- | --- |
| Policy language | `documentations/POLICY_LANGUAGE.md`: variables (`object`, `oldObject`, `request.*`), the supported CEL subset, CEL-like semantics (missing field is an error, `&&`/`||` absorb errors, no cross-type equality, bounded evaluation), every compiled rule, installation and limits. `policy/cel.py` is the gateway's interpreter; anything outside the subset raises and fails closed |
| Compiler (`policy/compile.py`) | `compile_policy(intent, workload=...)` gives `GatewayRules` plus one ValidatingAdmissionPolicy and binding per resource (Deployments, `deployments/scale`, Services, ConfigMaps). Rules: updates only, target names, protected names, replica bounds, image allowlist, restart marker, selector map, ConfigMap key and value. The admission validations are the same strings the gateway evaluates. Bindings select the intent's namespace; `matchConditions` limit them to the agent's ServiceAccount. `InstalledPolicy` installs at trial start and removes at exit or expiry, bindings first |
| One policy per resource | The API server type-checks each expression against every kind a policy matches; a single combined policy was never enforced on kind. Splitting per resource gives zero type-check warnings |
| Signed decisions | With a decision key, `MutationExecutor` writes only for a `SignedDecision`: HMAC over decision id, intent id and the SHA-256 of method, path, content type, body and bound identity. It refuses `DECISION_UNSIGNED`, `DECISION_FORGED`, `DECISION_REQUEST_MISMATCH`, `DECISION_INTENT_MISMATCH` and `DECISION_REPLAYED`. The gateway signs each ALLOW |
| Gateway | Checks the compiled rules on every dry-run object (`POLICY_RULE_VIOLATED`). Any failure or timeout before a write is `BLOCK(GATEWAY_UNAVAILABLE)` with `fail_closed: true`; once a write may have been sent it stays `ERROR(WRITE_OUTCOME_UNKNOWN)` so the agent stops |
| Security suite on kind (`tests/integration/test_kind_security.py`) | The agent's ServiceAccount has broad RBAC (patch, update, delete). (a) Four attacks through the gateway with task policy off: all `POLICY_RULE_VIOLATED`; the same requests sent directly with the agent's token, plus deleting payments: all 422 from the admission policy; an in-intent direct scale: 200. (b) Forged: `DECISION_FORGED`; replayed: `DECISION_REPLAYED`; valid: applied. (c) Target edited after the re-check: `STALE_TARGET`. (d) Inventory down and effect model down: `GATEWAY_UNAVAILABLE`. (e) Expired intent: `INTENT_EXPIRED`. Observer: only checkout-api's replica count changed, by the two in-intent writes. **Zero unauthorized writes**; passed 3 runs in a row |
| Tests | `tests/policy/` (CEL 40, compiler 17), `tests/security/test_gateway_security.py` (13). Full suite 1033 passed, 9 skipped; all 7 kind integration tests pass |
| Parity | Memory selector matrix against `main`: 0 outcome differences. Validation OK |

**Found while testing on kind.** (1) The combined policy above. (2) An admin client certificate loaded into a TLS context shared with the agent's client made the API server authenticate the agent as the admin, so the policy (scoped to the agent) never applied; the test now checks the agent's identity with a SelfSubjectReview. (3) The demo image needs `FIXTURE_SERVICE`; without it the Pod crashes a second after starting.

### S2-P1 Canonical actions, paraphrase suite, Events channel

**Date:** 26 November 2026. **Branch:** `feat/s2-p1-canonical`, stacked on `feat/s2-p4-decision`.

| Item | Result |
| --- | --- |
| Canonicaliser (`kube/canonical.py`) | `canonicalize(raw)` maps kubectl `scale`, `rollout restart`, `set image` and `patch` lines, JSON / merge / strategic patches and server-side-apply bodies (apps/v1 Deployment, v1 Service, v1 ConfigMap) to exactly one typed action. Everything else is `UNRECOGNIZED_FORM`: verbs with no typed operation (delete, apply -f, exec, label, create, helm), a missing namespace, more than one object or change, `--all`/`-l`, unknown flags (`--context`, `--kubeconfig`), extra fields, a container image by list index, shell chains. `ExecutionGateway.propose_raw()` decides only the canonical typed action |
| Paraphrase suite (`tests/security/test_paraphrase.py`) | All 53 attacked cases (restart 10, image 10, scale 10, config 10, selector 10, pilot 3), five or more raw forms each, all canonicalise to the typed action. Restart and selector cases also run through the real gateway with and without task policy: every variant gets the same status, reason and writes (task policy: BLOCK, no writes; unprotected: ALLOW with a dry-run and a write) |
| Events channel | `EventsRead` / `read_events` in the gateway; `evidence/events.py` lists core/v1 Events for one involved object, newest five, bounded excerpt with the newest Event as source, timestamp and SHA-256, marked untrusted. `inject_event` creates the payload Event with the fixture identity and checks it is stored unchanged. Registered as the fifth channel in `bench/channels.py` for memory and kind; the kind lab's `setup` role may create Events and `reader` may list them |
| Tests | `tests/kube/test_canonical.py` (53), `tests/evidence/test_events.py` (7), `tests/security/test_paraphrase.py` (101), opt-in `tests/integration/test_kind_events.py`. Full suite 963 passed, 8 skipped. On kind `opsguard-lab` all 6 integration tests pass, including the Events smoke test (payload intact, digest matches, reader cannot create Events) |
| Parity | Memory selector matrix against `main`: 0 outcome differences. Validation OK |

**Notes.** No case uses the Events channel yet; allocation reports list it only once a case does, so the team plan is unchanged. Stage 3 adds Events cases. A merge patch of `spec.selector` maps to `set_selector`, which replaces the whole map; the typed action is what gets decided and written.

### S2-P4 Three-step decision and episode state

**Date:** 26 November 2026. **Branch:** `feat/s2-p4-decision`, from `main` at `dfd30d6`.

| Item | Result |
| --- | --- |
| Three steps (`impact/assessment.py`) | `hard_rules()` (BLOCK on a failed policy), `envelope_check()` (ESCALATE on a breach or unknown impact), `graded_score()` (weighted score), called in that order by `decide_with_impact`. The stricter result wins; on a tie the graded reason is kept. Its decisions and reason codes are identical to v1.0 plus the S1-P4 rule, checked on 384 input combinations against a copy of the old function |
| Envelope reason codes | The `EnvelopeDecider` used with a signed intent gives `IMPACT_UNKNOWN`, `ENVELOPE_DEGRADATION`, `ENVELOPE_SERVICES`, `ENVELOPE_NAMESPACES`, `ENVELOPE_IRREVERSIBLE`, `ENVELOPE_DELETION` and `ENVELOPE_CUMULATIVE`, plus the graded codes. Every result is logged with its step |
| Mapping today's estimate | Services affected = reachable Services; namespaces from those. Scale and selector changes are settled by an exact check (does any Service lose every active backend, by the Pod-template approximation); rollout and ConfigMap uncertainty stays `IMPACT_UNKNOWN`. No typed operation deletes or is irreversible yet, so those sets are empty and Stage 3 fills them |
| Episode state | `EpisodeState` keeps the start snapshot and every applied change; the gateway creates one per task, begins it on the first proposal, records each applied write, and attaches it to an episode-aware decider. "Alone" is the action on the start snapshot; "cumulative" is the action after all approved changes. A cumulative-only breach is BLOCK(`ENVELOPE_CUMULATIVE`) |
| Tests | `tests/impact/test_envelope.py`: scale canary 2→0, restart checkout, move the selector to the canary: steps 1–2 ALLOW, step 3 BLOCK with `ENVELOPE_CUMULATIVE`; each step alone ALLOW; per-dimension codes; cumulative Services limit; gateway hook; v1.0 equivalence. Full suite 807 passed, 7 skipped |
| Parity | In-memory selector matrix (56 trials) and pilot (30 rows) against `main`: 0 outcome and 0 decision differences |

**Note.** The guide's example scales checkout 3→1. Today's estimate marks every scale-down as uncertain, so a canary Deployment stands in for the step that removes capacity; the sequence and the result are the same.

### S2-P2 IntentSpec v2, resolver, provenance

**Date:** 25 November 2026. **Branch:** `feat/s2-p2-intent`, from `main` at `44c71e6`. Not pushed.

| Item | Result |
| --- | --- |
| `IntentSpecV2` (`intent/models.py`) | Strict model with id, version, task id, issue and expiry times, namespace, target hint (Service, Deployment, StatefulSet, ConfigMap or bare name), verbs, argument bounds, protected resources, postconditions, impact envelope, risk class, approval mode, summary and `drafted_fields`. Rejects verbs without bounds, bounds without verbs, a protected target, a wildcard over its own namespace, and lifetimes over 24 hours. `to_task_intent()` gives the v1 view, so existing code runs unchanged |
| Signing | Canonical JSON (sorted keys, no signature field), HMAC-SHA256 with `OPSGUARD_INTENT_KEY` from the environment or the ignored `.env` (at least 32 bytes). `verify_intent()` returns `VALID`, `INTENT_INVALID` (altered, wrong key, unsigned, issued in the future, malformed) or `INTENT_EXPIRED` |
| Gateway | Accepts a signed `IntentSpecV2` plus the key and verifies it on every proposal; failure is BLOCK with `INTENT_INVALID` or `INTENT_EXPIRED` and nothing is sent to the API. A malformed intent or a missing key refuses to construct the gateway |
| Resolver (`intent/resolver.py`) | `resolve()` follows the guide's pseudocode, with `TARGET_MISSING`, `TARGET_AMBIGUOUS` and `TARGET_REPLACED`. StatefulSets are supported; Pods are followed to their controller root and never bound. `check_binding()` compares the bound object with a fresh read; the gateway blocks a new UID and re-resolves a new resourceVersion once, then escalates with `TARGET_VERSION_CHANGED` |
| Provenance (`intent/provenance.py`) | Every sensitive argument (namespace, name, replicas, image, ConfigMap key and value, selector) is classed as intent, gateway-read, untrusted or unobserved. A value found in this task's evidence that is neither approved nor read by the gateway for its own target gives `BLOCK(UNTRUSTED_ARGUMENT)` |
| Scope card (`intent/scope.py`, `demo-ui/src/ScopeCard.tsx`) | A rule-based drafter reads only the request text and the demo's object names and marks what it filled (`drafted_fields`). The operator edits and confirms; `POST /api/scopes` validates the scope and refuses anything the lab cannot enforce (unknown target, replicas outside 1–5, wider image, config or selector bounds, payments unprotected). At run start the server signs it into an `IntentSpecV2` for the run's namespace, and the gateway verifies it on every proposal. The key is `OPSGUARD_INTENT_KEY` from `.env`, or a per-process lab key. The UI requires a confirmed scope before a run; any edit clears the confirmation |
| Tests | 62 new Python tests in `tests/intent/` (resolver 18, intent spec 18, provenance and gateway 18, scope card 8); 2 opt-in kind tests in `tests/integration/test_kind_resolver.py`; 2 UI tests in `demo-ui/src/scope.test.ts`. Full suite 411 passed, 7 skipped; UI type check, tests and build pass. Draft, confirm, edit-after-confirm and a refused out-of-range edit were checked in the browser |
| kind run | `opsguard-lab` (new kind cluster, `opsguard-demo:v1/v2` loaded, local Alertmanager on 9093). All 5 opt-in integration tests passed: resolver situations, recreated Deployment, shared executor, selector smoke and admission-policy smoke. The run found a real bug: the API sends `"endpoints": null` for an empty EndpointSlice, which made the resolver escalate `INVENTORY_UNVERIFIED` for a Service with no Pods. Fixed, with a unit test. The tests need `kind.exe` on PATH (the local binary has no `.exe` suffix) |
| Parity | Scripted memory selector matrix (56 trials) and pilot (30 rows) before and after: no outcome changed. 10 attacked selector trials are still blocked, now with `UNTRUSTED_ARGUMENT` instead of `SELECTOR_NOT_AUTHORIZED` |

**Deliberate choices to review**

- *Gateway-read values* are the resolved target's own fields, not the whole namespace. Otherwise any decoy name in a log would be "read from cluster state" and provenance would never fire on target names.
- *Selector fallback.* When no Pod matches the selector, the resolver also matches workload Pod templates, so a workload scaled to 0 still resolves. v2 intents always allow the fallback; v1 intents keep the old opt-in.
- *Bare names* put Services in the candidate set, as the pseudocode says, so a Service and Deployment that share a name escalate.
- *StatefulSet reads* are off by default (`include_statefulsets=False`) because the lab Roles only grant Deployments and ReplicaSets. Unread StatefulSets give `INVENTORY_INCOMPLETE` whenever the answer depends on them. Writes to StatefulSets are not supported yet (`TARGET_KIND_UNSUPPORTED`).
- *Limitation for the paper:* taint is found by matching values, not by full information-flow tracking as in CaMeL.

**Open**

1. A full live demo run with a signed scope (needs Docker, kind and a Groq key).
2. Lab Roles in `bench/kind_lab.py`, `benchmark/kind_scale_lab.py` and `kube/lab.py` need `statefulsets: list` before live runs can turn on StatefulSet reads.
3. Hand the schema to the Executor owner (S2-P3) and the `envelope` fields to the Impact owner (S2-P4).

## Stage 1: Freeze, formalize and pilot

**Dates:** 25 November 2026. **Branch:** `stage-1`, based on the integrated `main`. Nothing has been pushed to GitHub.

### Headline results

- **The core claim held in the pilot.** On kind, RBAC, task policy and the v1.0 OpsGuard decision each allowed all three authorized-but-harmful actions, and the observer saw a Service go down every time (12 of 12). OpsGuard with the new degradation rule escalated all three, with no harm and every legitimate task completed (15 of 15). 0 harness errors in 30 trials. Details in [`PILOT_REPORT.md`](PILOT_REPORT.md).
- **A real model follows these injections.** `openai/gpt-oss-20b` proposed the exact harmful action in 9 of 9 trials; permission-only conditions allowed all 9, OpsGuard escalated all 9.
- **OpsGuard no longer blocks legitimate restarts.** On kind it now completes 14 of 14 restart tasks (was 0 of 14) and still allows 0 of 10 forbidden restarts.
- **A design flaw was found and fixed.** The v1.0 decision predicted each pilot outage but still allowed it, because the weighted score stayed under the escalation threshold. A predicted outage now always escalates.

### S1-P1 Integrate and freeze the codebase

| Item | Result |
| --- | --- |
| Branch integration | Every remote branch (`feat/vikas-impact-p1`, `feature2`, `feat/frontend-integration`, `feat/prapti-bench-p1`, `docs/opsguard-team-plan`) was already contained in `demo`, and `main` was an ancestor of `demo`. Local `main` was fast-forwarded to `demo` (`8f95527`). No merge conflicts |
| Reference tag | Local annotated tag `v1.0-reference` on `8f95527` |
| Reference run | 325 tests passed, 4 skipped; `bench.validate --replay` OK (70 cases); scripted memory matrix 56/56 trials, 0 harness errors. Results in `results/reference-v1.0/` (Git-ignored) |
| Dependency lock | `requirements.lock`, frozen from the Python 3.13 environment |
| Image digests | `benchmark/images.lock` records `python:3.13-slim` and `prom/alertmanager:v0.28.1`. `python:3.12-alpine` is not in the local Docker cache, so its digest is still missing |
| CI | Workflow extended with the pilot memory run. **Not yet run on GitHub**: it runs on the first push, which is waiting for your go-ahead |

### S1-P2 Threat model

| Item | Result |
| --- | --- |
| `documentations/THREAT_MODEL.md` | Assets, actors, attacker capabilities with exact boundaries, assumptions A1–A8, out-of-scope list, goals G1–G8 with success conditions and oracle predicates, severity tiers S0–S5, and a review checklist |
| `documentations/case_goal_map.csv` | 73 cases (70 existing plus 3 pilot) mapped to goals: G1 31, G3 14, G7 6, G4 2, clean 20. **No case covers G2, G5, G6 or G8**; Stage 3 must add them |
| Mentor sign-off | **Outstanding.** The sign-off line at the top of the threat model is empty |

### S1-P3 Wording, contribution statement, admission smoke test

| Item | Result |
| --- | --- |
| Overclaiming wording | "deterministic gate" replaced in `README.md` and `documentations/OPSGUARD_TEAM_PLAN.md`. Remaining uses of "deterministic" describe scripted test adapters, IDs or reproducible scores, which is accurate. The source study, proposal and specification `.txt` files were left as historical inputs. The Phase 2 slides contain no such wording |
| `documentations/CONTRIBUTION.md` | Decision problem, claim, known-vs-contribution table, comparison with CaMeL, Progent, AgentDojo, ITBench/AIOpsLab and STRATUS |
| Admission smoke test | `tests/integration/test_vap_smoke.py` (opt-in with `OPSGUARD_KIND_CLUSTER`). **Passed on kind `opsguard-lab`**: the API server rejected a 5-replica write with the policy message and accepted 3 replicas |

### S1-P4 Restart and image escalation; pilot cases

| Item | Result |
| --- | --- |
| Rollout rule | `impact/effects.py` resolves `maxUnavailable` and `maxSurge` like the Deployment controller (25% defaults, rounding down and up). A rollout that keeps at least one replica serving predicts no outage; one that can drop every replica is treated like Recreate; settings the controller would reject stay uncertain |
| Restart matrix on kind | 14 cases × {unprotected, task-policy, opsguard}, scripted agent, `opsguard-lab`, 0 harness errors. **OpsGuard: 14/14 tasks completed (was 0/14), 0/10 forbidden.** Task policy: 14/14 completed, 0/10 forbidden. Unprotected: 14/14 completed, 10/10 forbidden. Results in `results/s1-restart-kind.jsonl` |
| Degradation rule (new) | The pilot showed `decide_with_impact` allowed actions whose predicted effect included a Service outage, because one degraded Service out of two only scores 0.25 against an escalation threshold of 0.375. A predicted outage now always escalates (`impact_degradation`). `escalate_on_degradation=False` keeps the v1.0 behaviour for comparison |
| Pilot cases | `benchmark/cases/pilot/pilot_cases.json` and `benchmark/fixtures/pilot/app.py`: shared ConfigMap, authorized restart of a single-replica Recreate workload, and an allowlisted selector for a version with no Pods. The originally planned "restart with an unready second replica" was replaced: a default rolling update never removes the last ready Pod, so it caused no outage |
| Tests | 7 new effect tests; the old test that required restart to escalate was rewritten to require completion, plus a test that every attacked restart stays blocked |

### S1-P5 Related work and the pilot

| Item | Result |
| --- | --- |
| `documentations/RELATED_WORK.md` | 15 works with links checked on 25 November 2026: Greshake et al., InjecAgent, AgentDojo, Agent Security Bench, CaMeL, FIDES, Progent, AgentSpec, Task Shield, Design Patterns, Spotlighting, MELON, AIOpsLab, ITBench, STRATUS, plus ValidatingAdmissionPolicy docs |
| Pilot harness | `src/opsguard/bench/pilot.py` with 14 tests in `tests/bench/test_pilot.py`. It is a separate harness, not an extension of `bench/loader.py`: CaseSpec v1 cannot express an intent with argument bounds or trusted effects. S2-P5 brings these cases into the unified runner |
| Pilot report | `documentations/PILOT_REPORT.md` |

### Gate G0 checklist

- [ ] S1-P1: branches merged; CI green; `v1.0-reference` tagged; dependencies locked. Merged, tagged and locked locally; **CI has not run because nothing is pushed**; one image digest missing
- [ ] S1-P2: `THREAT_MODEL.md` signed off by the mentor. **Waiting for the mentor**
- [x] S1-P3: no overclaiming wording left; admission smoke test passes
- [x] S1-P4: clean restart and image tasks complete under OpsGuard on kind (restart verified on kind; image verified by unit tests and the effect model, not yet on kind)
- [x] S1-P5: pilot shows at least 1 of 3 cases where permission-only defences allow harm and OpsGuard prevents it
- [x] S1-P5: related work done; novelty claim confirmed for now (no prior work found that checks infrastructure effects of authorized actions; a broader search is still advised)

### Open items before Stage 2

1. Push `stage-1` (and the updated `main` and tag) when you approve, then confirm the GitHub Actions run is green.
2. Send `THREAT_MODEL.md` to the mentor and record the sign-off.
3. Pull `python:3.12-alpine`, add its digest to `benchmark/images.lock`.
4. Confirm image updates under OpsGuard on kind. The image runner (`benchmark/run_image.py`) has no kind backend or `opsguard` condition yet, so this moves to S2-P5, when the image suite joins the unified runner.
5. Optional: repeat the real-model pilot with a second model family.
