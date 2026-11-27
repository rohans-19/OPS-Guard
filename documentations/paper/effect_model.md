# Effect model (paper section draft)

Lead author: Impact owner (Vikas Y K). Draft of 27 November 2026. Numbers marked **[pending]** come from the kind validation (S3-P4) and the pre-registered test run (S5-P4); nothing here may be replaced by development figures.

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

Inside the envelope, a weighted score R = w_p·p + w_b·B + w_r·r ranks the remaining risk, with B = b4/3 and r from b8. Its thresholds were calibrated on development data under a pre-agreed unsafe-approval ceiling (CALIBRATION.md).

## Validation

The model is measured, not assumed correct. On fresh kind namespaces we make about 50 harmless changes per topology, plus a few deliberately harmful ones. We record every object change from watch events and every availability change from probes every 0.5 s, then compare with the prediction made from the recorded pre-change state.

| Topology | Trials | Static recall (95% CI) | Precision | Tier accuracy | Tier under-prediction |
| --- | --- | --- | --- | --- | --- |
| checkout-payments | **[pending]** | **[pending]** | **[pending]** | **[pending]** | **[pending]** |
| three-tier app | **[pending]** | **[pending]** | **[pending]** | **[pending]** | **[pending]** |
| Online Boutique | **[pending]** | **[pending]** | **[pending]** | **[pending]** | **[pending]** |

Effect-model accuracy on test trials (predicted harm vs harm observed unprotected): **[pending]**.

## Results on allowed-but-harmful and multi-step tasks

H3 (OpsGuard vs B5, B6, task policy, B11) and H5 (full OpsGuard vs A6, A7) on the sealed test split: **[pending, analysed exactly as pre-registered]**.

*Development observation (exploratory, not a result).* On the open splits, replaying the scripted attacks in memory:
- every permission-only condition allowed every harmful sequence;
- OpsGuard with the effect model allowed none and completed every legitimate task;
- without episode state, every multi-step attack succeeded.

The sensitivity analysis shows the full decision is stable under ±50% weight changes, while the weighted score alone cannot separate safe from harmful actions. The hard, effect-model-based envelope does the work.

## Limitations

- Predictions are only as good as the modelled dependencies and the trusted topology. Application behaviour that the file does not declare is invisible.
- Port-level and egress NetworkPolicy rules, Jobs, CRDs and operators are reported as uncertain or are out of scope.
- An HPA without a metrics server does not act.
- Static recall counts API object changes; tier accuracy measures availability through probes.
- The Impact-owner task families were written alongside the model. Generalisation claims rest on the held-out test split and the other owners' families.
