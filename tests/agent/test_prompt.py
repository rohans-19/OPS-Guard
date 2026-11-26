"""The agent prompt shows response shapes only: no real names, and every registered tool."""

import json
import re
import typing

import pytest
from pydantic import ValidationError

from opsguard.agent.interfaces import Step
from opsguard.agent.loop import BASE_PROMPT, HARDENING
from opsguard.contracts import ReadRequest, Verb

EXAMPLES = [line for line in BASE_PROMPT.splitlines() if line.startswith('{"kind"')]


def _tools() -> set[str]:
    members = typing.get_args(typing.get_args(ReadRequest)[0])
    return {typing.get_args(model.model_fields["tool"].annotation)[0] for model in members}


def test_examples_use_placeholders_not_real_namespaces_or_names():
    for line in EXAMPLES:
        step = json.loads(line.replace("<count>", "1"))
        request = step.get("request", {})
        if "namespace" in request:
            assert request["namespace"] == "<namespace>", line
            assert re.fullmatch(r"<[a-z-]+>", request["name"]), line
    assert "shop" not in BASE_PROMPT + HARDENING


def test_every_registered_read_tool_and_verb_has_an_example():
    shown_tools = {json.loads(line)["request"]["tool"] for line in EXAMPLES if '"kind":"read"' in line}
    shown_verbs = {json.loads(line.replace("<count>", "1"))["request"]["verb"] for line in EXAMPLES
                   if '"kind":"action"' in line}
    assert shown_tools == _tools()
    assert shown_verbs == set(typing.get_args(Verb))


@pytest.mark.parametrize("line", [line for line in EXAMPLES if '"kind":"finish"' not in line])
def test_a_copied_placeholder_is_rejected_before_the_gateway(line):
    with pytest.raises(ValidationError):
        Step.validate_json(line.replace("<count>", "1"))


def test_the_task_and_observations_are_named_as_the_source_of_names():
    assert "Take the namespace and object names from the operator task" in BASE_PROMPT
    assert "never use a namespace or name that is not in the task or an observation" in BASE_PROMPT
