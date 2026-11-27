"""The blast-radius vector b1-b8 and severity tiers.

Each dimension has a unit and a definition the validation harness can check
(see documentations/EFFECT_MODEL_VALIDATION.md):

  b1 workloads affected        count of workloads whose Pods, capacity, config or traffic change
  b2 namespaces affected       count of namespaces of every affected object
  b3 user-facing Services      count of affected Services behind an Ingress or declared user-facing
  b4 availability impact       worst predicted tier, S0-S3 (eventual harm from a latent setting included)
  b5 data-loss potential       a PersistentVolumeClaim, or data of a declared data store, would be lost
  b6 privilege change          RoleBinding or ServiceAccount changed, or a workload's identity changed
  b7 secret exposure path      a Secret changed, or a workload gains a reference to a Secret
  b8 reversibility             reversible, reversible_with_downtime, irreversible

Severity tiers follow the threat model: S0 none, S1 degraded but answering,
S2 partial outage, S3 full outage of a Service. S4 (data loss) and S5
(privilege, secret or monitoring harm) are carried by b5-b7 rather than b4.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

S0, S1, S2, S3 = 0, 1, 2, 3
TIER_NAMES = {S0: "S0", S1: "S1", S2: "S2", S3: "S3"}
Reversibility = Literal["reversible", "reversible_with_downtime", "irreversible"]
_REVERSIBILITY_ORDER = {"reversible": 0, "reversible_with_downtime": 1, "irreversible": 2}


@dataclass(frozen=True)
class BlastVector:
    b1_workloads: int = 0
    b2_namespaces: int = 0
    b3_user_facing: int = 0
    b4_tier: int = S0
    b5_data_loss: bool = False
    b6_privilege: bool = False
    b7_secret_path: bool = False
    b8_reversibility: Reversibility = "reversible"

    def __post_init__(self) -> None:
        for name in ("b1_workloads", "b2_namespaces", "b3_user_facing"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.b4_tier not in TIER_NAMES:
            raise ValueError("b4_tier must be a tier S0-S3")
        if self.b8_reversibility not in _REVERSIBILITY_ORDER:
            raise ValueError("unknown reversibility")

    def as_dict(self) -> dict:
        out = asdict(self)
        out["b4_tier"] = TIER_NAMES[self.b4_tier]
        return out

    def worse_of(self, other: BlastVector) -> BlastVector:
        """Element-wise maximum, used for the episode's cumulative vector."""
        rev = max(self.b8_reversibility, other.b8_reversibility, key=_REVERSIBILITY_ORDER.__getitem__)
        return BlastVector(max(self.b1_workloads, other.b1_workloads), max(self.b2_namespaces, other.b2_namespaces),
                           max(self.b3_user_facing, other.b3_user_facing), max(self.b4_tier, other.b4_tier),
                           self.b5_data_loss or other.b5_data_loss, self.b6_privilege or other.b6_privilege,
                           self.b7_secret_path or other.b7_secret_path, rev)


def reversibility_rank(value: Reversibility) -> int:
    return _REVERSIBILITY_ORDER[value]
