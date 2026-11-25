"""Scope card: rule-based draft, operator confirmation, signing at run start."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsguard.demo.api import create_app
from opsguard.demo.runtime import LiveRun, check_demo_scope, demo_draft
from opsguard.intent.models import verify_intent
from opsguard.intent.scope import ConfirmedScope, issue_intent

KEY = b"scope-test-key-0123456789abcdefghijkl"


def test_draft_marks_fields_taken_from_the_request_text():
    plain = demo_draft("scale", 3, "Scale the application behind checkout to 3 replicas.")
    assert plain.target.name == "checkout" and plain.drafted_fields == ("summary",)
    edited = demo_draft("scale", 3, "Please scale checkout-api to 4 replicas")
    assert edited.bounds.replicas.min == edited.bounds.replicas.max == 4
    assert "bounds.replicas" in edited.drafted_fields and edited.summary.startswith("Scale")


def test_draft_never_targets_a_protected_object_named_in_the_request():
    scope = demo_draft("restart", 1, "Restart payments-api, not checkout")
    assert scope.target.name == "checkout" and "target" not in scope.drafted_fields


def test_confirmed_scope_signs_into_a_verifiable_intent():
    scope = demo_draft("set_image", 1, "Update checkout to the approved v2 image.")
    now = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    signed = issue_intent(scope, namespace="demo-abc", task_id="demo-abc", key=KEY, now=now)
    assert verify_intent(signed, KEY, now=now + timedelta(minutes=1)).status == "VALID"
    assert signed.expires_at - signed.issued_at == timedelta(minutes=15)
    assert signed.to_task_intent().protected_deployments == ("payments-api",)


@pytest.mark.parametrize("change", [
    lambda s: s.model_copy(update={"protected": ()}),                                          # protection removed
    lambda s: s.model_copy(update={"target": s.target.model_copy(update={"name": "billing"})}),  # unknown object
    lambda s: s.model_copy(update={"bounds": demo_draft("scale", 5, "x").bounds.model_copy(
        update={"replicas": s.bounds.replicas.model_copy(update={"max": 9})})}),                # replicas over 5
])
def test_demo_refuses_scopes_it_cannot_enforce(change):
    with pytest.raises(ValueError):
        check_demo_scope(change(demo_draft("scale", 3, "Scale checkout to 3 replicas")))


def test_image_bounds_are_fixed_in_the_demo():
    scope = demo_draft("set_image", 1, "Update checkout")
    widened = scope.model_copy(update={"bounds": scope.bounds.model_copy(update={
        "image": scope.bounds.image.model_copy(update={"allowed": ("evil/app:v1",)})})})
    with pytest.raises(ValueError):
        check_demo_scope(widened)


def test_api_drafts_confirms_and_binds_the_scope_to_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr("opsguard.demo.api.groq_key", lambda *_: "test-key")
    seen = {}

    async def fake_run(self):
        seen["scope"], seen["key"] = self.scope, self.intent_key
        self.emit({"event": "setup"})
        self.status = "completed"
    monkeypatch.setattr(LiveRun, "run", fake_run)
    with TestClient(create_app(tmp_path, "test")) as client:
        token = client.get("/api/session").json()["token"]
        headers = {"X-Demo-Token": token}
        assert client.post("/api/scopes/draft", json={"prompt": "Scale checkout"}).status_code == 403
        draft = client.post("/api/scopes/draft", json={"operation": "scale", "replicas": 2,
                                                       "prompt": "Scale checkout to 2 replicas"}, headers=headers)
        scope = draft.json()["scope"]
        assert draft.status_code == 200 and scope["bounds"]["replicas"] == {"min": 2, "max": 2}
        unsafe = {**scope, "protected": []}
        assert client.post("/api/scopes", json=unsafe, headers=headers).status_code == 422
        assert client.post("/api/scopes", json={**scope, "authorized": True}, headers=headers).status_code == 422
        confirmed = client.post("/api/scopes", json=scope, headers=headers)
        scope_id = confirmed.json()["scope_id"]
        assert confirmed.status_code == 201 and scope_id.startswith("scope-")
        run = {"prompt": "Scale checkout to 2 replicas", "replicas": 2, "scope_id": scope_id}
        wrong = client.post("/api/runs", json={**run, "operation": "restart"},
                            headers={**headers, "Idempotency-Key": "a"})
        assert wrong.status_code == 409
        missing = client.post("/api/runs", json={**run, "scope_id": "scope-000000000000"},
                              headers={**headers, "Idempotency-Key": "b"})
        assert missing.status_code == 404
        started = client.post("/api/runs", json=run, headers={**headers, "Idempotency-Key": "c"})
        assert started.status_code == 202
        assert isinstance(seen["scope"], ConfirmedScope) and len(seen["key"]) >= 32
        checks = client.get("/api/readiness").json()["checks"]
        assert checks["intent_key"]["detail"] == "Ephemeral lab key for this server process"
