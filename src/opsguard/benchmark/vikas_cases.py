"""Load Vikas's ConfigMap cases and check their observed outcomes."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Union


def load_cases(path: Union[str, Path]) -> list[dict]:
    """Load the trusted case corpus with one entry per case ID."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("case corpus must contain a list of cases")

    seen = set()
    for case in cases:
        case_id = case.get("id") if isinstance(case, dict) else None
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("every case must have a nonempty ID")
        if case_id in seen:
            raise ValueError(f"duplicate case ID: {case_id}")
        seen.add(case_id)
    return cases


def namespaced_action(outcome: Mapping[str, Any], namespace: str) -> dict:
    """Bind a fixture proposal to the namespace allocated for the replay."""
    action = outcome["action"]
    return {**action, "namespace": namespace, "arguments": dict(action["arguments"])}


def expected_state_met(
    expected: Mapping[str, Any],
    *,
    namespace: str,
    read_configmap: Callable[[str, str], Mapping[str, str]],
    probe_http: Callable[[str, str, str], int],
) -> bool:
    """Check one stored value and its HTTP observations via independent callbacks.

    Probe callbacks return the actual response status, including non-2xx responses.
    Read and probe errors propagate so the runner can count harness errors separately.
    """
    data = read_configmap(namespace, expected["configmap"])
    if data.get(expected["key"]) != expected["value"]:
        return False
    return all(
        probe_http(namespace, probe["service"], probe["path"]) == probe["status"]
        for probe in expected["probes"]
    )
