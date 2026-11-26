"""Kubernetes Events as the fifth evidence channel.

``KubernetesEventsReader`` lists core/v1 Events about one involved object (the
task's diagnostic workload) and returns a bounded, untrusted excerpt with the
newest Event as its source reference, a timestamp and a SHA-256 digest.

``inject_event`` is the harness's helper: the fixture identity creates one Event
whose message is the payload. The agent can never create Events.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from opsguard.contracts import parse_json
from opsguard.intent.evidence import EvidenceRecord, EvidenceSource, record_evidence

MAX_LIST_BYTES = 65_536
MAX_EVENTS = 5


def _when(event: dict) -> str:
    return (event.get("lastTimestamp") or event.get("eventTime") or event.get("firstTimestamp")
            or event.get("metadata", {}).get("creationTimestamp") or "")


def format_events(events: list[dict]) -> str:
    """Newest first: 'Type Reason: message', one line each."""
    lines = []
    for event in events:
        message = event.get("message")
        if not isinstance(message, str):
            raise ValueError("an Event message must be text")
        lines.append(f"{event.get('type', 'Normal')} {event.get('reason', '')}: {message}")
    return "\n".join(lines)


class KubernetesEventsReader:
    """Read-only, scoped to one namespace and one involved object."""

    def __init__(self, namespace: str, client: httpx.AsyncClient, target: str, *, max_bytes: int = 2048):
        self.namespace, self.client, self.target, self.max_bytes = namespace, client, target, max_bytes

    async def read_events(self, request, *, timeout_seconds: float) -> EvidenceRecord:
        if (request.namespace, request.name) != (self.namespace, self.target):
            raise PermissionError("evidence outside registered workload")
        params = {"fieldSelector": f"involvedObject.name={self.target},involvedObject.namespace={self.namespace}",
                  "limit": 50}
        raw = bytearray()
        async with self.client.stream("GET", f"/api/v1/namespaces/{self.namespace}/events", params=params,
                                      timeout=timeout_seconds) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_LIST_BYTES:
                    raise ValueError("Event list exceeds limit")
        items = parse_json(raw.decode("utf-8"), max_bytes=MAX_LIST_BYTES)["items"]
        if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("metadata"), dict)
                or not isinstance(item["metadata"].get("name"), str) for item in items):
            raise ValueError("invalid Event list")
        newest = sorted(items, key=_when, reverse=True)[:MAX_EVENTS]
        source_event = newest[0]["metadata"] if newest else {"name": self.target, "uid": None}
        return record_evidence(
            channel="events", text=format_events(newest) if newest else "No events.",
            max_bytes=self.max_bytes,
            source=EvidenceSource(resource="event", namespace=self.namespace, name=source_event["name"],
                                  uid=source_event.get("uid"), deployment=self.target))


def fixture_event(namespace: str, target: str, payload: str, *, kind: str = "Deployment") -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"apiVersion": "v1", "kind": "Event",
            "metadata": {"generateName": f"{target}-fixture-", "namespace": namespace},
            "involvedObject": {"apiVersion": "apps/v1" if kind == "Deployment" else "v1", "kind": kind,
                               "name": target, "namespace": namespace},
            "reason": "FixtureNote", "message": payload, "type": "Normal",
            "source": {"component": "opsguard-fixture"}, "firstTimestamp": now, "lastTimestamp": now, "count": 1}


async def inject_event(client: httpx.AsyncClient, namespace: str, target: str, payload: str, *,
                       timeout_seconds: float = 10) -> dict:
    """Create the payload Event with the fixture identity and return it as stored."""
    response = await client.post(f"/api/v1/namespaces/{namespace}/events",
                                 json=fixture_event(namespace, target, payload), timeout=timeout_seconds)
    response.raise_for_status()
    stored = response.json()
    if stored.get("message") != payload:
        raise ValueError("the API server changed the payload")
    return stored


def memory_events_transport(namespace: str, target: str, payload: str) -> httpx.MockTransport:
    """An in-process events API that holds one fixture Event, for the memory backend."""
    event = fixture_event(namespace, target, payload)
    event["metadata"] = {"name": f"{target}-fixture-memory", "namespace": namespace, "uid": "memory-event-uid"}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != f"/api/v1/namespaces/{namespace}/events":
            return httpx.Response(404)
        return httpx.Response(200, json={"kind": "EventList", "items": [event]})

    return httpx.MockTransport(handle)
