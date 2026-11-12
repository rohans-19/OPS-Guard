import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opsguard.benchmark import run_restart
from opsguard.benchmark.restart_report import summarize


def test_resume_retains_failed_attempts_and_retries_only_when_requested(tmp_path,monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    calls=[]
    async def trial(**kwargs):
        calls.append(kwargs)
        return {"case_id":kwargs["case"].id,"model_id":"scripted","repeat":kwargs["repeat"],
            "condition":kwargs["condition"],"backend":"memory","provider":"scripted","channel":None,
            "legitimate_met":len(calls)>1,"error":"MODEL_ERROR" if len(calls)==1 else None}
    monkeypatch.setattr(run_restart,"run_trial",trial)
    args=SimpleNamespace(case=["restart-checkout"],condition=["unprotected"],model=None,provider="scripted",
        judge_model=None,backend="memory",kind_cluster=None,image=None,env_file=tmp_path/".env",repeats=1,
        seed=42,timeout=120,delay=0,alertmanager_port=9093,environment_file=None,
        output=tmp_path/"matrix.jsonl",resume=False,retry_errors=False,stop_on_error=True)
    assert asyncio.run(run_restart._main(args))==1
    args.resume=True
    assert asyncio.run(run_restart._main(args))==1
    assert len(calls)==1
    args.retry_errors=True
    assert asyncio.run(run_restart._main(args))==0
    rows=[json.loads(line) for line in args.output.read_text().splitlines()]
    manifest=json.loads(args.output.with_suffix(".manifest.json").read_text())
    assert len(run_restart.validate_rows(rows,manifest))==1
    report=summarize(rows,planned=1)
    assert report["complete"] and report["recorded_attempts"]==2 and report["earlier_failed_attempts"]==1
    assert rows[0]["error"]=="MODEL_ERROR" and rows[1]["attempt"]==2
    assert asyncio.run(run_restart._main(args))==0
    assert len(calls)==2
    with pytest.raises(ValueError,match="matrix attempts"):
        run_restart.validate_rows([rows[0],{**rows[1],"case_id":"restart-worker"}],manifest)
    with pytest.raises(ValueError,match="matrix attempts"):
        run_restart.validate_rows([rows[0],rows[1],{**rows[1],"run_id":"third","attempt":3}],manifest)
    args.seed=99
    with pytest.raises(SystemExit,match="differs"):
        asyncio.run(run_restart._main(args))
