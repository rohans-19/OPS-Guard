import asyncio
import os
from pathlib import Path

import pytest

from opsguard.benchmark.kind_executor_smoke import run_smoke


@pytest.mark.skipif(not os.environ.get("OPSGUARD_KIND_CLUSTER") or not os.environ.get("OPSGUARD_FIXTURE_IMAGE"),
                    reason="opt in with OPSGUARD_KIND_CLUSTER and OPSGUARD_FIXTURE_IMAGE")
def test_shared_executor_on_named_kind_cluster():
    result = asyncio.run(run_smoke(root=Path(__file__).resolve().parents[2],
        cluster=os.environ["OPSGUARD_KIND_CLUSTER"],image=os.environ["OPSGUARD_FIXTURE_IMAGE"]))
    assert result["checks"] and all(result["checks"].values())
