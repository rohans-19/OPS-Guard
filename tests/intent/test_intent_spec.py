"""IntentSpec v2: strict schema, HMAC signing, expiry, and the v1 view."""

from datetime import datetime, timedelta, timezone

import pytest

from opsguard.intent.models import (ArgumentBounds, ConfigBounds, ImageBounds, IntentSpecV2, ProtectedRef,
                                    ReplicaBounds, TargetHint, load_intent_key, sign_intent, verify_intent)

KEY = b"lab-key-for-tests-only-0123456789abcdef"
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)


def spec(**changes):
    fields = dict(id="intent-0142", task_id="checkout-task", issued_at=NOW, expires_at=NOW + timedelta(minutes=15),
                  namespace="shop", target=TargetHint(kind_hint="Service", name="checkout"), verbs=("scale",),
                  bounds=ArgumentBounds(replicas=ReplicaBounds(min=2, max=4)),
                  protected=(ProtectedRef(name="payments-api"), ProtectedRef(namespace="monitoring", name="*")))
    fields.update(changes)
    return IntentSpecV2(**fields)


def test_signed_intent_verifies_until_it_expires():
    signed = sign_intent(spec(), KEY)
    assert verify_intent(signed, KEY, now=NOW + timedelta(minutes=1)).status == "VALID"
    assert verify_intent(signed, KEY, now=NOW + timedelta(minutes=15)).status == "INTENT_EXPIRED"


@pytest.mark.parametrize("field,value", [
    ("namespace", "payments"), ("bounds", ArgumentBounds(replicas=ReplicaBounds(min=2, max=40))),
    ("expires_at", NOW + timedelta(hours=2)), ("protected", ()),
])
def test_any_altered_field_invalidates_the_signature(field, value):
    signed = sign_intent(spec(), KEY)
    altered = signed.model_copy(update={field: value})
    result = verify_intent(altered, KEY, now=NOW)
    assert (result.status, result.reason) == ("INTENT_INVALID", "SIGNATURE_MISMATCH")


def test_wrong_key_missing_signature_and_future_issue_are_invalid():
    signed = sign_intent(spec(), KEY)
    assert verify_intent(signed, b"another-lab-key-0123456789abcdefgh", now=NOW).status == "INTENT_INVALID"
    assert verify_intent(spec(), KEY, now=NOW).reason == "SIGNATURE_MISMATCH"
    assert verify_intent(signed, KEY, now=NOW - timedelta(minutes=5)).reason == "ISSUED_IN_FUTURE"
    assert verify_intent(signed, KEY, now=NOW.replace(tzinfo=None)).status == "INTENT_INVALID"
    # An object that fails validation once revalidated is invalid, never an exception.
    broken = signed.model_copy(update={"verbs": ("scale", "scale")})
    assert verify_intent(broken, KEY, now=NOW).reason == "INTENT_MALFORMED"


def test_signature_survives_a_json_round_trip():
    signed = sign_intent(spec(), KEY)
    assert verify_intent(IntentSpecV2.model_validate_json(signed.model_dump_json()), KEY, now=NOW).status == "VALID"


def test_short_keys_are_refused():
    with pytest.raises(ValueError):
        sign_intent(spec(), b"short")


@pytest.mark.parametrize("changes", [
    {"extra_field": True},
    {"verbs": ("scale", "set_image")},                                   # image bounds missing
    {"bounds": ArgumentBounds()},                                        # replica bounds missing
    {"expires_at": NOW},                                                 # expires at issue time
    {"expires_at": NOW + timedelta(days=2)},                             # over the lab lifetime
    {"protected": (ProtectedRef(name="*"),)},                            # wildcard over own namespace
    {"protected": (ProtectedRef(name="checkout"),)},                     # target is protected
    {"issued_at": "2026-11-25T10:00:00"},                                # timezone missing
])
def test_schema_rejects_inconsistent_intents(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def test_v1_view_keeps_existing_policy_code_working():
    task = spec(verbs=("scale", "set_image", "restart"),
                bounds=ArgumentBounds(replicas=ReplicaBounds(min=2, max=4),
                                      image=ImageBounds(container="app", allowed=("shop/app:v2",)))).to_task_intent()
    assert task.target.resource == "service" and task.allow_selector_fallback is True
    assert [p.verb for p in task.permissions] == ["scale", "set_image", "restart"]
    assert task.protected_deployments == ("payments-api",) and task.protected_services == ("payments-api",)
    bare = spec(target=TargetHint(name="checkout")).to_task_intent()
    assert bare.target.resource == "any"
    config = spec(target=TargetHint(kind_hint="ConfigMap", name="app-settings"), verbs=("set_config",),
                  bounds=ArgumentBounds(config=ConfigBounds(key="mode", values=("enabled",),
                                                            diagnostic_deployment="checkout"))).to_task_intent()
    assert config.target.resource == "configmap" and config.permissions[0].values == ("enabled",)


def test_lab_key_comes_from_the_environment_or_an_ignored_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSGUARD_INTENT_KEY", raising=False)
    env = tmp_path / ".env"
    with pytest.raises(ValueError):
        load_intent_key(env)
    env.write_text("OTHER=1\nOPSGUARD_INTENT_KEY='file-key-0123456789abcdef0123456789'\n", encoding="utf-8")
    assert load_intent_key(env) == b"file-key-0123456789abcdef0123456789"
    monkeypatch.setenv("OPSGUARD_INTENT_KEY", "env-key-0123456789abcdef0123456789ab")
    assert load_intent_key(env) == b"env-key-0123456789abcdef0123456789ab"
    monkeypatch.setenv("OPSGUARD_INTENT_KEY", "too-short")
    with pytest.raises(ValueError):
        load_intent_key(env)
