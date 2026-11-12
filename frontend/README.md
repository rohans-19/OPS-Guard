# OpsGuard replay dashboard

Open `index.html` in a browser from the `frontend` folder. Keep the files in that folder together. The dashboard uses standard HTML, CSS, and JavaScript with bundled assets and recorded data. It needs no build, server, API key, or running Kubernetes cluster.

You can also serve it locally from the repository root:

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory frontend
```

Open `http://127.0.0.1:8765/` and press Ctrl+C in PowerShell when finished.

## Present the demo

The [Phase 1 presenter guide](../documentations/PHASE_1_DEMO_GUIDE.md) includes a timed walkthrough, explanations, and answers to likely review questions.

1. Start with **Scale Deployment → Pod log target swap**. Show the recorded difference between **Unprotected** and **Task policy**. Use **Inspect decision** to show the gateway result, then the legitimate checkout scale that followed the block. **Billing scale-down** provides a clean example with one ready replica and a passing Service probe.
2. Select **Restart Deployment** and then **Update container image** from **Workstream**. Open the recorded decisions for checkout to show the typed action and scored outcome.
3. Select **Update ConfigMap**. Show the recorded decision and outcome under each condition. This runner records a summary for these cases, so the dashboard displays the available evidence in place of an event timeline.
4. Select **Repair Service selector** and choose **OpsGuard** in the trace. Expand **Observed state before and after** to show the Service selector in the captured fixture state.
5. Open **Run library** to show the available cases and import the team's benchmark results. Finish by explaining how the benchmark observes task completion separately from the gateway decision.

The bundled data includes 11 scripted recordings. The three scale rows came from kind on November 9, 2026. The restart, image, ConfigMap, and selector rows came from clean in-memory runs on November 12, 2026, using benchmark commit `5447c33`. The dashboard labels the backend for each run. See the [runtime runbook](../documentations/AGENT_RUNTIME.md) for the scale setup.

## Use your own results

- Click **Import JSONL** to select one or more result files with inline traces or summary rows. Use **Import run folder** for runs with a `results.jsonl` file and a sibling `traces` directory. Keep the folder structure intact to load its referenced traces and observed state.
- Imports are processed in the browser and stay in the current session. The limit is 8 MB of JSONL and 5,000 result rows, plus up to 32 MB of trace files. Missing trace files leave the corresponding run summary available.
- The scenario selector separates case, backend, provider, model, repeat, seed, fixture and experiment fingerprints, and retry attempt. Repeated rows for the same condition and configuration form additional sets in file order. Metrics include runs with a recorded boolean outcome and no harness error.
- Use **Run library** to search the loaded runs, open their traces, or export the loaded dataset. **Restore demo** brings back the bundled example.

## Check the data layer

From the repository root, run the tests with Node.js:

```powershell
node --test tests/frontend/replay-data.test.cjs
```
