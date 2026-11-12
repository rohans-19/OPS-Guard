"""Gateway-owned evidence records; observation references never grant permission."""

from datetime import datetime, timezone
import hashlib
from typing import Literal
from uuid import uuid4

from pydantic import Field

from opsguard.contracts import DnsLabel, ObjectName, WireModel


class EvidenceSource(WireModel):
    namespace: DnsLabel
    resource: Literal["pod", "runbook", "alert", "configmap"]
    name: ObjectName
    uid: str | None = None
    deployment: ObjectName | None = None
    pod: ObjectName | None = None
    pod_uid: str | None = None
    container: DnsLabel | None = None
    section: str | None = Field(default=None, max_length=120)


class EvidenceRecord(WireModel):
    observation_id: str
    channel: Literal["logs", "alerts", "runbooks", "configmaps"]
    trust: Literal["untrusted"] = "untrusted"
    source: EvidenceSource
    observed_at: str
    content_sha256: str
    text: str = Field(max_length=4096)
    truncated_by_gateway: bool = False


def record_evidence(*, channel, source: EvidenceSource, text: str, max_bytes: int = 2048) -> EvidenceRecord:
    raw = text.encode("utf-8")
    excerpt = raw[:max_bytes].decode("utf-8", errors="ignore")
    return EvidenceRecord(
        observation_id=uuid4().hex, channel=channel, source=source,
        observed_at=datetime.now(timezone.utc).isoformat(), text=excerpt,
        content_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        truncated_by_gateway=len(raw) > max_bytes,
    )


class EvidenceRegistry:
    """One task's observations, registered by trusted readers only."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._records: dict[str, EvidenceRecord] = {}

    def add(self, record: EvidenceRecord) -> None:
        if record.observation_id in self._records:
            raise ValueError("observation IDs cannot be replaced")
        self._records[record.observation_id] = record

    def contains(self, task_id: str, observation_ids: tuple[str, ...]) -> bool:
        return task_id == self.task_id and all(ref in self._records for ref in observation_ids)
