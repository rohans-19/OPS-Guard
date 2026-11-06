"""Prepare a bounded ConfigMap update for the shared executor."""

from collections.abc import Mapping, Set
from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedConfigMutation:
    namespace: str
    name: str
    key: str
    value: str

    def patch_body(self) -> dict[str, dict[str, str]]:
        """Return the single-key JSON merge patch for preview and execution."""
        return {"data": {self.key: self.value}}


def prepare_set_config(
    action: Mapping[str, object],
    *,
    lab_values: Mapping[tuple[str, str, str], Set[str]],
) -> PreparedConfigMutation:
    """Validate a proposal against lab bounds shared by all defences."""
    if not isinstance(action, Mapping) or action.keys() != {
        "verb", "resource", "namespace", "name", "arguments"
    }:
        raise ValueError("set_config requires the typed action envelope")
    if action["verb"] != "set_config" or action["resource"] != "configmap":
        raise ValueError("set_config only supports the configmap resource")

    namespace, name = action["namespace"], action["name"]
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("set_config requires a nonempty namespace string")
    if not isinstance(name, str) or not name:
        raise ValueError("set_config requires a nonempty ConfigMap name string")

    arguments = action["arguments"]
    if not isinstance(arguments, Mapping) or arguments.keys() != {"key", "value"}:
        raise ValueError("set_config requires exactly one key and one value")
    key, value = arguments["key"], arguments["value"]
    if not isinstance(key, str) or not key or not isinstance(value, str):
        raise ValueError("set_config requires a nonempty key and a string value")

    allowed_values = lab_values.get((namespace, name, key))
    if not isinstance(allowed_values, Set) or value not in allowed_values:
        raise ValueError("set_config target, key, or value is outside lab bounds")

    return PreparedConfigMutation(namespace=namespace, name=name, key=key, value=value)
