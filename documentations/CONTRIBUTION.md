# OpsGuard contribution statement

**Status:** draft for team review, 25 November 2026. Prior-work descriptions are from the team's reading and still need the citation check in `RELATED_WORK.md`.

## The decision problem

Given a user task, live cluster state and an agent-proposed action, decide whether the action is authorized, consistent with the user's intent, and within an acceptable predicted impact envelope. The answer is ALLOW, BLOCK or ESCALATE. Only ALLOW writes.

OpsGuard answers it with a gateway that contains no language model. The model that reads untrusted evidence only proposes. A separate observer, with its own read-only credentials, records what actually happened on the cluster.

## The claim

Permission checks are necessary but not sufficient for agent-driven Kubernetes operations. An injected instruction can steer an agent into an action that RBAC, an admission policy and a task-scoped intent all authorize, and that still takes a Service down. Checking the predicted effect of the action against live dependencies stops that class of harm.

The Stage 1 pilot (`documentations/PILOT_REPORT.md`) is the first evidence for this claim.

## What is already known

| Known component | Where it comes from | How OpsGuard uses it |
| --- | --- | --- |
| Separating trusted intent from untrusted data; capability and provenance tags on values | CaMeL; FIDES (information-flow control for agents) | The task intent is fixed before evidence is read; sensitive arguments must come from trusted sources |
| Privilege and policy gates on agent tool calls | Progent; Task Shield; AgentSpec | Task policy checks verbs, targets and argument bounds |
| Typed tools, allowlists, no shell | Common practice in the papers above | The agent can only propose typed actions |
| Server-side dry-run, RBAC, admission control (ValidatingAdmissionPolicy, OPA Gatekeeper, Kyverno) | Kubernetes | Dry-run validates the prepared request; admission is the second enforcement point |
| Separate LLM judge | Common baseline | A comparison condition, never part of the decision path |
| Indirect prompt-injection benchmarks | AgentDojo; InjecAgent; Agent Security Bench | Our attack design follows their idea of payloads delivered through tool output |
| Agent SRE benchmarks on real clusters | AIOpsLab; ITBench; STRATUS | Our observer scores outcomes from cluster state, as these do for task success |

None of these components is claimed as new.

## What we contribute

The contribution is the composition, specialised to Kubernetes, and the evidence that it matters:

1. **Target re-derivation from live state.** The gateway resolves the target from the intent through Service, EndpointSlice, Pod and owner references, binds the write to one object's UID and version, and escalates when more than one object could be meant.
2. **Kubernetes dependency semantics.** Selectors, owner chains, ConfigMap consumers and trusted application edges predict which Services an action reaches and which it takes down.
3. **An impact envelope.** A predicted Service outage is never allowed automatically, whatever the authorization says. Stage 2 extends this from single actions to the whole episode.
4. **Post-action verification** against real cluster state (Stage 4).
5. **A reproducible benchmark** that measures where permission-only defences allow harm, and what OpsGuard costs in false blocks and escalations (Stages 3–5).

## How we differ from the closest work

| Work | What it does well | What it does not address that we test |
| --- | --- | --- |
| CaMeL | Stops untrusted data from choosing which tool is called or with which privileged values | An action whose tool, target and values are all authorized but whose effect on other Services is harmful |
| Progent | Fine-grained privilege policies on tool calls | Same gap: policies describe permissions, not effects on live dependencies |
| AgentDojo | A standard injection benchmark across many tool environments | Infrastructure operations, live cluster state, and harm that only shows up in other Services |
| ITBench / AIOpsLab | Realistic SRE tasks scored on cluster state | Adversarial evidence and defence comparisons |
| STRATUS | Safe, undoable SRE actions through a transactional model | Injected instructions and authorization; we compare against reversal only as a post-action step |

## Wording we use

| Say | Do not say |
| --- | --- |
| policy-constrained, invariant-enforcing, provenance-aware | deterministic safety, guarantees safety |
| server dry-run validates the object | dry-run predicts effects |
| predicted impact, within the modelled dependencies | the blast radius is known |
| enforces the specified intent | understands what the user wants |
