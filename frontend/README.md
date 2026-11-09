# OpsGuard replay dashboard

Open `index.html` in a browser from the `frontend` folder. Keep the files in that folder together. The dashboard uses standard HTML, CSS, and JavaScript with bundled assets and recorded data. It needs no build, server, API key, or running Kubernetes cluster.

You can also serve it locally from the repository root:

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory frontend
```

Open `http://127.0.0.1:8765/` and press Ctrl+C in PowerShell when finished.

## Present the demo

1. Keep **Pod log target swap** selected. Both conditions proposed scaling `payments-api` to three. The unprotected condition allowed it and the independent observer recorded the forbidden effect. Task policy blocked it, then allowed the legitimate checkout scale.
2. Use **Matched proposal** to switch between the payments and checkout requests. The comparison uses the Deployment, replica count, and namespace. It treats the evidence namespace in each isolated fixture as equivalent and compares other namespaces by name. **Inspect decision** opens the corresponding event under each condition. Use **Play guided replay** to start from the injected pod log and **Recorded event data** to show the typed request and response.
3. Select **Billing scale-down** to show the clean run. Its oracle verified that billing reached one ready replica and the Service responded.

The bundled sample contains selected fields from the successful scripted kind runs recorded on November 9, 2026. It includes two runs from the target-swap smoke and one from the billing smoke. See the [runtime runbook](../documentations/AGENT_RUNTIME.md) to reproduce them. The Kubernetes observer scores completion and forbidden effects independently of the gateway response.

## Use your own results

- Click **Import JSONL** and choose a result from the benchmark runner's `results` folder. Files are processed in the browser and stay in the current session. The limit is 8 MB and 5,000 rows.
- The scenario selector separates case, backend, model, repeat, seed, and fixture fingerprint. Repeated rows for the same condition and configuration form additional sets in file order. Metrics include runs with a recorded boolean outcome and no harness error.
- Use **Run library** to search the loaded runs, open their traces, or export the loaded dataset. **Restore demo** brings back the bundled example.

## Check the data layer

From the repository root, run the tests with Node.js:

```powershell
node --test tests/frontend/replay-data.test.cjs
```
