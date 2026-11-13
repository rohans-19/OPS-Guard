import { describe, it, expect } from "vitest";
import {
  mergeEvents,
  latest,
  describe as label,
  Event,
  resolverPath,
} from "./events";
const event = (seq: number, name = "request") =>
  ({ seq, event: name, timestamp: "", elapsed_ms: 0 }) as Event;
describe("live event recovery", () => {
  it("deduplicates reconnect deliveries and preserves backend order", () => {
    const merged = mergeEvents(
      [event(2), event(1)],
      [event(2), event(4), event(3)],
    );
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });
  it("selects the newest result rather than an earlier blocked proposal", () => {
    expect(
      latest(
        [event(1, "policy"), event(2, "evidence"), event(3, "policy")],
        "policy",
      )?.seq,
    ).toBe(3);
  });
  it("labels an actual proposal without inferring execution", () => {
    expect(
      label({
        ...event(1),
        kind: "action",
        request: { verb: "scale", name: "payments-api" },
      }),
    ).toBe("Proposed scale → payments-api");
  });
  it("draws only a verified endpoint ownership chain for the resolved UID", () => {
    const snapshot = {
      endpointslices: [
        {
          metadata: {
            name: "endpoints",
            labels: { "kubernetes.io/service-name": "checkout" },
          },
          endpoints: [
            { conditions: { ready: true }, targetRef: { uid: "pod-uid" } },
          ],
        },
      ],
      pods: [
        {
          metadata: {
            name: "pod",
            uid: "pod-uid",
            ownerReferences: [
              { kind: "ReplicaSet", controller: true, uid: "rs-uid" },
            ],
          },
        },
      ],
      replicasets: [
        {
          metadata: {
            name: "replicaset",
            uid: "rs-uid",
            ownerReferences: [
              { kind: "Deployment", controller: true, uid: "deployment-uid" },
            ],
          },
        },
      ],
    };
    const resolution = {
      binding: "endpoint_owner_chain",
      target: { name: "checkout-api", uid: "deployment-uid" },
    };
    expect(resolverPath(snapshot, resolution)).toEqual([
      "checkout",
      "endpoints",
      "pod",
      "replicaset",
      "checkout-api",
    ]);
    expect(
      resolverPath(snapshot, {
        ...resolution,
        target: { name: "decoy", uid: "other-uid" },
      }),
    ).toEqual([]);
  });
});
