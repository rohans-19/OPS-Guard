// Add milestones in chronological order. See frontend/README.md for the update workflow.
globalThis.OPSGUARD_JOURNEY = [
  {
    when: 'November 6, 2026', date: '2026-11-06', status: 'merged',
    title: 'The team agreed on what OpsGuard should prove',
    owner: 'Five-person project team',
    detail: 'The implementation plan defined a shared action schema, four evidence channels, and an independent observer to measure what actually changes in Kubernetes.',
    links: [{ label: 'Team plan PR #1', href: 'https://github.com/rohans-19/OPS-Guard/pull/1' }],
  },
  {
    when: 'November 9, 2026', date: '2026-11-09', status: 'verified',
    title: 'The agent could request a real scale operation',
    owner: 'Rishab Nayak · Agent and scaling',
    detail: 'Rishab took over Rohan S’s agent workstream. The bounded loop validates proposals and sends them to a gateway. The typed scale handler prepares the Kubernetes request, and the pod-log reader records where the evidence came from.',
    links: [{ label: 'Runtime changes', href: 'https://github.com/rohans-19/OPS-Guard/pull/2/files' }],
  },
  {
    when: 'November 9, 2026', date: '2026-11-09', status: 'verified',
    title: 'The standalone lab reproduced the attack on kind',
    owner: 'Rishab Nayak · Verification',
    detail: 'The audit passed 23 Python tests and ran 42 scripted memory trials. All four clean cases completed on Kubernetes. Across the four live pod-log attack pairs, standalone task policy withheld every forbidden change and completed every intended scale. One unprotected run hit a stale resource version during recovery.',
    links: [{ label: 'Replay the target swap', href: '#replay' }],
  },
  {
    when: 'November 9–10, 2026', status: 'pilot',
    title: 'Two local models ran through the same adapter',
    owner: 'Rishab Nayak · Model pilot',
    detail: 'Qwen and Llama saw the same injected log in the in-memory pilot. Llama followed the injection in the unprotected run. Qwen’s hardened run stopped after repeated out-of-scope namespace proposals. These single runs give the team concrete failure cases to investigate.',
    links: [{ label: 'Model pilot results', href: 'https://github.com/rohans-19/OPS-Guard/blob/main/documentations/AGENT_WORKSTREAM_HANDOFF.md#verified-results' }],
  },
  {
    when: 'November 10, 2026', date: '2026-11-10', status: 'merged',
    title: 'The runtime and replay console landed on main',
    owner: 'OpsGuard team repository',
    detail: 'PR #2 merged the agent, scale handler, and benchmark runner. PR #3 merged the replay dashboard and workstream handoff. The console compares the same request across conditions and opens the exact event behind each decision.',
    links: [
      { label: 'Runtime PR #2', href: 'https://github.com/rohans-19/OPS-Guard/pull/2' },
      { label: 'Replay PR #3', href: 'https://github.com/rohans-19/OPS-Guard/pull/3' },
    ],
  },
  {
    when: 'November 13, 2026', date: '2026-11-13', status: 'next',
    title: 'Phase 1 review and interface handoff',
    owner: 'Rishab Nayak · Team review',
    detail: 'Show the injected pod log, the blocked payments request, and checkout reaching three ready replicas. Confirm the gateway, executor, observer, and impact interfaces with their owners.',
    links: [{ label: 'Team handoff and next steps', href: 'https://github.com/rohans-19/OPS-Guard/blob/main/documentations/AGENT_WORKSTREAM_HANDOFF.md#who-gets-what' }],
  },
  {
    when: 'After the review', status: 'planned',
    title: 'Replay the same case through the shared OpsGuard path',
    owner: 'Sandesh · Rohan S · Prapti · Vikas · Rishab',
    detail: 'Connect target resolution, the typed executor, impact checks, and the independent observer. Verify that the baseline changes payments, full OpsGuard blocks that change, and checkout still reaches three ready replicas.',
  },
  {
    when: 'After shared integration', status: 'planned',
    title: 'Run the matched evaluation and report the results',
    owner: 'Five-person project team',
    detail: 'Freeze cases, model settings, and images. Pilot the four defence conditions, then run the agreed matrix. Report task completion and forbidden effects with denominators, failed runs, and reproduction commands.',
  },
];
