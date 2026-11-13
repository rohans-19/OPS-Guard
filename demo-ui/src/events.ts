export type Event = {
  seq: number;
  event: string;
  timestamp: string;
  elapsed_ms: number;
  [key: string]: any;
};
export function mergeEvents(previous: Event[], incoming: Event[]) {
  const indexed = new Map(previous.map((e) => [e.seq, e]));
  incoming.forEach((e) => indexed.set(e.seq, e));
  return [...indexed.values()].sort((a, b) => a.seq - b.seq);
}
export function latest(events: Event[], name: string) {
  return events.findLast((e) => e.event === name);
}
export function describe(e: Event): string {
  const names: Record<string, string> = {
    setup: "Preparing a real Kubernetes environment",
    intent: "Your authorization is frozen",
    model_start: "Groq is choosing the next step",
    model: "Groq response received",
    read_start: "Reading diagnostic evidence",
    evidence: "Tool response supplied to the agent",
    policy_check: "Checking task authorization",
    policy: "Task policy result",
    inventory: "Read live objects and ownership",
    prepared: "Prepared an immutable Kubernetes request",
    preview: "Kubernetes dry-run returned",
    policy_recheck: "Rechecked authorization after preview",
    recheck: "Checking current authorization again",
    impact: "Predicted impact from the dependency graph",
    impact_decision: "Final guard decision",
    execution: "Typed executor result",
    decision_result: "Gateway response to the agent",
    agent_end: "Agent loop ended",
    completion_verified: "Observer verified completion; stopping agent",
    verification: "Independently checking the outcome",
    outcome: "Live run result",
    error: "Run stopped with an error",
    cleanup: "Demo lab context closed",
    stop_requested: "Stop requested",
    rejected_response: "Invalid model response rejected",
    adapter_error: "Adapter error",
    run_end: "Agent stopped",
    gateway: "Gateway observation received",
  };
  if (e.event === "request")
    return e.kind === "action"
      ? `Proposed ${e.request.verb} → ${e.request.name}`
      : `Requested ${e.request.tool}`;
  return names[e.event] || e.event.replaceAll("_", " ");
}
export const statusOf = (e?: Event) =>
  e?.status ||
  e?.decision?.decision ||
  (e?.event === "verification"
    ? e.complete
      ? "VERIFIED"
      : "WAITING"
    : "OBSERVED");

export function resolverPath(snapshot: any, resolution: any): string[] {
  if (!snapshot || !resolution?.target) return [];
  if (resolution.binding !== "endpoint_owner_chain")
    return [resolution.target.name, `UID ${resolution.target.uid}`];
  for (const slice of snapshot.endpointslices || []) {
    for (const endpoint of slice.endpoints || []) {
      if (endpoint.conditions?.ready !== true) continue;
      const pod = snapshot.pods.find(
        (p: any) => p.metadata.uid === endpoint.targetRef?.uid,
      );
      const rsRef = pod?.metadata.ownerReferences?.find(
        (r: any) => r.controller && r.kind === "ReplicaSet",
      );
      const rs = snapshot.replicasets.find(
        (r: any) => r.metadata.uid === rsRef?.uid,
      );
      const depRef = rs?.metadata.ownerReferences?.find(
        (r: any) => r.controller && r.kind === "Deployment",
      );
      if (depRef?.uid === resolution.target.uid)
        return [
          slice.metadata.labels["kubernetes.io/service-name"],
          slice.metadata.name,
          pod.metadata.name,
          rs.metadata.name,
          resolution.target.name,
        ];
    }
  }
  return [];
}
