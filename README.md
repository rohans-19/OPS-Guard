# OPS-Guard

## Vikas Phase 1 local checks

From the repository root on Windows, install the project and run the tests with Python 3.9 or newer.

```powershell
py -3 -m pip install -e ".[test]"
py -3 -m pytest
```

The tests use synthetic resources and need no Kubernetes cluster or model credentials. To run just the feature-flag replay, use `py -3 -m pytest tests/test_vikas_phase1.py`. It traces one ConfigMap to two Services, previews a single-key update, and checks that only an allowed decision changes the fake state. The case data lives in `benchmark/cases/vikas/feature_flag_clean.json`.

- The Phase 1 impact graph takes namespaced resources and trusted dependency edges and returns the reachable Services.
- The risk decision takes the policy outcome and normalized risk signals and can serialize the result as one JSON Lines record. The default weights and thresholds are provisional settings for Phase 1 tests.
- The `set_config` handler prepares a one-key patch within explicit lab bounds. Task authorization is a separate decision. The shared executor will supply live preview, execution, and version checks.

## Vikas Phase 2 ConfigMap replay

Run the local Phase 2 tests with `py -3 -m pytest tests/test_vikas_phase2.py`. They replay the four clean cases and ten attacked cases from `benchmark/cases/vikas/configmap_cases.json` using temporary ConfigMap files, scripted policy decisions, and the fixture application's response logic. The outcome checks read the stored setting and probe the affected application behavior independently of the guard. The case records include the assigned logs, alerts, runbooks, and ConfigMap payloads for the shared runner.

Pass Kubernetes API JSON for Deployments, Services, and ConfigMaps and the target objects before and after dry-run to `predict_impact`. It estimates reach, degradation, and uncertainty for `scale`, `restart`, `set_image`, `set_config`, and `set_selector`. Deployment selectors and templates approximate backend routing. Application behavior for the ConfigMap fixture comes from the separate trusted declarations in `benchmark/fixtures/vikas/impact_topology.json`. The ConfigMap evidence reader supplies raw observations only. `decide_with_impact` combines the prediction with the policy and risk inputs. Missing or uncertain impact prevents an otherwise low-risk allow.

To run the two opt-in smoke tests, use a local `kind` cluster with `kubectl` available and the ability to pull the fixture's Python image. For an existing cluster named `opsguard`, run the following from the repository root.

```powershell
$env:OPSGUARD_KIND_CLUSTER = 'opsguard'
py -3 -m pytest tests/test_vikas_phase2.py -k kind_server_preview -v
Remove-Item Env:OPSGUARD_KIND_CLUSTER
```

The smoke tests deploy the fixture in disposable namespaces. They verify server-side preview, compare the projected effects with real HTTP responses, and replay a guarded and an unprotected ConfigMap proposal with a scripted policy. The shared executor, policy, and runner can replace those test adapters when available.

## Vikas Phase 3 and 4 experiment

After installing the project, run this command from the repository root to reproduce the local experiment.

```powershell
py -3 -m opsguard.benchmark.vikas_run --output ../output/opsguard-vikas-results
```

The default run covers 336 primary trials, 252 trials across the policy-only, no-graph, and no-dry-run variants, and 10 fresh-fixture impact replays. The two proposal strategies and the judge are scripted in this mode. The hardened and unprotected conditions see the same scripted proposals. Each impact replay independently compares the predicted degradation with HTTP status changes across the fixture Services. The separate synthetic pilot scenarios choose a threshold preset before the case matrix starts. The weights remain fixed during the threshold sweep.

The run directory contains `manifest.json` with pinned settings and fixture and implementation hashes, `results.jsonl` with trial and replay records, and `report.md` with the metrics. Rerunning with the same manifest resumes missing records. A fresh output directory and the `--case` and `--condition` filters let you retry failed trials while preserving the original error records.

Regenerate the report from saved results without a cluster or model credentials.

```powershell
py -3 -m opsguard.benchmark.vikas_run --report-only --output ../output/opsguard-vikas-results
```

For live impact replays, use an existing named `kind` cluster with `kubectl` and the fixture image available. This run needs no model credentials.

```powershell
py -3 -m opsguard.benchmark.vikas_run --mode live --cluster opsguard --impact-only --output ../output/opsguard-vikas-live-impact
```

For live model trials, install `.[live]`, set `OPENAI_API_KEY` in your local environment, and replace the three placeholders below with the exact model IDs chosen for the study. Set `--risk-preset` if the live pilot selects a different preset.

```powershell
py -3 -m pip install -e ".[live]"
py -3 -m opsguard.benchmark.vikas_run --mode live --cluster opsguard --model 'MODEL_ID_1' --model 'MODEL_ID_2' --judge-model 'JUDGE_MODEL_ID' --output ../output/opsguard-vikas-live
```

The standalone live adapter uses disposable namespaces, the bounded typed executor, server dry-run, and fresh ConfigMap and HTTP observations. It uses a case-defined task policy, one model proposal per trial, and synthetic log, alert, and runbook delivery. The team comparison requires the shared agent, policy, channel adapters, and runner. The guarded ablations preserve hard task-policy checks, so the case-defined forbidden actions are blocked before graph or preview checks. The controlled replays provide a separate measure of impact prediction and threshold sensitivity.
