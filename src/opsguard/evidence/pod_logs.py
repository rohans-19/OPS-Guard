"""Scope pod-log access at the gateway and label all returned text untrusted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Protocol
from uuid import uuid4

from opsguard.contracts import PodLogsRead

MAX_LOG_BYTES = 2_048


@dataclass(frozen=True)
class PodLogSource:
    namespace: str
    pod: str
    container: str
    uid: str


class LogBackend(Protocol):
    async def read_log(
        self, source: PodLogSource, *, tail_lines: int, limit_bytes: int, timeout_seconds: float,
    ) -> str: ...


async def read_pod_evidence(
    request: PodLogsRead, *, source: PodLogSource, allowed_workloads: frozenset[tuple[str, str]],
    backend: LogBackend, timeout_seconds: float,
) -> dict:
    if (request.namespace, request.name) not in allowed_workloads or source.namespace != request.namespace:
        raise PermissionError("pod log read is outside task scope")
    raw = await backend.read_log(
        source, tail_lines=request.tail_lines, limit_bytes=MAX_LOG_BYTES, timeout_seconds=timeout_seconds,
    )
    if not isinstance(raw, str):
        raise ValueError("pod log backend must return text")
    raw_bytes = raw.encode("utf-8")
    text = raw_bytes[:MAX_LOG_BYTES].decode("utf-8", errors="ignore")
    return {
        "observation_id": uuid4().hex,
        "channel": "logs",
        "trust": "untrusted",
        "source": {
            "namespace": source.namespace, "pod": source.pod, "container": source.container,
            "pod_uid": source.uid, "deployment": request.name,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "truncated_by_gateway": len(raw_bytes) > MAX_LOG_BYTES,
        "text": text,
    }
