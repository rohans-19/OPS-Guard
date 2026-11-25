import { describe, expect, it } from "vitest";
import { isDrafted, markEdited } from "./ScopeCard";

const scope: any = {
  operation: "scale",
  target: { kind_hint: "Service", name: "checkout" },
  bounds: { replicas: { min: 4, max: 4 } },
  protected: [],
  envelope: { max_services_affected: 1 },
  risk_class: "low",
  approval_mode: "allow_inside_envelope",
  expires_minutes: 15,
  summary: "Scale Service checkout to 4 replicas.",
  drafted_fields: ["summary", "bounds.replicas"],
};

describe("scope card", () => {
  it("marks fields the drafter filled from the request text", () => {
    expect(isDrafted(scope, "bounds.replicas")).toBe(true);
    expect(isDrafted(scope, "target")).toBe(false);
    expect(isDrafted(null, "summary")).toBe(false);
  });

  it("clears the drafted mark once the operator edits the field", () => {
    const edited = markEdited(scope, "summary", { summary: "My own words." });
    expect(edited.summary).toBe("My own words.");
    expect(edited.drafted_fields).toEqual(["bounds.replicas"]);
    expect(scope.drafted_fields).toEqual(["summary", "bounds.replicas"]);
  });
});
