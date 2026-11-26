"""A small, documented subset of CEL, evaluated in the gateway.

The same expression strings are installed in a ValidatingAdmissionPolicy, where
the API server evaluates them with full CEL. This interpreter supports only the
subset in documentations/POLICY_LANGUAGE.md and raises ``CelError`` for anything
else, so a rule outside the subset fails closed in the gateway.

Semantics follow CEL where it matters for policy:
* a missing field or key is an error, never ``null``; guard with ``has()``;
* ``&&`` and ``||`` absorb an error when the other side decides the result;
* ``==`` never equates values of different types (``true != 1``);
* an error in a rule is a violation (the admission policy uses failurePolicy: Fail).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EXPRESSION = 4096
MAX_STEPS = 100_000


class CelError(Exception):
    pass


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<int>\d+)
  | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<op>\|\||&&|==|!=|<=|>=|[<>!?:.,()\[\]{}])
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)
_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t"}


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: object


def _tokens(text: str) -> list[_Tok]:
    if len(text) > MAX_EXPRESSION:
        raise CelError("expression too long")
    out, pos = [], 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise CelError(f"unexpected character at {pos}")
        pos = match.end()
        kind = match.lastgroup
        raw = match.group()
        if kind == "ws":
            continue
        if kind == "int":
            out.append(_Tok("lit", int(raw)))
        elif kind == "str":
            body, chars, i = raw[1:-1], [], 0
            while i < len(body):
                if body[i] == "\\":
                    if i + 1 >= len(body) or body[i + 1] not in _ESCAPES:
                        raise CelError("unsupported string escape")
                    chars.append(_ESCAPES[body[i + 1]])
                    i += 2
                else:
                    chars.append(body[i])
                    i += 1
            out.append(_Tok("lit", "".join(chars)))
        elif kind == "id" and raw in ("true", "false", "null"):
            out.append(_Tok("lit", {"true": True, "false": False, "null": None}[raw]))
        elif kind == "id" and raw == "in":
            out.append(_Tok("op", "in"))
        else:
            out.append(_Tok(kind if kind == "id" else "op", raw))
    out.append(_Tok("end", None))
    return out


# ---------------------------------------------------------------------------
# Parser: ternary < || < && < relations < unary < member/call/index < primary
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, text: str):
        self.toks, self.i = _tokens(text), 0

    def peek(self, value=None):
        tok = self.toks[self.i]
        return tok if value is None or (tok.kind == "op" and tok.value == value) else None

    def take(self, value=None) -> _Tok:
        tok = self.toks[self.i]
        if value is not None and not (tok.kind == "op" and tok.value == value):
            raise CelError(f"expected {value!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.ternary()
        if self.peek().kind != "end":
            raise CelError("unexpected trailing input")
        return node

    def ternary(self):
        cond = self.logic_or()
        if self.peek("?"):
            self.take("?")
            yes = self.ternary()
            self.take(":")
            return ("?", cond, yes, self.ternary())
        return cond

    def logic_or(self):
        node = self.logic_and()
        while self.peek("||"):
            self.take()
            node = ("||", node, self.logic_and())
        return node

    def logic_and(self):
        node = self.relation()
        while self.peek("&&"):
            self.take()
            node = ("&&", node, self.relation())
        return node

    def relation(self):
        node = self.unary()
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("==", "!=", "<", "<=", ">", ">=", "in"):
            self.take()
            node = (tok.value, node, self.unary())
            if self.peek().kind == "op" and self.peek().value in ("==", "!=", "<", "<=", ">", ">=", "in"):
                raise CelError("chained relations need parentheses")
        return node

    def unary(self):
        if self.peek("!"):
            self.take()
            return ("!", self.unary())
        return self.member()

    def member(self):
        node = self.primary()
        while True:
            if self.peek("."):
                self.take()
                name = self.take()
                if name.kind != "id":
                    raise CelError("expected a field name")
                if self.peek("("):
                    node = self.method(node, name.value)
                else:
                    node = ("field", node, name.value)
            elif self.peek("["):
                self.take()
                index = self.ternary()
                self.take("]")
                node = ("index", node, index)
            else:
                return node

    def method(self, target, name):
        self.take("(")
        if name in ("all", "exists"):
            var = self.take()
            if var.kind != "id":
                raise CelError("a macro needs a variable name")
            self.take(",")
            body = self.ternary()
            self.take(")")
            return (name, target, var.value, body)
        if name == "size":
            self.take(")")
            return ("size", target)
        raise CelError(f"unsupported method {name}")

    def primary(self):
        tok = self.take()
        if tok.kind == "lit":
            return ("lit", tok.value)
        if tok.kind == "id":
            if self.peek("("):
                self.take("(")
                if tok.value == "has":
                    arg = self.member()
                    self.take(")")
                    if arg[0] != "field":
                        raise CelError("has() needs a field selection")
                    return ("has", arg[1], arg[2])
                if tok.value == "size":
                    arg = self.ternary()
                    self.take(")")
                    return ("size", arg)
                raise CelError(f"unsupported function {tok.value}")
            return ("var", tok.value)
        if tok.kind == "op" and tok.value == "(":
            node = self.ternary()
            self.take(")")
            return node
        if tok.kind == "op" and tok.value == "[":
            items = []
            while not self.peek("]"):
                items.append(self.ternary())
                if not self.peek("]"):
                    self.take(",")
            self.take("]")
            return ("list", items)
        if tok.kind == "op" and tok.value == "{":
            pairs = []
            while not self.peek("}"):
                key = self.ternary()
                self.take(":")
                pairs.append((key, self.ternary()))
                if not self.peek("}"):
                    self.take(",")
            self.take("}")
            return ("map", pairs)
        raise CelError("unexpected token")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _type(value):
    return "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else type(value).__name__


def _eq(a, b) -> bool:
    if _type(a) != _type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    return a == b


class _Eval:
    def __init__(self, env: dict):
        self.env, self.steps = dict(env), 0

    def run(self, node):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise CelError("evaluation budget exceeded")
        op = node[0]
        if op == "lit":
            return node[1]
        if op == "var":
            if node[1] not in self.env:
                raise CelError(f"undeclared variable {node[1]}")
            return self.env[node[1]]
        if op == "list":
            return [self.run(item) for item in node[1]]
        if op == "map":
            out = {}
            for key, value in node[1]:
                k = self.run(key)
                if not isinstance(k, (str, int)) or isinstance(k, bool):
                    raise CelError("map keys must be strings or ints")
                out[k] = self.run(value)
            return out
        if op == "field":
            base = self.run(node[1])
            if not isinstance(base, dict) or node[2] not in base:
                raise CelError(f"no such key: {node[2]}")
            return base[node[2]]
        if op == "has":
            base = self.run(node[1])
            if not isinstance(base, dict):
                raise CelError("has() on a non-object")
            return node[2] in base and base[node[2]] is not None
        if op == "index":
            base, key = self.run(node[1]), self.run(node[2])
            if isinstance(base, dict) and isinstance(key, (str, int)) and key in base:
                return base[key]
            if isinstance(base, list) and _type(key) == "int" and 0 <= key < len(base):
                return base[key]
            raise CelError("index out of range or no such key")
        if op == "size":
            value = self.run(node[1])
            if not isinstance(value, (str, list, dict)):
                raise CelError("size() of an unsupported value")
            return len(value)
        if op == "!":
            value = self.run(node[1])
            if not isinstance(value, bool):
                raise CelError("! needs a bool")
            return not value
        if op in ("&&", "||"):
            decisive = op == "||"  # true decides ||, false decides &&
            try:
                left = self.run(node[1])
                if not isinstance(left, bool):
                    raise CelError(f"{op} needs bools")
            except CelError as error:
                right = self.run(node[2])
                if right is decisive:
                    return right
                raise error
            if left is decisive:
                return left
            right = self.run(node[2])
            if not isinstance(right, bool):
                raise CelError(f"{op} needs bools")
            return right
        if op == "?":
            cond = self.run(node[1])
            if not isinstance(cond, bool):
                raise CelError("?: needs a bool condition")
            return self.run(node[2] if cond else node[3])
        if op in ("all", "exists"):
            target = self.run(node[1])
            items = list(target) if isinstance(target, (list, dict)) else None
            if items is None:
                raise CelError(f"{op}() needs a list or map")
            outer = self.env.get(node[2], _MISSING)
            results, failure = [], None
            try:
                for item in items:
                    self.env[node[2]] = item
                    try:
                        value = self.run(node[3])
                        if not isinstance(value, bool):
                            raise CelError(f"{op}() body must be a bool")
                        results.append(value)
                    except CelError as error:
                        failure = failure or error
            finally:
                if outer is _MISSING:
                    self.env.pop(node[2], None)
                else:
                    self.env[node[2]] = outer
            if op == "all":
                if False in results:
                    return False
            elif True in results:
                return True
            if failure:
                raise failure
            return op == "all"
        left, right = self.run(node[1]), self.run(node[2])
        if op == "==":
            return _eq(left, right)
        if op == "!=":
            return not _eq(left, right)
        if op == "in":
            if isinstance(right, list):
                return any(_eq(left, item) for item in right)
            if isinstance(right, dict):
                return any(_eq(left, key) for key in right)
            raise CelError("in needs a list or map")
        if _type(left) != _type(right) or _type(left) not in ("int", "str"):
            raise CelError(f"{op} needs two ints or two strings")
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    # (unreachable)


_MISSING = object()


def parse(expression: str):
    """Parse once; raises CelError for anything outside the subset."""
    return _Parser(expression).parse()


def evaluate(expression: str, env: dict) -> bool:
    """Evaluate a rule to a bool. Any error, or a non-bool result, raises CelError."""
    result = _Eval(env).run(parse(expression))
    if not isinstance(result, bool):
        raise CelError("a rule must evaluate to a bool")
    return result


def quote(value: str) -> str:
    """A CEL single-quoted string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\t", "\\t") + "'"


def literal(value) -> str:
    """A CEL literal for a string, int, bool, list or string-keyed map."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{quote(k)}: {literal(v)}" for k, v in sorted(value.items())) + "}"
    raise CelError(f"no CEL literal for {type(value).__name__}")
