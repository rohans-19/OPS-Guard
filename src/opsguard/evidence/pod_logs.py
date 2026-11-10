"""Scope pod-log access at the gateway and label all returned text untrusted."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from opsguard.contracts import PodLogsRead
from opsguard.intent.evidence import EvidenceSource, record_evidence

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
    return record_evidence(channel="logs", text=raw, max_bytes=MAX_LOG_BYTES, source=EvidenceSource(
        namespace=source.namespace, resource="pod", name=source.pod, uid=source.uid,
        pod=source.pod, container=source.container, pod_uid=source.uid, deployment=request.name,
    )).model_dump()
