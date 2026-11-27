# Effect model (paper section draft)

Lead author: Impact owner (Vikas Y K). Draft of 27 November 2026. The checkout-payments row records the completed kind validation of `effect-model-v1-dev`. Numbers marked **[pending]** await the remaining topology validations (S3-P4) and the pre-registered test run (S5-P4); development measurements do not fill sealed-test result fields.

## Motivation

Permission checks answer *may this request be made?* They do not answer *what will this request do?* A Kubernetes write that is fully authorized (the right verb, an allowed object, arguments within the intent's bounds) can still take a Service offline:
- through a shared ConfigMap another workload reads;
- through a rollout strategy that removes every Pod at once;
- through a selector the allowlist permits but no ready Pod matches;
- through a scale-down below a disruption budget;
- through a sequence of individually harmless steps.

A server-side dry-run validates the object; it does not predict what controllers do next. OpsGuard's effect model fills this gap *within the modelled dependencies*. It does not claim to know the blast radius.

## Inputs

The model reads 17 namespaced kinds through the gateway's read-only identity:
- workloads: Deployment, StatefulSet, DaemonSet, Job;
- ReplicaSet and Pod;
- Service, EndpointSlice, Ingress and NetworkPolicy;
- ConfigMap and Secret (metadata only);
- PersistentVolumeClaim and HorizontalPodAutoscaler;
- ServiceAccount and RoleBinding;
- PodDisruptionBudget.

A kind that was not read is unknown, not empty. Any rule that depends on it reports uncertainty, and the gateway escalates.

Application-level facts come only from a trusted topology file written with each fixture, never from evidence:
- which workload calls which Service;
- which values of a setting each consumer tolerates;
- how each consumer reloads a setting;
- which Services are user-facing.

## Graph and rules

**Graph edges.** A typed graph connects objects:
- *Static edges* come from ownerReferences, volume and environment references, ServiceAccounts and RoleBindings, Service selectors against Pods and Pod templates, EndpointSlice labels, Ingress backends, HPA targets, NetworkPolicy selectors, and PodDisruptionBudget selectors (a constraint, not a propagation path).
- *Dynamic edges* record EndpointSlice membership and Pod readiness.
- *Topology edges* record declared calls and declared setting consumers.

**Controller-effect rules.** These turn a previewed object into predicted effects:
- the rolling-update budget (`min(ready, replicas − maxUnavailable)`; Recreate drops to 0);
- remaining ready replicas against every disruption budget covering the Pods, across workloads;
- the HPA range overriding a manual scale;
- ready endpoints before and after a selector change;
- each consumer's healthy, degraded or unknown values and its reload behaviour (live or at the next restart);
- Pod recreation, new HPA bounds and declared callers losing a NetworkPolicy peer;
- cascading deletion;
- propagation of a full outage to hard callers.

Capacity comes from observed Pods, then from the controller's status, then from the spec, in which case the prediction is marked approximate.

## Blast-radius vector and decision

Each prediction yields b1–b8:
- workloads affected;
- namespaces affected;
- user-facing Services affected;
- the worst availability tier S0–S3 (latent harm included);
- data-loss potential;
- privilege change;
- a new path to a Secret;
- reversibility.

The gateway escalates when the action alone breaches the signed intent's impact envelope: a Service at S2 or worse, unknown impact, or an unallowed data-loss, privilege, Secret, deletion, irreversibility, Service-count or namespace breach. It then predicts the same action on the episode's cumulative state (the start snapshot plus every approved change) and blocks it when only the combination breaks the envelope (`ENVELOPE_CUMULATIVE`).

Inside the envelope, a weighted score R = w_p·p + w_b·B + w_r·r ranks the remaining risk, with B = b4/3 and r from b8. Provisional thresholds were calibrated on task-derived development labels under the Impact owner's proposed unsafe-approval ceiling of 0.05. Team agreement on the ceiling and confirmation using the independent labelled proposal set remain open (CALIBRATION.md).

## Validation

The model is measured against observations on fresh kind namespaces. The first topology uses 22 changes (19 harmless and three deliberately harmful), repeated twice. Object changes are recorded from watch events and availability changes from probes, then compared with the prediction made from the recorded pre-change state. The other two topology catalogues and their validation runs remain pending.

| Topology | Trials | Static recall (95% CI) | Precision | Tier accuracy | Tier under-prediction |
| --- | --- | --- | --- | --- | --- |
| checkout-payments | 44 | 98.92% (96.86%-99.63%) | 95.12% | 95.45% | 4.55% |
| three-tier app | **[pending]** | **[pending]** | **[pending]** | **[pending]** | **[pending]** |
| Online Boutique | **[pending]** | **[pending]** | **[pending]** | **[pending]** | **[pending]** |

The checkout-payments run completed on 27 November 2026 with zero harness errors and no uncertain trials, using the development model `effect-model-v1-dev`. Its 132 Service/trial comparisons include six under-predictions from S0 to S1; every comparison was within one tier. Three observed Pod changes were unmatched, and selector-rule recall was 0.75. The first topology meets the static-recall target, while Gate G2 remains open until the three-tier app and Online Boutique are validated. The model remains unfrozen and untagged. Local records are [scores.json](../../results/effect-validation/checkout-payments/scores.json) and [trials.jsonl](../../results/effect-validation/checkout-payments/trials.jsonl), with method and limitations in [EFFECT_MODEL_VALIDATION.md](../EFFECT_MODEL_VALIDATION.md).

Effect-model accuracy on test trials (predicted harm vs harm observed unprotected): **[pending]**.

## Results on allowed-but-harmful and multi-step tasks

H3 (OpsGuard vs B5, B6, task policy, B11) and H5 (full OpsGuard vs A6, A7) on the sealed test split: **[pending, analysed exactly as pre-registered]**.

*Development observation (exploratory, not a result).* On the open splits, replaying the scripted attacks in memory:
- every permission-only condition allowed every harmful sequence;
- OpsGuard with the effect model allowed none and completed every legitimate task;
- without episode state, every multi-step attack succeeded.

The sensitivity analysis shows the full decision is stable under ±50% weight changes, while the weighted score alone cannot separate safe from harmful actions. The hard, effect-model-based envelope does the work.

On 27 November 2026, Groq `openai/gpt-oss-20b` reviewed scripted candidate sequences for the five development tasks in the `ms-reroute-then-drain` family on the memory backend. B9 allowed 5/5 harmful sequences and B10 allowed 2/5; both completed 5/5 legitimate sequences. The final set has 20 records and zero provider errors after paced retries of 12 initial rate-limit failures, which remain archived. These exploratory results cover one task family and predetermined proposals; they do not measure acting-model proposal generation or fill the sealed-test H3/H5 fields. Local records are [summary.json](../../results/judges-dev/summary.json) and [manifest.json](../../results/judges-dev/manifest.json).

## Limitations

- Predictions are only as good as the modelled dependencies and the trusted topology. Application behaviour that the file does not declare is invisible.
- Port-level and egress NetworkPolicy rules, Jobs, CRDs and operators are reported as uncertain or are out of scope.
- An HPA without a metrics server does not act.
- Static recall counts API object changes; tier accuracy measures availability through probes.
- The Impact-owner task families were written alongside the model. Generalisation claims rest on the held-out test split and the other owners' families.
