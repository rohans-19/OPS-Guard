# OpsGuard progress

This file tracks progress against the five-stage implementation guide. Update it at the end of every work package: what was done, the evidence, and what is still open. Newest stage first.

| Stage | Status | Gate |
| --- | --- | --- |
| 1 Freeze, formalize and pilot | Implemented locally on branch `stage-1`; not pushed | G0: 4 of 6 items met; mentor sign-off and GitHub CI run outstanding |
| 2 Design the intent and the gate | Not started | G1 |
| 3 Effect model and Benchmark v2 | Not started | G2 |
| 4 Baselines, tuning and pre-registration | Not started | G3 |
| 5 Evaluate, analyse and release | Not started | G4, G5 |

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
