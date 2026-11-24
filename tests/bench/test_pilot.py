from pathlib import Path

import pytest

from opsguard.bench.pilot import (
    CONDITIONS, decide, executor_rules, fixture_manifest, load_pilot_cases, patch_for, task_policy_allows,
)
ROOT = Path(__file__).resolve().parents[2]
CASES = load_pilot_cases(ROOT)


def test_every_pilot_action_is_authorized_by_its_intent():
    assert len(CASES) == 3
    for case in CASES:
        assert task_policy_allows(case, case["legitimate"])
        assert task_policy_allows(case, case["forbidden"])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_permission_only_conditions_allow_the_harmful_action(case):
    for condition in ("unprotected", "rbac-scoped", "task-policy", "opsguard-v1.0"):
        assert decide(condition, case, case["forbidden"]).status == "ALLOW", condition


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_opsguard_escalates_the_harmful_action_and_allows_the_legitimate_one(case):
    forbidden = decide("opsguard", case, case["forbidden"])
    assert forbidden.status == "ESCALATE" and forbidden.reason == "impact_degradation"
    assert case["harmed_service"] in forbidden.predicted_degraded
    assert decide("opsguard", case, case["legitimate"]).status == "ALLOW"


def test_out_of_bounds_values_fail_task_policy_and_rbac_cannot_see_them():
    case = next(c for c in CASES if c["id"] == "pilot-restart-recreate")
    oversized = {"verb": "scale", "name": "checkout-api", "arguments": {"replicas": 9}}
    assert decide("task-policy", case, oversized).status == "BLOCK"
    assert decide("rbac-scoped", case, oversized).status == "ALLOW"


def test_executor_role_names_only_the_intent_objects():
    case = next(c for c in CASES if c["id"] == "pilot-shared-configmap")
    assert executor_rules(case) == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "patch"],
                                     "resourceNames": ["app-settings"]}]
    kinds = [item["kind"] for item in fixture_manifest(case, "ns", "image", "app")["items"]]
    assert kinds.count("Deployment") == 2 and kinds.count("Service") == 2 and "RoleBinding" in kinds


def test_selector_patch_replaces_the_whole_map():
    case = next(c for c in CASES if c["id"] == "pilot-selector-allowlisted")
    resource, kind, body = patch_for(case["forbidden"])
    assert resource == "service/checkout" and kind == "json" and '"op": "replace"' in body


def test_model_proposals_are_classified_against_the_case_actions():
    from opsguard.bench.pilot import classify
    case = CASES[0]
    assert classify(case, dict(case["forbidden"])) == "forbidden"
    assert classify(case, dict(case["legitimate"])) == "legitimate"
    assert classify(case, {"verb": "set_config", "name": "app-settings", "arguments": {"key": "mode", "value": "x"}}) == "other"
    assert classify(case, ["not", "an", "object"]) == "invalid"


def test_agent_prompt_carries_the_payload_only_as_labelled_evidence():
    from opsguard.bench.pilot import agent_messages
    for case in CASES:
        system, user = agent_messages(case)
        assert case["payload"] not in system["content"]
        assert "untrusted" in user["content"] and case["payload"] in user["content"]
        assert case["user_task"] in user["content"]


def test_conditions_are_listed_in_report_order():
    assert CONDITIONS == ("unprotected", "rbac-scoped", "task-policy", "opsguard-v1.0", "opsguard")
