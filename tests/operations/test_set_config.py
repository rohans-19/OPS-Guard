from dataclasses import FrozenInstanceError

import pytest

from opsguard.operations.set_config import prepare_set_config


LAB_VALUES = {("shop", "features", "checkout"): frozenset({"on", "off"})}


def action(**changes):
    return {
        "verb": "set_config",
        "resource": "configmap",
        "namespace": "shop",
        "name": "features",
        "arguments": {"key": "checkout", "value": "on"},
        **changes,
    }


def test_prepares_one_key_without_mutating_request_or_reusing_patch_dict():
    proposal = action()
    prepared = prepare_set_config(proposal, lab_values=LAB_VALUES)

    assert (prepared.namespace, prepared.name) == ("shop", "features")
    assert proposal == action()
    preview_body = prepared.patch_body()
    assert preview_body == {"data": {"checkout": "on"}}
    preview_body["data"]["checkout"] = "off"
    assert prepared.patch_body() == {"data": {"checkout": "on"}}
    with pytest.raises(FrozenInstanceError):
        prepared.value = "off"


def test_prepares_lab_value_even_when_only_another_value_fits_the_user_task():
    proposal = action(arguments={"key": "checkout", "value": "off"})

    prepared = prepare_set_config(proposal, lab_values=LAB_VALUES)

    assert prepared.patch_body() == {"data": {"checkout": "off"}}


@pytest.mark.parametrize(
    "proposal",
    [
        action(resource="secret"),
        action(namespace=""),
        action(name=42),
        action(dry_run=False),
        action(arguments={"key": "checkout", "value": True}),
        action(arguments={"key": "checkout"}),
        action(arguments={"key": "checkout", "value": "on", "patch": {}}),
        action(arguments={"data": {"checkout": "on", "payments": "off"}}),
    ],
)
def test_rejects_malformed_proposal(proposal):
    with pytest.raises(ValueError):
        prepare_set_config(proposal, lab_values=LAB_VALUES)


@pytest.mark.parametrize(
    "proposal",
    [
        action(namespace="elsewhere"),
        action(name="other-features"),
        action(arguments={"key": "payments", "value": "on"}),
        action(arguments={"key": "checkout", "value": "disabled"}),
    ],
)
def test_rejects_values_and_targets_outside_the_lab(proposal):
    with pytest.raises(ValueError, match="outside lab bounds"):
        prepare_set_config(proposal, lab_values=LAB_VALUES)
