# OpsGuard testing guide

## Coverage

| Layer | Location | What it verifies |
| --- | --- | --- |
| Python unit and component tests | [`tests/agent`](../tests/agent), [`tests/operations`](../tests/operations), [`tests/intent`](../tests/intent), [`tests/evidence`](../tests/evidence), [`tests/impact`](../tests/impact), [`tests/kube`](../tests/kube) | Proposal validation, loop limits, scoped evidence reads, authorization, typed operations, dry-run, risk decisions, and executor error handling. |
| Offline integration and reporting | [`tests/benchmark`](../tests/benchmark), [`tests/bench`](../tests/bench), ConfigMap Phase 1 and Phase 2 tests | Scripted agent-to-gateway flow, independent outcome scoring, retries, manifests, aggregation, and reports. |
| Frontend unit tests | [`tests/frontend/replay-data.test.cjs`](../tests/frontend/replay-data.test.cjs) | Result parsing, trace attachment, condition grouping, proposal matching, and score calculations. |
| Kubernetes integration | [`tests/integration`](../tests/integration), [`tests/test_vikas_phase2.py`](../tests/test_vikas_phase2.py), and the smoke commands below | Clean fixture setup, real writes through supported paths, and observed Kubernetes or HTTP outcomes. |
| Browser and reporting system checks | Replay console and `opsguard.bench.report` | Recorded decisions, imported results and trace files, restored demo data, and report coverage. |

## Run the default suites

Open PowerShell at the repository root. Use Python 3.10 or newer and Node.js 22 or newer. The Python extras include the test, reporting, and Groq adapter dependencies. The Groq adapter tests use fakes and require no API credentials.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,report,groq]"
.\.venv\Scripts\python.exe -m pytest -q -ra
node --test tests/frontend/replay-data.test.cjs
.\.venv\Scripts\python.exe -m opsguard.bench.validate
```

Run the default suite in a shell without the live opt-in variables `OPSGUARD_KIND_CLUSTER` and `OPSGUARD_FIXTURE_IMAGE`. Pytest reports why each case was skipped. The case validator checks definitions and fixture contracts across all five workstreams.

For a narrower check while changing policy or executor code, run `python -m pytest -q tests/intent tests/kube` with the project environment active. See the [CI workflow](../.github/workflows/ci.yml) for the Python 3.11 and 3.13 matrix, Node.js frontend checks, case validation, and the manually enabled kind job.

## Run clean Kubernetes integration checks

Start Docker Desktop and check the local `opsguard` kind cluster. Use the repository's disposable test fixtures. The restart case needs a local Alertmanager instance. [kind setup and image loading](BENCHMARK_RUNNER_AND_SELECTORS.md#part-2-run-on-a-live-kind-cluster) · [Restart fixture setup](EXECUTOR_AND_RESTART.md#live-restart-replay-and-model-experiments)

```powershell
docker info --format '{{.OSType}}'
kind get clusters
kubectl --context kind-opsguard get nodes
```

Expect `linux`, an `opsguard` cluster, and a `Ready` control-plane node. The commands below use `python:3.12-alpine` in the cluster. Check `http://127.0.0.1:9093/-/ready` before the restart case. Use a fresh output directory to preserve earlier results.

```powershell
$testResults = "results/phase1-verification-$(Get-Date -Format yyyyMMdd-HHmmss)"
$opsGuardPython = '.\.venv\Scripts\python.exe'

& $opsGuardPython -m opsguard.benchmark.run_scale --kind-cluster opsguard --image python:3.12-alpine --case scale-checkout --condition task-policy --output "$testResults/scale.jsonl"
& $opsGuardPython -m opsguard.benchmark.run_restart --backend kind --kind-cluster opsguard --image python:3.12-alpine --case restart-checkout --condition task-policy --output "$testResults/restart.jsonl"
& $opsGuardPython -m opsguard.bench.run --backend kind --kind-cluster opsguard --image python:3.12-alpine --case selector-checkout-clean-01 --condition opsguard --output "$testResults/selector"

$env:OPSGUARD_KIND_CLUSTER = 'opsguard'
try {
    & $opsGuardPython -m pytest -q tests/test_vikas_phase2.py -k 'kind_server_preview and clean'
} finally {
    Remove-Item Env:OPSGUARD_KIND_CLUSTER
}

& $opsGuardPython -m opsguard.benchmark.run_image --case image-checkout --condition task-policy --output "$testResults/image.jsonl"
```

For each replay, check that `legitimate_met` is `true` and `error` is `null`. A clean replay can have an unscored forbidden-effect field. Check the `backend` field before reporting the result. The image command exercises the in-memory runner. The ConfigMap check should show `1 passed` and may take several minutes for the HTTP state to converge.

## Verify the browser and report

1. Open [`frontend/index.html`](../frontend/index.html) in Chrome or Edge. In the bundled scale scenario, inspect the task-policy decision and the subsequent checkout proposal.
2. Use **Import JSONL** for `scale.jsonl`. Confirm the run is labelled **Recorded on kind** and shows **Safe completion**.
3. Use **Run library → Import run folder** for the `selector` folder. Verify the attached trace count and expand **Observed state before and after**. Use **Restore demo** to return to the bundled dataset.
4. Generate a report from the single clean selector run. Expect one recorded trial and partial coverage against the default 56-trial scripted matrix.

```powershell
& $opsGuardPython -m opsguard.bench.report "$testResults/selector" --output "$testResults/selector-report" --backend kind --allow-incomplete
```

## Verification record for November 12, 2026

The verification ran on Windows with Python 3.12.7 and Node.js 24.11.1. The selector manifest identifies `d65a930` as the tested revision. The local cluster was `opsguard`, running Kubernetes v1.37.0.

| Check | Result |
| --- | --- |
| Default Python suite | **316 passed, 4 live cases skipped**. |
| Frontend suite and case validation | **10 frontend tests passed** and **70 case definitions validated**. |
| Clean scale, restart, and selector on kind | All three completed with no harness error. Restart passed after starting Alertmanager and retrying the failed setup. |
| Clean ConfigMap on kind | **1 passed** in the separate live run. Server-side preview left persisted data untouched, and the write produced the expected ConfigMap and HTTP state. |
| Clean image update | Passed in memory. The simulated observer confirmed the requested image and readiness. |
| Browser and report | Fresh scale JSONL and the selector folder imported successfully. The selector trace attached, the bundled dataset restored, and the report identified its coverage as **1 of 56**. |

Local artifacts are under `results/phase1-verification-20261112/`. The successful restart replay is `restart-kind-retry.jsonl`. The selector trace and report are in `selector/` and `selector-report/`. Keep the original failed row when comparing the retry.

The earlier **56 scripted selector trials** belong to a separate benchmark run. This verification leaves **three opt-in live pytest cases**, an **actual changed-image rollout on kind**, and the **matched real-model evaluation** open. [Earlier selector benchmark verification](BENCHMARK_RUNNER_AND_SELECTORS.md#part-2-run-on-a-live-kind-cluster)

## Troubleshoot common failures

| Symptom | Check |
| --- | --- |
| Pytest stalls at the first async test in a restricted runner | Try the same suite from normal PowerShell. The initial run stalled while Windows asyncio was opening a local loopback socket. |
| Docker pipe missing or kind API unreachable | Start Docker Desktop in Linux container mode and rerun the three cluster preflight commands. |
| Restart returns `FIXTURE_ERROR:ConnectError` | Verify that the fixture Alertmanager is ready on `127.0.0.1:9093`, then rerun the case with a fresh output filename. |
| Selector result has a summary but no events | Import the whole folder with its `results.jsonl` and `traces/` subfolder. |
