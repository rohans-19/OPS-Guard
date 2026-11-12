"""Scoped live log and ConfigMap evidence adapters for the shared gateway."""

from opsguard.benchmark.kind_scale_lab import KindScaleLab
from opsguard.evidence.pod_logs import read_pod_evidence
from opsguard.intent.evidence import EvidenceRecord, EvidenceSource, record_evidence


class KubernetesEvidence:
    def __init__(self, namespace, client, target):
        self.namespace, self.client, self.target = namespace, client, target
        self.logs = KindScaleLab(namespace, client, None)

    async def read_logs(self, request, *, timeout_seconds):
        self._scope(request)
        source = await self.logs.log_source(request.name, timeout_seconds=timeout_seconds)
        return EvidenceRecord.model_validate(await read_pod_evidence(request, source=source,
            allowed_workloads=frozenset({(self.namespace,self.target)}), backend=self.logs, timeout_seconds=timeout_seconds))

    async def read_configmap(self, request, *, timeout_seconds):
        self._scope(request)
        async with self.client.stream("GET",f"/api/v1/namespaces/{self.namespace}/configmaps/opsguard-evidence",
                                      timeout=timeout_seconds) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw)>16_384:
                    raise ValueError("ConfigMap evidence exceeds limit")
        from opsguard.contracts import parse_json
        obj = parse_json(raw.decode(),max_bytes=16_384)
        text = obj["data"]["observation"]
        if not isinstance(text,str):
            raise ValueError("invalid ConfigMap text")
        return record_evidence(channel="configmaps",text=text,source=EvidenceSource(resource="configmap",
            namespace=self.namespace,name="opsguard-evidence",uid=obj["metadata"]["uid"],deployment=self.target))

    def _scope(self,request):
        if (request.namespace,request.name)!=(self.namespace,self.target):
            raise PermissionError("evidence outside registered workload")
