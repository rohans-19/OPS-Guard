"""Trusted topology: application-level facts the Kubernetes API cannot show.

Which workload calls which Service, which workloads read a setting and which
values break them, how each one picks up a change, and which Services are
user-facing. These come only from a file written by the fixture authors and
reviewed with the fixture, never from evidence the agent reads (rule 1).

Names are relative to the trial namespace, because every trial runs in a
fresh namespace. File format (JSON)::

    {
      "schema": "opsguard-topology/v1",
      "name": "pilot-checkout-payments",
      "calls": [{"caller": "payments-api", "service": "checkout", "dependency": "hard"}],
      "settings": [{"configmap": "app-settings", "key": "mode", "consumer": "payments-api",
                    "healthy": ["legacy", "enabled"], "degraded": ["disabled"],
                    "reload": "live", "symptom": "health"}],
      "user_facing": ["checkout"],
      "data_stores": [{"workload": "db", "claims": ["db-data"]}]
    }

``reload`` is ``live`` (the app re-reads the mounted file) or ``on_restart``
(environment variables, or apps that read once at start). ``symptom`` is how a
degraded value shows: ``health`` (Pods stay ready but answer errors),
``readiness`` (Pods leave the endpoints) or ``crash`` (CrashLoopBackOff).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SCHEMA = "opsguard-topology/v1"
Reload = Literal["live", "on_restart"]
Symptom = Literal["health", "readiness", "crash"]


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        raise ValueError(f"{what} must be a non-empty object name")
    return value


@dataclass(frozen=True)
class Call:
    caller: str             # workload name
    service: str            # Service name it calls
    dependency: Literal["hard", "soft"] = "hard"


@dataclass(frozen=True)
class Setting:
    configmap: str
    key: str
    consumer: str           # workload name
    healthy: frozenset[str] = frozenset()
    degraded: frozenset[str] = frozenset()
    reload: Reload = "live"
    symptom: Symptom = "health"

    def __post_init__(self) -> None:
        if self.healthy & self.degraded:
            raise ValueError(f"{self.configmap}/{self.key}: a value cannot be both healthy and degraded")
        if self.reload not in ("live", "on_restart") or self.symptom not in ("health", "readiness", "crash"):
            raise ValueError(f"{self.configmap}/{self.key}: unknown reload or symptom")


@dataclass(frozen=True)
class DataStore:
    workload: str
    claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustedTopology:
    name: str = "empty"
    calls: tuple[Call, ...] = ()
    settings: tuple[Setting, ...] = ()
    user_facing: frozenset[str] = frozenset()
    data_stores: tuple[DataStore, ...] = ()
    source: str = field(default="", compare=False)

    def settings_for(self, configmap: str, key: str | None = None) -> tuple[Setting, ...]:
        return tuple(s for s in self.settings if s.configmap == configmap and (key is None or s.key == key))

    def callers_of(self, service: str) -> tuple[Call, ...]:
        return tuple(c for c in self.calls if c.service == service)

    def claims(self) -> frozenset[str]:
        return frozenset(claim for store in self.data_stores for claim in store.claims)

    def stateful_workloads(self) -> frozenset[str]:
        return frozenset(store.workload for store in self.data_stores)


def parse_topology(data: dict, *, source: str = "") -> TrustedTopology:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"a topology file needs schema {SCHEMA!r}")
    unknown = set(data) - {"schema", "name", "description", "calls", "settings", "user_facing", "data_stores"}
    if unknown:
        raise ValueError(f"unknown topology fields: {sorted(unknown)}")
    calls = tuple(
        Call(_name(c.get("caller"), "call.caller"), _name(c.get("service"), "call.service"),
             c.get("dependency", "hard"))
        for c in data.get("calls", ()))
    if any(c.dependency not in ("hard", "soft") for c in calls):
        raise ValueError("call.dependency must be hard or soft")
    settings = []
    for s in data.get("settings", ()):
        extra = set(s) - {"configmap", "key", "consumer", "healthy", "degraded", "reload", "symptom"}
        if extra:
            raise ValueError(f"unknown setting fields: {sorted(extra)}")
        values = [v for v in (*s.get("healthy", ()), *s.get("degraded", ())) if not isinstance(v, str)]
        if values:
            raise ValueError("setting values must be strings")
        settings.append(Setting(_name(s.get("configmap"), "setting.configmap"), _name(s.get("key"), "setting.key"),
                                _name(s.get("consumer"), "setting.consumer"), frozenset(s.get("healthy", ())),
                                frozenset(s.get("degraded", ())), s.get("reload", "live"), s.get("symptom", "health")))
    stores = tuple(DataStore(_name(d.get("workload"), "data_store.workload"),
                             tuple(_name(c, "data_store.claim") for c in d.get("claims", ())))
                   for d in data.get("data_stores", ()))
    return TrustedTopology(
        name=_name(data.get("name", "unnamed"), "name"), calls=calls, settings=tuple(settings),
        user_facing=frozenset(_name(n, "user_facing") for n in data.get("user_facing", ())),
        data_stores=stores, source=source)


def load_topology(path: Path | str) -> TrustedTopology:
    path = Path(path)
    return parse_topology(json.loads(path.read_text(encoding="utf-8")), source=str(path))


def from_config_effects(effects: Iterable, *, name: str = "config-effects") -> TrustedTopology:
    """Adapt the v0 ``ConfigEffect`` rules so callers can move to the effect model unchanged."""
    settings = []
    for effect in effects:
        for consumer in effect.consumers:
            settings.append(Setting(effect.configmap.name, effect.key, consumer.name,
                                    frozenset(effect.healthy_values), frozenset(effect.degraded_values)))
    return TrustedTopology(name=name, settings=tuple(settings))


def merge(*topologies: TrustedTopology) -> TrustedTopology:
    return TrustedTopology(
        name="+".join(t.name for t in topologies),
        calls=tuple(c for t in topologies for c in t.calls),
        settings=tuple(s for t in topologies for s in t.settings),
        user_facing=frozenset(n for t in topologies for n in t.user_facing),
        data_stores=tuple(d for t in topologies for d in t.data_stores))
