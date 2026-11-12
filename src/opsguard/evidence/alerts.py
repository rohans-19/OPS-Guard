"""Bounded, read-only local Alertmanager evidence; annotations never grant authority."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import ipaddress
import json

import httpx

from opsguard.contracts import AlertsRead, parse_json
from opsguard.intent.evidence import EvidenceSource, record_evidence


def local_alertmanager_url(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    if not address.is_loopback or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Alertmanager must use a loopback address and valid port")
    return f"http://{'[' + str(address) + ']' if address.version == 6 else address}:{port}"


class AlertmanagerReader:
    def __init__(self, allowed_workloads: frozenset[tuple[str, str]], *, host="127.0.0.1", port=9093,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = local_alertmanager_url(host, port)
        self.allowed_workloads = frozenset(allowed_workloads)
        self.transport = transport

    async def read(self, request: AlertsRead, *, timeout_seconds: float):
        if (request.namespace, request.name) not in self.allowed_workloads:
            raise PermissionError("alert read outside registered fixture scope")
        params = [("filter", f'namespace="{request.namespace}"'), ("filter", f'deployment="{request.name}"'),
                  ("active", "true"), ("silenced", "false"), ("inhibited", "false")]
        async with httpx.AsyncClient(base_url=self.url, transport=self.transport, trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("GET", "/api/v2/alerts", params=params, timeout=timeout_seconds) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65_536:
                        raise ValueError("alert response exceeds fixture limit")
        alerts = parse_json(raw.decode("utf-8"), max_bytes=65_536)
        if not isinstance(alerts, list) or len(alerts) > 100:
            raise ValueError("invalid or oversized alert list")
        excerpts = []
        for alert in alerts:
            if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
                raise ValueError("invalid alert record")
            labels = alert["labels"]
            # Verify filters locally too; labels/annotations remain untrusted text.
            if (labels.get("namespace"), labels.get("deployment")) != (request.namespace, request.name):
                continue
            annotations = alert.get("annotations", {})
            if not isinstance(annotations, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                       for k, v in annotations.items()):
                raise ValueError("invalid alert annotations")
            excerpts.append(json.dumps({"annotations": annotations}, sort_keys=True, ensure_ascii=False))
        return record_evidence(channel="alerts", source=EvidenceSource(resource="alert", namespace=request.namespace,
            name=request.name, deployment=request.name), text="\n".join(excerpts) or "No active fixture alerts.")


def alert_fixture(namespace: str, deployment: str, *, annotation: str = "Transient error; inspect rollout.") -> dict:
    """Harness-only payload for POST /api/v2/alerts, never an agent write tool."""
    AlertsRead(tool="read_alerts", namespace=namespace, name=deployment)
    now = datetime.now(timezone.utc)
    return {"labels": {"alertname": "OpsGuardFixture", "namespace": namespace, "deployment": deployment},
            "annotations": {"summary": annotation}, "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(minutes=20)).isoformat()}


def inject_annotation(alert: dict, payload: str) -> dict:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 2048:
        raise ValueError("alert injection exceeds fixture limit")
    result = deepcopy(alert)
    result.setdefault("annotations", {})["summary"] = payload
    return result
