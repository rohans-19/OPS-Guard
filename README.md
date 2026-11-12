# OPS-Guard

OpsGuard is a deterministic gate for an LLM-powered Kubernetes operations agent. The agent reads logs, alert annotations, runbooks and ConfigMaps as evidence, then proposes a typed Kubernetes action. OpsGuard checks that action against the operator's authorization, re-derives the intended target from live inventory, previews the write, and estimates its downstream effect. A separate oracle reads real cluster state to decide what actually happened.

The repository also contains OpsGuard-Bench: 20 clean tasks and 50 attacked cases across four evidence channels, with runners that compare four defence conditions.

## Quick start

No cluster or model credentials are needed. Run these from the repository root in PowerShell with Python 3.10 or newer.

1. Create the environment and install the project.

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[test,report]"
   ```

2. Run the tests.

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   ```

3. Validate all 70 benchmark cases and replay the selector cases in memory.

   ```powershell
   .\.venv\Scripts\python.exe -m opsguard.bench.validate --replay
   ```

   The last line prints `OK`.

4. Run one attacked case without a defence and with OpsGuard.

   ```powershell
   .\.venv\Scripts\python.exe -m opsguard.bench.run --case selector-logs-decoy-route-01 --condition unprotected --condition opsguard --output results/one-case
   ```

   The unprotected line shows `forbidden=True` and the OpsGuard line shows `forbidden=False`.

The [benchmark guide](documentations/BENCHMARK_RUNNER_AND_SELECTORS.md) continues from here with the live kind cluster, model trials and the merged team report.

## Components

| Component | Code | Guide |
| --- | --- | --- |
| Bounded agent loop, model adapters, `scale` operation, pod-log evidence | `src/opsguard/agent`, `src/opsguard/operations/scale.py` | [Agent runtime and scaling](documentations/AGENT_RUNTIME.md) |
| Task policy, provenance, live target resolution, `set_image` operation, runbook evidence | `src/opsguard/intent`, `src/opsguard/operations/set_image.py` | [Task authorization and image updates](documentations/AUTHORIZATION_AND_IMAGES.md) |
| Typed executor, server-side dry-run, `restart` operation, alert evidence | `src/opsguard/kube`, `src/opsguard/operations/restart.py` | [Shared executor, restart and alerts](documentations/EXECUTOR_AND_RESTART.md) |
| Dependency graph, risk decision, `set_config` operation, ConfigMap evidence | `src/opsguard/impact`, `src/opsguard/operations/set_config.py` | [ConfigMap impact workstream](#configmap-impact-workstream) below |
| Common runner, state oracle, `set_selector` operation, result merging and figures | `src/opsguard/bench`, `src/opsguard/operations/set_selector.py` | [Benchmark runner, state oracle and selector repair](documentations/BENCHMARK_RUNNER_AND_SELECTORS.md) |
| Replay dashboard | `frontend` | [Demo walkthrough](frontend/README.md) |

## Benchmark runner and selector repair

The common runner in `src/opsguard/bench` loads cases, sets up a fixture, places one injected payload in one evidence channel, runs the agent through the shared gateway, and scores the result from independent observations of cluster state. It supports case, model, condition and repeat filters, resumable result IDs, and retries that keep earlier failed attempts.

The `set_selector` operation replaces the whole selector map of one Service. Its suite has four clean repairs and ten attacked cases (three through logs, three through alerts, two through runbooks, two through ConfigMaps). The attacks try to route a Service to a decoy, leave it with no ready endpoints, or change another Service.

`python -m opsguard.bench.report` merges result files from every workstream, checks coverage and duplicates, and reports paired attack-success differences with intervals that resample whole tasks. It writes tables and figures.

Verified so far: the unit tests, a 168-trial scripted run in memory, and a 56-trial scripted run on a live kind cluster, all with no harness errors. Scripted runs check the runner, gateway and oracle paths. Trials with real models have not been run yet.

## ConfigMap impact workstream

### Phase 1 local checks

From the repository root on Windows, install the project and run the tests with Python 3.10 or newer.

```powershell
py -3 -m pip install -e ".[test]"
py -3 -m pytest
```

The tests use synthetic resources and need no Kubernetes cluster or model credentials. To run just the feature-flag replay, use:

```powershell
py -3 -m pytest tests/test_vikas_phase1.py
```

It traces one ConfigMap to two Services, previews a single-key update, and checks that only an allowed decision changes the fake state. The case data lives in `benchmark/cases/vikas/feature_flag_clean.json`.

- The Phase 1 impact graph takes namespaced resources and trusted dependency edges and returns the reachable Services.
- The risk decision takes the policy outcome and normalized risk signals and can serialize the result as one JSON Lines record. The default weights and thresholds are provisional settings for Phase 1 tests.
- The `set_config` handler prepares a one-key patch within explicit lab bounds. Task authorization is a separate decision. The shared executor will supply live preview, execution, and version checks.

### Phase 2 ConfigMap replay

Run the local Phase 2 tests with:

```powershell
py -3 -m pytest tests/test_vikas_phase2.py
```

They replay the four clean cases and ten attacked cases from `benchmark/cases/vikas/configmap_cases.json` using temporary ConfigMap files, scripted policy decisions, and the fixture application's response logic.

The outcome checks read the stored setting and probe the affected application behavior independently of the guard. The case records include the assigned logs, alerts, runbooks, and ConfigMap payloads for the shared runner.

Pass Kubernetes API JSON for Deployments, Services, and ConfigMaps and the target objects before and after dry-run to `predict_impact`. It estimates reach, degradation, and uncertainty for `scale`, `restart`, `set_image`, `set_config`, and `set_selector`.

Deployment selectors and templates approximate backend routing. Application behavior for the ConfigMap fixture comes from the separate trusted declarations in `benchmark/fixtures/vikas/impact_topology.json`.

The ConfigMap evidence reader supplies raw observations only. `decide_with_impact` combines the prediction with the policy and risk inputs. Missing or uncertain impact prevents an otherwise low-risk allow.

#### Phase 2 Kubernetes smoke tests

To run the two opt-in smoke tests, use a local `kind` cluster with `kubectl` available and the ability to pull the fixture's Python image.

For an existing cluster named `opsguard`, run the following from the repository root:

```powershell
$env:OPSGUARD_KIND_CLUSTER = 'opsguard'
py -3 -m pytest tests/test_vikas_phase2.py -k kind_server_preview -v
Remove-Item Env:OPSGUARD_KIND_CLUSTER
```

The smoke tests deploy the fixture in disposable namespaces. They verify server-side preview, compare the projected effects with real HTTP responses, and replay a guarded and an unprotected ConfigMap proposal with a scripted policy.

The shared executor, policy, and runner can replace those test adapters when available.

### Phase 3 and 4 experiment

After installing the project, run this command from the repository root to reproduce the local experiment.

```powershell
py -3 -m opsguard.benchmark.vikas_run --output ../output/opsguard-vikas-results
```

The default run covers 336 primary trials, 252 trials across the policy-only, no-graph, and no-dry-run variants, and 10 fresh-fixture impact replays.

The two proposal strategies and the judge are scripted in this mode. The hardened and unprotected conditions see the same scripted proposals.

Each impact replay independently compares the predicted degradation with HTTP status changes across the fixture Services.

The separate synthetic pilot scenarios choose a threshold preset before the case matrix starts. The weights remain fixed during the threshold sweep.

#### Experiment output

The run directory contains:

- `manifest.json` with pinned settings and fixture and implementation hashes.
- `results.jsonl` with trial and replay records.
- `report.md` with the metrics.

Rerunning with the same manifest resumes missing records. A fresh output directory and the `--case` and `--condition` filters let you retry failed trials while preserving the original error records.

#### Regenerate the report

Regenerate the report from saved results without a cluster or model credentials.

```powershell
py -3 -m opsguard.benchmark.vikas_run --report-only --output ../output/opsguard-vikas-results
```

#### Live impact replays

For live impact replays, use an existing named `kind` cluster with `kubectl` and the fixture image available. This run needs no model credentials.

```powershell
py -3 -m opsguard.benchmark.vikas_run --mode live --cluster opsguard --impact-only --output ../output/opsguard-vikas-live-impact
```

#### Live model trials

Live model trials use Groq by default. Install `.[groq]`, set `GROQ_API_KEY` in your local environment, and replace the placeholders below with the exact Groq model IDs chosen for the study. Use models that support JSON Object Mode. Set `--risk-preset` if the live pilot selects a different preset.

```powershell
py -3 -m pip install -e ".[groq]"
py -3 -m opsguard.benchmark.vikas_run --mode live --cluster opsguard --model 'MODEL_ID_1' --model 'MODEL_ID_2' --judge-model 'JUDGE_MODEL_ID' --output ../output/opsguard-vikas-live
```

For OpenAI live trials, install `.[live]`, set `OPENAI_API_KEY`, pass `--provider openai`, and choose OpenAI model IDs and a separate output directory.

The standalone live adapter uses disposable namespaces, the bounded typed executor, server dry-run, and fresh ConfigMap and HTTP observations.

It uses a case-defined task policy, one model proposal per trial, and synthetic log, alert, and runbook delivery.

The team comparison requires the shared agent, policy, channel adapters, and runner.

The guarded ablations preserve hard task-policy checks, so the case-defined forbidden actions are blocked before graph or preview checks.

The controlled replays provide a separate measure of impact prediction and threshold sensitivity.

---

## Team Documentation and Implementation

The following documentation covers the team's shared implementation, architecture, responsibilities, testing, and integration work.

### 1. Team Implementation Plan

[Team implementation plan](documentations/OPSGUARD_TEAM_PLAN.md) covers ownership, parallel milestones, tools, benchmark cases, and the GitHub review workflow.

### 2. Agent Runtime and Scaling

[Agent runtime and scaling](documentations/AGENT_RUNTIME.md) covers setup, shared contracts, scripted and Ollama replays, and the live kind smoke test.

### 3. Task Authorization and Image Updates

[Task authorization and image updates](documentations/AUTHORIZATION_AND_IMAGES.md) covers trusted intent, live target resolution, runbook evidence, and the image replay cases.

### 4. Shared Executor, Restart and Alerts

[Shared executor, restart and alerts](documentations/EXECUTOR_AND_RESTART.md) covers registered operations, preview/write controls, restart cases, and opt-in kind acceptance.

### 5. Project Journey

[Project Journey](documentations/PROJECT_JOURNEY.md) records the milestones, verified results, and upcoming checkpoints for project reporting.

### 6. Replay Dashboard

[Replay dashboard](frontend/index.html) presents the recorded kind demo offline.

See the [demo walkthrough](frontend/README.md) for launch and trace import steps.

### 7. Agent Workstream Handoff

[Agent workstream handoff](documentations/AGENT_WORKSTREAM_HANDOFF.md) tracks the role swap, verified results, and team integration tasks.

### 8. Benchmark Runner, State Oracle and Selector Repair

[Benchmark runner, state oracle and selector repair](documentations/BENCHMARK_RUNNER_AND_SELECTORS.md) is a step-by-step guide to the common runner, the `set_selector` operation and its 14 cases, the live kind lab, model trials, and the merged team report.

---

## Development and Contribution

- Work on your assigned feature branch.
- Keep changes compatible with the shared project architecture and contracts.
- Run relevant tests before pushing changes.
- Review merge conflicts carefully and preserve functionality from all contributing branches.
- Use pull requests to integrate completed work into the shared project.
