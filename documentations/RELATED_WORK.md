# Related work

**Checked:** 25 November 2026, against each paper's arXiv or venue page (links below). Numbers quoted are the authors' own and come from the abstracts; verify against the full paper before citing a number in the report.

**Bottom line:** every OpsGuard component has prior art. Capability and provenance enforcement (CaMeL, FIDES), privilege policies (Progent, AgentSpec) and task-alignment checks (Task Shield) all decide whether an action is permitted or aligned with the user's task. None of them predicts what an authorized action will do to other services on live infrastructure. Agent SRE benchmarks (AIOpsLab, ITBench, STRATUS) score real cluster state but do not inject adversarial evidence. OpsGuard's claim sits in that gap: authorized but harmful actions on Kubernetes, and a benchmark that measures them.

## Prompt injection and its benchmarks

| Work | What it does | Overlap with OpsGuard | What OpsGuard adds |
| --- | --- | --- | --- |
| [Greshake et al., "Not what you've signed up for"](https://arxiv.org/abs/2302.12173) (AISec 2023) | Introduces indirect prompt injection: instructions planted in data an LLM application later retrieves | Our attacker model is exactly this, through logs, alerts, runbooks, ConfigMaps and Events | Infrastructure channels and cluster-state outcomes |
| [InjecAgent](https://arxiv.org/abs/2403.02691) (ACL Findings 2024) | 1,054 cases across 17 user tools and 62 attacker tools; direct harm and data exfiltration | Payloads delivered through tool output | Live Kubernetes state rather than simulated tool responses |
| [AgentDojo](https://arxiv.org/abs/2406.13352) (NeurIPS 2024 Datasets and Benchmarks) | Extensible environment: 97 tasks and 629 security cases (email, banking, travel); evaluates attacks and defences | Benchmark design: utility and attack success measured together; supports adaptive attacks | Operations on a real cluster, with harm that shows up in other Services |
| [Agent Security Bench](https://arxiv.org/abs/2410.02644) (ICLR 2025) | 10 scenarios, 400+ tools, 27 attack and defence methods, 7 metrics; finds current defences of limited effect | Breadth of attacks and defences compared | Kubernetes-specific semantics and state-based oracles |

## Defences that constrain what the agent may do

| Work | What it does | Overlap with OpsGuard | What OpsGuard adds |
| --- | --- | --- | --- |
| [CaMeL](https://arxiv.org/abs/2503.18813) (Debenedetti et al., 2025) | Separates the trusted user request from untrusted data; an interpreter tracks provenance and enforces security policies. Reports 77% of AgentDojo tasks solved with provable security in v2 (84% undefended) | Our trusted intent, provenance tags and model-free enforcement follow the same principle | Checks the effect of an authorized action on live dependencies. CaMeL decides whether a value may flow into a call; it does not predict outages |
| [FIDES](https://arxiv.org/abs/2505.23643) (Microsoft, 2025) | Planner that tracks confidentiality and integrity labels, enforces policies deterministically, and uses a quarantined LLM with constrained decoding. Stops all prompt injections in AgentDojo with policy checks enabled | Information-flow labels on values; quarantined reading of untrusted data | Same gap as CaMeL: permissions and flows, not infrastructure effects |
| [Progent](https://arxiv.org/abs/2504.11703) (2025) | Privilege control at the tool level through a policy DSL; restricts agents to calls needed for the task; LLMs can draft the policies | Task-scoped verbs, targets and argument bounds, like our task policy | Policies describe what is permitted, not what a permitted call will break |
| [AgentSpec](https://arxiv.org/abs/2503.18666) (ICSE 2026) | DSL for runtime rules (trigger, predicate, enforcement); enforcement includes stopping, user inspection and corrective calls | Rule-based runtime enforcement and escalation to a person | Rules come from a dependency model and live state, not hand-written triggers |
| [Task Shield](https://arxiv.org/abs/2412.16682) (ACL 2025) | Test-time guard that checks whether each instruction and tool call serves the user's goal; 2.07% attack success with 69.79% utility on GPT-4o in AgentDojo | Same idea of task alignment | Our gate contains no model, so it cannot itself be injected; Task Shield relies on LLM scoring and is exposed to adaptive attacks (the authors note this) |
| [Design Patterns for Securing LLM Agents](https://arxiv.org/abs/2506.08837) (Beurer-Kellner et al., 2025) | Six patterns sharing one principle: once an agent has read untrusted input, that input must not be able to trigger side effects | The guiding principle behind our gateway | A concrete gate and benchmark for one domain |

## Defences that detect or resist injection

| Work | What it does | Role in our study |
| --- | --- | --- |
| [Spotlighting](https://arxiv.org/abs/2403.14720) (Microsoft, 2024) | Delimiting, datamarking and encoding of untrusted input; attack success from over 50% to under 2% with little task cost on GPT-family models | Baseline B2 |
| [MELON](https://arxiv.org/abs/2502.05174) (ICML 2025) | Re-executes the trajectory with the user input masked and compares tool calls to detect injection | Candidate extra baseline; a strong detection-based comparison |

## Agents for cloud operations

| Work | What it does | Overlap with OpsGuard | What OpsGuard adds |
| --- | --- | --- | --- |
| [AIOpsLab](https://arxiv.org/abs/2501.06706) (MLSys 2025) | Deploys microservices, injects faults, generates load and exports telemetry to evaluate operations agents | Fault injection and state-based scoring on real clusters | Adversarial evidence and defence comparison |
| [ITBench](https://arxiv.org/abs/2502.05352) (ICML 2025) | 94 scenarios across SRE, CISO and FinOps; agents solve a small share of SRE scenarios | Realistic SRE tasks; reproducible scenario tooling | Security of the agent's actions under injected evidence |
| [STRATUS](https://arxiv.org/abs/2506.02009) (NeurIPS 2025) | Multi-agent SRE system in a state machine with a formal safety specification, Transactional No-Regression, for safe mitigation | Safety of operational actions; undo as a safety tool | Injection and authorization; we treat rollback as a post-action step, not the main defence |

## Kubernetes mechanisms we build on

- [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/kubernetes-api/policy-resources/validating-admission-policy-v1/) evaluates CEL expressions at the API server. It has been stable since Kubernetes 1.30 ([GA announcement](https://v1-33.docs.kubernetes.io/blog/2024/04/24/validating-admission-policy-ga)). It is our second enforcement point and baseline B6.
- RBAC is baseline B5. Server-side dry-run validates our prepared requests.

## Open questions from this review

1. Does any 2026 work check infrastructure effects of authorized actions? This search did not find one, but it was a targeted search of known papers, not a systematic survey. Run a broader search before the paper's related-work section is final.
2. Should MELON be added as a baseline alongside B9 (LLM judge)? It is training-free and has public AgentDojo code.
