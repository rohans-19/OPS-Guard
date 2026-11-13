"""Exercise authorization and API boundaries without a model key or live cluster."""
import asyncio
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from opsguard.demo.api import create_app
from opsguard.demo.runtime import LiveRun, RunRequest, bounds_for, intent_for
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.policy import evaluate_policy
from opsguard.kube.executor import MutationExecutor


def config(namespace="shop", uid="config-1", version="7"):
    return {"apiVersion":"v1","kind":"ConfigMap","metadata":{"namespace":namespace,"name":"app-settings","uid":uid,"resourceVersion":version},
            "data":{"mode":"disabled","untouched":"preserve-me"}}


def test_config_authorization_rejects_wrong_key_value_and_target():
    r = RunRequest(operation="set_config",prompt="Enable checkout")
    intent = intent_for(r,"shop","demo-test")
    inventory = InventorySnapshot("shop",configmaps=(config(),))
    action = {"verb":"set_config","resource":"configmap","namespace":"shop","name":"app-settings","arguments":{"key":"mode","value":"enabled"}}
    result = evaluate_policy(action,intent=intent,task_id=intent.task_id,inventory=inventory)
    assert result.status == "PASS" and result.resolution.target.uid == "config-1"
    for arguments in ({"key":"untouched","value":"enabled"},{"key":"mode","value":"disabled"}):
        assert evaluate_policy({**action,"arguments":arguments},intent=intent,task_id=intent.task_id,inventory=inventory).status == "BLOCK"
    assert evaluate_policy({**action,"name":"another-config"},intent=intent,task_id=intent.task_id,inventory=inventory).status == "BLOCK"
    recreated = intent.model_copy(update={"target":intent.target.model_copy(update={"uid":"old-uid"})})
    assert evaluate_policy(action,intent=recreated,task_id=intent.task_id,inventory=inventory).reason == "TARGET_RECREATED"


def test_config_executor_uses_same_preconditioned_body_once():
    class Transport:
        calls = []
        async def send(self,mutation,*,dry_run,timeout_seconds):
            self.calls.append((dry_run,mutation.body()))
            return config()
    async def check():
        transport = Transport()
        executor = MutationExecutor(transport,bounds_for("shop"))
        action = {"verb":"set_config","resource":"configmap","namespace":"shop","name":"app-settings","arguments":{"key":"mode","value":"enabled"}}
        prepared = executor.prepare(action,deployment=config())
        assert prepared.body()[:2] == [{"op":"test","path":"/metadata/uid","value":"config-1"},{"op":"test","path":"/metadata/resourceVersion","value":"7"}]
        assert (await executor.preview(prepared,timeout_seconds=1)).status == "PREVIEWED"
        assert (await executor.execute(prepared,decision="ALLOW",timeout_seconds=1)).status == "APPLIED"
        assert (await executor.execute(prepared,decision="ALLOW",timeout_seconds=1)).status == "BLOCK"
        assert len(transport.calls) == 2 and transport.calls[0][1] == transport.calls[1][1]
        assert prepared.body()[-1] == {"op":"add","path":"/data/mode","value":"enabled"}
    asyncio.run(check())


def test_config_block_withholds_persistent_write():
    class Transport:
        calls = []
        async def send(self,mutation,*,dry_run,timeout_seconds):
            self.calls.append(dry_run)
            return config()
    async def check():
        transport = Transport()
        executor = MutationExecutor(transport,bounds_for("shop"))
        action = {"verb":"set_config","resource":"configmap","namespace":"shop","name":"app-settings","arguments":{"key":"mode","value":"enabled"}}
        prepared = executor.prepare(action,deployment=config())
        await executor.preview(prepared,timeout_seconds=1)
        assert (await executor.execute(prepared,decision="ESCALATE",timeout_seconds=1)).reason == "WRITE_WITHHELD"
        assert transport.calls == [True]
    asyncio.run(check())


def test_payload_byte_limit_and_extra_authority_fields_rejected():
    with pytest.raises(ValueError):
        RunRequest(prompt="Scale checkout",payload="🙂"*129)
    with pytest.raises(ValueError):
        RunRequest(prompt="Scale checkout",namespace="kube-system")
    with pytest.raises(ValueError):
        RunRequest(prompt="Scale checkout",replicas=True)


def test_form_sets_exact_scale_authority():
    intent = intent_for(RunRequest(prompt="Scale checkout",replicas=3),"shop","demo-test")
    assert intent.permissions[0].min_replicas == intent.permissions[0].max_replicas == 3
    assert "payments-api" in intent.protected_deployments


def test_verified_completion_stops_without_fabricating_a_model_finish():
    from opsguard.agent.loop import run_agent
    from opsguard.agent.models import ScriptedModel
    from opsguard.agent.interfaces import GatewayResult
    async def check():
        completion = asyncio.Event()
        class Gateway:
            async def read(self,request,**kwargs):
                completion.set()
                return GatewayResult(status="OK",reason="STATE_OBSERVED")
        model = ScriptedModel([json.dumps({"kind":"read","request":{"tool":"get_deployment","namespace":"shop","name":"checkout-api"}})])
        result = await run_agent(task_id="demo-test",user_task="Scale checkout",model=model,gateway=Gateway(),completion=completion)
        assert result.status == "COMPLETION_VERIFIED"
        assert result.steps == 1
        assert len([e for e in result.trace if e["event"]=="model"]) == 1
    asyncio.run(check())


def test_event_log_preserves_order_and_stage_duration(tmp_path):
    run = LiveRun(RunRequest(prompt="Scale checkout"),tmp_path,"test",9093)
    run.emit({"event":"execution","elapsed_ms":23,"status":"APPLIED"})
    run.emit({"event":"outcome","task_completed":False})
    saved = [json.loads(line) for line in (run.folder/"events.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in saved] == [1,2]
    assert saved[0]["stage_elapsed_ms"] == 23
    assert not saved[1]["task_completed"]


def test_api_origin_session_idempotency_and_event_recovery(tmp_path,monkeypatch):
    monkeypatch.setattr("opsguard.demo.api.groq_key",lambda *_:"test-key")
    async def fake_run(self):
        self.emit({"event":"setup"})
        self.status = "completed"
        await self.release.wait()
        self.emit({"event":"cleanup"})
    monkeypatch.setattr(LiveRun,"run",fake_run)
    app = create_app(tmp_path,"test")
    with TestClient(app) as client:
        body = {"prompt":"Scale checkout"}
        assert client.post("/api/runs",json=body).status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Demo-Token":token,"Idempotency-Key":"submit-1"}
        assert client.post("/api/runs",json=body,headers={**headers,"Origin":"https://untrusted.example"}).status_code == 403
        first = client.post("/api/runs",json=body,headers=headers).json()
        second = client.post("/api/runs",json=body,headers=headers).json()
        assert first["id"] == second["id"] and len(app.state.runs) == 1
        assert client.post("/api/runs",json={"prompt":"A different task"},headers=headers).status_code == 409
        run_id = first["id"]
        assert client.post(f"/api/runs/{run_id}/reset",headers=headers).status_code == 200
        with client.stream("GET",f"/api/runs/{run_id}/events?after=1") as stream:
            text = "".join(stream.iter_text())
            assert 'id: 2' in text and 'id: 1\n' not in text
        exported = client.get(f"/api/runs/{run_id}/export").text
        assert "test-key" not in exported and '"event": "setup"' in exported
