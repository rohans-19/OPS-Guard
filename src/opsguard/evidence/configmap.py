"""Read raw ConfigMap observations and seed them from the benchmark harness."""

from dataclasses import dataclass
from typing import ClassVar, Mapping, Optional, Protocol


class _ConfigMap(Protocol):
    data: Optional[Mapping[str, str]]


class _ConfigMapReader(Protocol):
    def read_namespaced_config_map(self, name: str, namespace: str) -> _ConfigMap: ...


class _ConfigMapWriter(Protocol):
    def patch_namespaced_config_map(
        self, name: str, namespace: str, body: dict[str, dict[str, str]]
    ) -> object: ...


@dataclass(frozen=True)
class ConfigMapEvidenceSource:
    """Trusted scope configured by the harness for one namespaced fixture."""

    namespace: str
    name: ClassVar[str] = "opsguard-evidence"
    key: ClassVar[str] = "observation"

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace or self.namespace.strip() != self.namespace:
            raise ValueError("a nonempty fixture namespace is required")


def read_configmap_evidence(
    client: _ConfigMapReader,
    *,
    source: ConfigMapEvidenceSource,
    namespace: str,
    name: str,
    key: str,
) -> str:
    """Read the allowlisted key and return its untrusted text verbatim."""
    if (namespace, name, key) != (source.namespace, source.name, source.key):
        raise ValueError("ConfigMap observation is outside the configured evidence source")
    configmap = client.read_namespaced_config_map(source.name, source.namespace)
    value = (configmap.data or {}).get(source.key)
    if not isinstance(value, str):
        raise ValueError("ConfigMap observation is missing or is not text")
    return value


def inject_configmap_evidence(
    client: _ConfigMapWriter, *, source: ConfigMapEvidenceSource, text: str
) -> None:
    """Set the fixture's evidence key using the harness's setup identity."""
    if not isinstance(text, str):
        raise ValueError("ConfigMap observation must be text")
    client.patch_namespaced_config_map(source.name, source.namespace, {"data": {source.key: text}})
