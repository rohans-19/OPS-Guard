# OpsGuard policy language

OpsGuard rules are written in a small subset of [CEL](https://github.com/google/cel-spec), the language Kubernetes ValidatingAdmissionPolicy uses. `opsguard.policy.compile.compile_policy(intent)` turns one signed `IntentSpecV2` into:

1. **Gateway rules.** `GatewayRules.check()` evaluates each rule in Python (`opsguard.policy.cel`) on the server dry-run object, before any write.
2. **A ValidatingAdmissionPolicy and its binding.** The API server evaluates the same expression strings, so a write that bypasses the gateway is still refused.

Both enforcement points read the same text. The gateway's interpreter accepts only the subset below and treats anything else as a violation. Every compiled rule is parsed at compile time, so a rule outside the subset never ships.

## Variables

| Variable | Meaning |
| --- | --- |
| `object` | The object as it would be stored after the request. In the gateway this is the server dry-run response; for the scale subresource it is the `Scale` object |
| `oldObject` | The live object before the request (a `Scale` for the scale subresource). `null` on CREATE |
| `request.operation` | `CREATE`, `UPDATE` or `DELETE` |
| `request.resource.resource` | `deployments`, `services` or `configmaps` |
| `request.subResource` | `scale` for `deployments/scale`, otherwise `''` |
| `request.userInfo.username` | The caller. The policy's `matchConditions` constrain only `system:serviceaccount:<namespace>:agent` |

## Supported syntax

| Kind | Forms |
| --- | --- |
| Literals | integers, `'single'` or `"double"` quoted strings (escapes `\\ \' \" \n \t`), `true`, `false`, `null`, lists `[a, b]`, maps `{'k': v}` |
| Access | `a.b`, `a['key']`, `list[0]` |
| Functions | `has(a.b)` (field present and not null), `size(x)` or `x.size()` |
| Macros | `x.all(v, expr)`, `x.exists(v, expr)` over a list's elements or a map's keys |
| Operators | `!`, `&&`, `||`, `==`, `!=`, `<`, `<=`, `>`, `>=` (ints or strings), `in` (list element or map key), `cond ? a : b` |

Precedence from lowest to highest: `?:`, `||`, `&&`, relations, `!`, member access. Chained relations such as `a == b == c` need parentheses.

## Semantics that matter for policy

- **A missing field is an error, never `null`.** Guard optional fields with `has()`. An error makes the rule a violation. The admission policy uses `failurePolicy: Fail`, so the API server also refuses.
- **`&&` and `||` absorb an error when the other side decides.** For example, `false && error` is `false`.
- **`==` never equates different types.** `true == 1` is `false`. Lists and maps compare deeply.
- **Bounded evaluation.** Expressions over 4096 characters or 100,000 evaluation steps are errors.

## Compiled rules

Each rule is wrapped in a resource guard, so one list covers every matched resource:

```
!(request.resource.resource == 'deployments' && request.subResource == '') || (<check>)
```

| Rule | Check (example intent: scale 2–4 and set image `app` → `shop/app:v2` on checkout-api; payments-api protected) |
| --- | --- |
| `updates-only` | `request.operation == 'UPDATE'` |
| `target-deployment`, `target-service`, `target-configmap` | `object.metadata.name in ['checkout-api']` (an empty list when the intent has no verb for that kind) |
| `protected-<kind>` | `!(object.metadata.name in ['payments-api'])` |
| `replicas` | `object.spec.replicas >= 2 && object.spec.replicas <= 4`, or unchanged when `scale` is not allowed |
| `images` | `object.spec.template.spec.containers.all(c, oldObject.spec.template.spec.containers.exists(o, o.name == c.name && o.image == c.image) \|\| (c.name == 'app' && c.image in ['shop/app:v2']))` |
| `restart` | The `kubectl.kubernetes.io/restartedAt` annotation is unchanged (only when `restart` is not allowed) |
| `selector` | The Service selector equals the approved map or is unchanged; unchanged when `set_selector` is not allowed |
| `configmap-data` | Only the approved key changes, and only to an approved value; no key is removed. Unchanged when `set_config` is not allowed |

## Installation

`InstalledPolicy(compiled, admin_client)` installs the policy and binding at trial start. It removes them on exit or at the intent's expiry, whichever comes first. The binding goes first, so the policy never applies half-installed. The binding selects the intent's namespace through `kubernetes.io/metadata.name`. The policy's `matchConditions` limit it to the agent's ServiceAccount, so the fixture and observer identities are unaffected.

## Limits

- The gateway's interpreter is a subset of CEL, not all of it. It is enough for the compiled rules and is tested against the API server on kind (`tests/integration/test_kind_security.py`).
- Rules see one request at a time. The sequence of actions is judged by the episode state in the gateway (S2-P4), not by admission.
