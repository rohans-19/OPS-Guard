"""Read only explicitly registered Markdown fixtures, and label the text untrusted."""

from dataclasses import dataclass
from pathlib import Path

from opsguard.contracts import RunbookRead
from opsguard.intent.evidence import EvidenceRecord, EvidenceSource, record_evidence


@dataclass(frozen=True)
class RunbookBinding:
    namespace: str
    deployment: str
    filename: str
    section: str = "Incident"


class RunbookReader:
    def __init__(self, root: Path, bindings: dict[str, RunbookBinding]):
        self.root = root.resolve(strict=True)
        self.bindings = dict(bindings)

    def read(self, request: RunbookRead) -> EvidenceRecord:
        binding = self.bindings.get(request.runbook_id)
        if binding is None or (request.namespace, request.name) != (binding.namespace, binding.deployment):
            raise PermissionError("runbook is outside task scope")
        path = (self.root / binding.filename).resolve(strict=True)
        if not path.is_relative_to(self.root) or path.suffix.lower() != ".md":
            raise PermissionError("runbook must stay inside the fixture directory")
        with path.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("runbook fixture exceeds size limit")
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        marker = f"## {binding.section}\n"
        if text.count(marker) != 1:
            raise ValueError("runbook incident section is missing or ambiguous")
        section = text.split(marker, 1)[1].split("\n## ", 1)[0].strip()
        return record_evidence(channel="runbooks", text=section, source=EvidenceSource(
            namespace=request.namespace, resource="runbook", name=request.runbook_id,
            deployment=request.name, section=binding.section,
        ))


def inject_incident(template: str, payload: str, *, section: str = "Incident") -> str:
    """Harness-only helper: return a modified fixture; never write to reader files."""
    template = template.replace("\r\n", "\n").replace("\r", "\n")
    marker = f"## {section}\n"
    if template.count(marker) != 1 or not isinstance(payload, str) or len(payload.encode("utf-8")) > 512:
        raise ValueError("invalid fixture section or payload")
    return template.replace(marker, marker + payload + "\n\n", 1)
