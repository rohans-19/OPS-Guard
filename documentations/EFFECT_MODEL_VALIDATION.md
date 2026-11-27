# Effect model v1: design and validation

Owner: Impact owner (Vikas Y K). Work package S3-P4. Status on 27 November 2026: **model and harness built and unit-tested; the kind validation has not been run yet**, so no accuracy figure exists and `effect-model-v1` is not tagged. The model version string is `effect-model-v1-dev` until the gate is met.

## Why

A server dry-run validates the object: the API server would accept it. It does not predict what controllers do next (rollouts, autoscaling, endpoint changes, cascading deletes) or which other Services break. Allowed-but-harmful attacks live in that gap. The effect model predicts impact within the modelled dependencies, and this document defines how that prediction is measured against real cluster behaviour. We never claim the blast radius is known.

## What the model reads

`src/opsguard/impact/kinds.py` lists 17 namespaced kinds: Deployment, StatefulSet, DaemonSet, Job, ReplicaSet, Pod, Service, EndpointSlice, Ingress, NetworkPolicy, ConfigMap, Secret (metadata only), PersistentVolumeClaim, HorizontalPodAutoscaler, ServiceAccount, RoleBinding and PodDisruptionBudget. These are the 16 kinds in the Stage 3 table, plus PodDisruptionBudget, which the scale-down rule needs. CRDs and operator resources are followed through ownerReferences only; that is a stated limitation.

- `KubernetesInventory(effect_kinds=...)` reads the extra kinds; a kind that was not read is `None` in the snapshot and *unknown* in `ClusterState`, never "none exist". A rule that depends on an unknown kind reports uncertainty, so the gateway escalates (`IMPACT_UNKNOWN`).
- The lab reader identity gets `get` and `list` on these kinds only (`reader_rules()`), with **Secrets excluded**: listing Secrets would let the reader see their values, and the model only needs Secret names, which it takes from Pod specs.
- Application-level facts come only from a trusted topology file per fixture (`opsguard-topology/v1`): which workload calls which Service, which values of a setting are healthy or degraded for each consumer, how each consumer reloads (`live` or `on_restart`) and how a bad value shows (`health`, `readiness`, `crash`), which Services are user-facing, and which workloads hold data. Evidence never feeds it.

## Edges

| Edge | Source | From → to |
| --- | --- | --- |
| owns | static | owner → object with that ownerReference |
| mounts, env | static | ConfigMap, Secret, PVC → workload and Pod (volumes, projected volumes, envFrom, valueFrom) |
| uses_sa | static | ServiceAccount → workload and Pod |
| binds | static | RoleBinding → ServiceAccount subject |
| selects, selects_template | static | Pod or workload template → Service whose selector matches |
| slice_of | static | EndpointSlice → Service (`kubernetes.io/service-name`) |
| backend | static | Service → Ingress |
| targets | static | HPA → its scale target |
| isolates | static | NetworkPolicy → selected Pods and workloads |
| constrains | static, not propagating | PodDisruptionBudget → workload |
| member | dynamic | Pod → EndpointSlice, with the endpoint's ready condition |
| readiness | dynamic | Pod Ready condition (node attribute used by capacity) |
| calls | topology | Service → calling workload |
| reads_setting | topology | ConfigMap → declared consumer |

## Controller-effect rules

| Change | Rule | Prediction |
| --- | --- | --- |
| restart, set_image, rollback, set_resources | rollout | Lowest ready count is min(ready now, replicas − maxUnavailable) for RollingUpdate (25% defaults, rounded like the controller), 0 for Recreate; StatefulSet and DaemonSet lose one at a time. 0 is S3, otherwise S1 (Pods are replaced). Unknown strategies or budgets the controller rejects are uncertain. New ReplicaSet, Pod churn, EndpointSlice and PDB status updates are predicted |
| scale | scale, pdb | Scale-down removes unready Pods first: ready after = min(ready now, new replicas). Below a PodDisruptionBudget's minimum is S2, 0 is S3. Scale-down with HPAs or PDBs unread is uncertain |
| scale with an HPA | hpa_override | The HPA clamps a manual scale to [minReplicas, maxReplicas] |
| set_selector | selector | Ready endpoints before and after, from live Pods when read; 0 is S3, fewer is S1, different backends is S1 |
| set_config | config | Declared values per consumer: healthy no effect; degraded follows the symptom (health: all backends S3, some S2; readiness or crash: all S3, some S1); undeclared values and consumers that may read the key are uncertain; `on_restart` or env-only consumers are latent harm (counted in b4 and the envelope, not in immediate tiers) |
| delete_pod | delete_pod | The owner recreates the Pod; capacity drops by one meanwhile; a Pod without an owner is irreversible |
| set_hpa_bounds | hpa_bounds | New bounds that exclude the current replica count act as a scale |
| patch_networkpolicy | netpol | Declared callers that lose an allowed peer are degraded (S2 hard, S1 soft); no ingress rules is S3; port-level rules and egress are uncertain |
| delete | delete | Cascade through ownerReferences; PVC is data loss; RoleBinding or ServiceAccount is a privilege change |
| any | calls | An S3 Service degrades its declared callers (S2 hard, S1 soft), repeated until stable |

Capacity comes from observed Pods when they were read, then from the controller's status (`readyReplicas`, omitted by Kubernetes when 0), then from the spec, in which case the prediction is marked `approximate`. Workloads changed earlier in the same episode use the spec.

## Blast-radius vector

| | Dimension | Definition |
| --- | --- | --- |
| b1 | Workloads affected | Workloads whose Pods, capacity, configuration or traffic change, including callers degraded through the topology |
| b2 | Namespaces affected | Namespaces of every affected object |
| b3 | User-facing Services affected | Affected Services behind an Ingress or declared user-facing |
| b4 | Availability impact | Worst predicted tier S0–S3, latent settings included |
| b5 | Data-loss potential | A PVC or a declared data store's data would be lost |
| b6 | Privilege change | RoleBinding or ServiceAccount changed, or a workload's identity or token mount changed |
| b7 | Secret exposure path | A Secret changed, or a workload gains a reference to a Secret |
| b8 | Reversibility | reversible; reversible_with_downtime (S2 or worse, or a recreated object); irreversible (data loss, or deletion of an object no controller recreates) |

## Envelope check (S2-P4 step 2, extended)

`EnvelopeDecider(effect_model="v1", topology=..., complete_kinds=...)` checks the action alone, in this order, and escalates on the first breach: `ENVELOPE_DEGRADATION` (any Service at S2 or worse, now or latent), `IMPACT_UNKNOWN`, `ENVELOPE_DATA_LOSS`, `ENVELOPE_PRIVILEGE` and `ENVELOPE_SECRET` (unless the intent allows them), `ENVELOPE_DELETION`, `ENVELOPE_IRREVERSIBLE`, `ENVELOPE_SERVICES`, `ENVELOPE_NAMESPACES`. It then predicts the same action on the episode's cumulative state (start snapshot plus every approved change) and blocks with `ENVELOPE_CUMULATIVE` when only the combination degrades a Service or exceeds the Services limit. Inside the envelope the graded score decides, with b8 as the irreversibility term (0, 0.5, 1).

`complete_kinds` lets a trusted fixture declare kinds it knows are absent although they were not read (memory fixtures have no PDBs or HPAs). Without it, unread kinds stay unknown.

v0 (`predict_impact`) stays the default everywhere, so `v1.0-reference` results and Stage 2 parity are unchanged. Switching runners to v1 is a decision for the team once the gate below is met.

## Validation method

`python -m opsguard.impact.validate` runs a catalogue of changes on fresh kind namespaces.

1. Apply the fixture, wait for every rollout, wait 5 s, read every kind except Secrets (the recorded state).
2. Probe every Service for the baseline (ready endpoints through EndpointSlices, `/health` through the API-server proxy).
3. Start a watch per kind (`kubectl get --watch-only --output-watch-events`), wait 1 s, make the change with kubectl.
4. Sample every Service every 0.5 s for 40 s (120 s for ConfigMap changes, because the kubelet refreshes mounted volumes within about a minute).
5. Stop the watches. Observed changes are unique (kind, name, operation) triples from the watch events, each with its owner chain; Events, Leases, Endpoints and ControllerRevisions are ignored.
6. Delete the namespace. A failure anywhere is a harness error, recorded and never scored.

Every record stores the raw state, the change, the events and the samples, so `rescore` recomputes predictions from the recorded state with the current model. Iterating the model does not require new cluster runs, but the final figure must come from a fresh run with the frozen model.

**Metrics.**
- *Static recall* = observed object changes matched by any predicted change ÷ all observed object changes (micro-averaged, Wilson 95% interval). A prediction may name an object or stand for any object of a kind under an owner (new Pods and ReplicaSets have generated names).
- *Precision* = predicted changes matched by at least one observed change ÷ all predicted changes.
- *Tier accuracy* = predicted immediate tier = observed tier, per Service per trial, over trials without uncertainty. We also report the under-prediction rate (predicted below observed, the unsafe direction), within-one accuracy and the confusion matrix. Trials with uncertainty are counted separately.

**Observed tier**: S3 if a Service has 0 ready endpoints at any sample (with endpoints at baseline) or `/health` fails for at least 2 s in a row; S2 if `/health` fails at some sample; S1 if ready endpoints drop below the baseline at any sample or its backing Pods are replaced; else S0.

**Gate (G2, S3-P4)**: static recall at least 0.95 on each of the three topologies. When it is not met, add the missing edges or rules shown in `missed_by_kind` and `recall_by_rule`, rescore, then run fresh trials. Freeze and tag `effect-model-v1` when the gate is met.

## Topologies and change catalogues

| Topology | Fixture | Catalogue | Status |
| --- | --- | --- | --- |
| checkout-payments | `checkout_payments_fixture()` in `impact/validate.py` (pilot app: checkout ×3, payments ×2, web ×1, PDB, Ingress, NetworkPolicy, two ConfigMaps) with `benchmark/fixtures/effect_validation/checkout_payments/topology.json` | 22 changes (19 harmless, 3 deliberately harmful to test S2/S3); `--repeats 2` gives 44 trials | Ready to run |
| three-tier app | S3-P3 (Executor owner) | Drop `manifest.json`, `topology.json`, `changes.json` into `benchmark/fixtures/effect_validation/three_tier/` | Waiting for the fixture |
| Online Boutique | S3-P5 (Benchmark owner) | Same, in `online_boutique/` | Waiting for the fixture |

The checkout-payments topology declares no calls because the pilot app's workloads do not call each other; the `calls` rule is validated on the three-tier app and Online Boutique, where calls are real.

## Commands

```powershell
# One-time lab (Docker Desktop running)
kind create cluster --name opsguard-lab
docker pull python:3.13-slim
kind load docker-image python:3.13-slim --name opsguard-lab

# Validate on the first topology (about 45-60 minutes)
.\.venv\Scripts\python.exe -m opsguard.impact.validate run --fixture checkout-payments `
    --kind-cluster opsguard-lab --image python:3.13-slim --repeats 2 `
    --output results/effect-validation/checkout-payments

# After a model change, rescore the recorded trials without the cluster
.\.venv\Scripts\python.exe -m opsguard.impact.validate rescore `
    --input results/effect-validation/checkout-payments/trials.jsonl `
    --output results/effect-validation/checkout-payments/scores.json

# Results page once all three topologies have scores
.\.venv\Scripts\python.exe -m opsguard.impact.validate report `
    --scores results/effect-validation/checkout-payments/scores.json `
             results/effect-validation/three-tier/scores.json `
             results/effect-validation/online-boutique/scores.json `
    --output documentations/EFFECT_MODEL_VALIDATION_RESULTS.md
```

## Known limits (for the paper)

- Predictions are within the modelled dependencies; application behaviour not in the topology file is invisible.
- HTTP health is probed through the API-server proxy, which a NetworkPolicy can block; fixtures allow it with an ipBlock rule.
- Port-level NetworkPolicy rules, egress rules, Jobs, CRDs and operators are reported as uncertain or out of scope.
- An HPA without a metrics server does not act; HPA rules are validated only where the lab installs one.
- Static recall counts API object changes. It does not measure application-level effects; tier accuracy does that through probes.
