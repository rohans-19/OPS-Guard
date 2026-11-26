"""The documented CEL subset: syntax, CEL-like semantics, and fail-closed errors."""

import pytest

from opsguard.policy.cel import CelError, evaluate, literal, parse, quote

ENV = {"object": {"metadata": {"name": "checkout-api"}, "spec": {"replicas": 3, "selector": {"app": "x"},
                  "template": {"spec": {"containers": [{"name": "app", "image": "a:1"}, {"name": "side", "image": "s:1"}]}}},
                  "data": {"mode": "on", "other": "1"}},
       "oldObject": {"spec": {"replicas": 1}, "data": None},
       "request": {"operation": "UPDATE", "subResource": ""}}


@pytest.mark.parametrize("expression,expected", [
    ("object.spec.replicas >= 2 && object.spec.replicas <= 4", True),
    ("object.metadata.name in ['payments-api']", False),
    ("!(object.metadata.name in ['payments-api'])", True),
    ("object.spec.selector == {'app': 'x'}", True),
    ("object.spec.selector == {'app': 'x', 'b': 'y'}", False),
    ("object.spec.template.spec.containers.all(c, c.image != 'evil')", True),
    ("object.spec.template.spec.containers.exists(c, c.name == 'side')", True),
    ("object.data.all(k, k == 'mode' ? object.data[k] in ['on'] : true)", True),
    ("'mode' in object.data && size(object.data) == 2 && object.data.size() == 2", True),
    ("has(object.data) && !has(oldObject.data)", True),  # A null field counts as absent.
    ("(has(oldObject.data) ? oldObject.data : {}) == {}", True),
    ("request.subResource == '' ? true : false", True),
    ("'it\\'s' == \"it's\"", True),
    ("object.spec.template.spec.containers[1].name == 'side'", True),
    ("true == 1", False),        # No cross-type equality.
    ("[1, 2] == [1, 2]", True),
    ("'a' < 'b'", True),
])
def test_supported_expressions(expression, expected):
    assert evaluate(expression, ENV) is expected


@pytest.mark.parametrize("expression,expected", [
    ("object.missing == 1 || true", True),
    ("true || object.missing == 1", True),
    ("object.missing == 1 && false", False),
    ("false && object.missing == 1", False),
])
def test_logical_operators_absorb_errors_when_the_other_side_decides(expression, expected):
    assert evaluate(expression, ENV) is expected


@pytest.mark.parametrize("expression", [
    "object.missing == 1",                 # missing field
    "object.data['absent'] == 'x'",        # missing key
    "object.spec.template.spec.containers[9].name == 'x'",
    "object.missing == 1 || false",        # error not absorbed
    "1 < 'a'",                             # mixed-type ordering
    "object.spec.replicas",                # not a bool
    "!object.spec.replicas",
    "unknown == 1",                        # undeclared variable
    "matches(object.metadata.name, 'x')",  # function outside the subset
    "object.metadata.name.startsWith('c')",
    "1 == 1 == true",                      # chained relation
    "object.spec.template.spec.containers.all(c, c.missing == 1)",
    "1 +",
    "'unterminated",
    "'bad \\q escape'",
    "x" * 5000,
])
def test_errors_and_unsupported_syntax_raise(expression):
    with pytest.raises(CelError):
        evaluate(expression, ENV)


def test_macro_variable_does_not_leak():
    env = {**ENV, "c": "outer"}
    assert evaluate("object.spec.template.spec.containers.all(c, has(c.name)) && c == 'outer'", env)


def test_evaluation_budget_is_bounded():
    big = {"object": {"items": list(range(400))}}
    with pytest.raises(CelError):
        evaluate("object.items.all(a, object.items.all(b, object.items.all(c, true)))", big)


def test_literals_round_trip_through_the_parser():
    value = {"app": "it's \"quoted\"\\", "n": 3, "flag": True, "list": ["a", 2]}
    assert evaluate(f"object == {literal(value)}", {"object": value})
    assert parse(quote("x\ny")) == ("lit", "x\ny")
