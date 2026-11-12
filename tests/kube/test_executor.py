import asyncio
from copy import deepcopy

import httpx
import pytest

from opsguard.benchmark.restart_lab import MemoryExecutionLab
from opsguard.contracts import RestartAction, ScaleAction, SetImageAction
from opsguard.kube.executor import KubernetesTransport, MutationExecutor
from opsguard.kube.operations import LabBounds, default_registry
from opsguard.operations.restart import RESTART_ANNOTATION


def fixture(transport=None):
    lab = MemoryExecutionLab({"checkout-api": {"service": "checkout", "replicas": 1},
                              "payments-api": {"service": "payments", "replicas": 1}})
    bounds = LabBounds(lab.namespace, scales={(lab.namespace, "checkout-api"): (0, 4)},
        images={(lab.namespace, "checkout-api", "app"): frozenset({"opsguard/app:v2"})},
        restarts=frozenset((lab.namespace, name) for name in lab.deployments))
    return lab, MutationExecutor(transport or lab, bounds)


def request(verb="restart", **changes):
    cls, arguments = {"restart": (RestartAction, {}), "scale": (ScaleAction, {"replicas": 2}),
                      "set_image": (SetImageAction, {"container": "app", "image": "opsguard/app:v2"})}[verb]
    return cls.model_validate({"verb": verb, "resource": "deployment", "namespace": "restart-lab",
                               "name": "checkout-api", "arguments": arguments, **changes})


@pytest.mark.parametrize("verb", ["scale", "set_image", "restart"])
def test_registered_operations_preview_without_writing_and_reuse_exact_body(verb):
    lab, executor = fixture()
    original = deepcopy(lab.deployments)
    prepared = executor.prepare(request(verb), deployment=lab.deployments["checkout-api"])
    async def scenario():
        preview = await executor.preview(prepared, timeout_seconds=1)
        assert preview.status == "PREVIEWED" and lab.deployments == original
        result = await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)
        assert result.status == "APPLIED"
        assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "BLOCK"
    asyncio.run(scenario())
    assert len(lab.calls) == 2 and lab.calls[0][1].body_json == lab.calls[1][1].body_json
    assert lab.deployments["payments-api"] == original["payments-api"]
    template = lab.deployments["checkout-api"]["spec"]["template"]
    assert template["metadata"]["annotations"]["fixture-note"] == "preserve"
    assert template["spec"]["containers"][1]["image"] == "opsguard/sidecar:v1"
    if verb == "restart":
        assert template["metadata"]["annotations"][RESTART_ANNOTATION]


@pytest.mark.parametrize("decision", ["BLOCK", "ESCALATE", "PASS", "anything"])
def test_non_allow_decisions_withhold_and_invalidate_ticket(decision):
    lab, executor = fixture()
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        await executor.preview(prepared, timeout_seconds=1)
        assert (await executor.execute(prepared, decision=decision, timeout_seconds=1)).status in ("BLOCK", "ESCALATE")
        assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "BLOCK"
    asyncio.run(scenario())
    assert [dry for dry, _ in lab.calls] == [True]


def test_preview_and_executor_owned_preparation_are_required():
    lab, executor = fixture()
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    forged = prepared.model_copy(update={"path": "/api/v1/namespaces/other/secrets"})
    async def scenario():
        assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "BLOCK"
        assert (await executor.preview(forged, timeout_seconds=1)).status == "BLOCK"
        independent = MutationExecutor(lab, executor.bounds)
        assert (await independent.preview(prepared, timeout_seconds=1)).status == "BLOCK"
    asyncio.run(scenario())
    assert lab.calls == []


def test_concurrent_execute_cannot_send_twice():
    lab, executor = fixture()
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        await executor.preview(prepared, timeout_seconds=1)
        outcomes = await asyncio.gather(*(executor.execute(prepared, decision="ALLOW", timeout_seconds=1) for _ in range(2)))
        assert sorted(result.status for result in outcomes) == ["APPLIED", "BLOCK"]
    asyncio.run(scenario())
    assert len(lab.calls) == 2


@pytest.mark.parametrize("field", ["uid", "resourceVersion"])
def test_target_change_requires_fresh_preparation(field):
    lab, executor = fixture()
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        await executor.preview(prepared, timeout_seconds=1)
        lab.deployments["checkout-api"]["metadata"][field] = "changed"
        result = await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)
        assert result.reason == "STALE_TARGET" and result.requires_reevaluation
        assert not result.write_outcome_unknown
    asyncio.run(scenario())
    assert len(lab.calls) == 1


def test_expired_preparation_is_not_executed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("opsguard.kube.executor.monotonic", lambda: clock[0])
    lab, executor = fixture()
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        await executor.preview(prepared, timeout_seconds=1)
        clock[0] += 121
        assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "BLOCK"
    asyncio.run(scenario())
    assert len(lab.calls) == 1


@pytest.mark.parametrize("preview_failure", [True, False])
def test_timeouts_distinguish_safe_preview_failure_from_unknown_write(preview_failure):
    class LostReply:
        async def send(self, mutation, *, dry_run, timeout_seconds):
            if preview_failure or not dry_run:
                raise TimeoutError
            return {}
    lab, executor = fixture(LostReply())
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        preview = await executor.preview(prepared, timeout_seconds=1)
        outcome = preview if preview_failure else await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)
        assert outcome.reason == ("PREVIEW_TIMEOUT" if preview_failure else "WRITE_OUTCOME_UNKNOWN")
        assert outcome.write_outcome_unknown is not preview_failure
        assert executor.write_outcome_unknown is not preview_failure
    asyncio.run(scenario())
    if not preview_failure:
        with pytest.raises(ValueError, match="reconcile"):
            executor.prepare(request(), deployment=lab.deployments["checkout-api"])


@pytest.mark.parametrize("code,reason", [(401,"API_AUTHORIZATION_ERROR"), (403,"API_AUTHORIZATION_ERROR"),
    (404,"TARGET_NOT_FOUND"), (409,"STALE_TARGET"), (422,"API_VALIDATION_ERROR"), (500,"API_ERROR")])
def test_http_errors_are_explicit_and_failed_preview_never_writes(code, reason):
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(code, json={"message": "synthetic rejection"})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
            lab, executor = fixture(KubernetesTransport("restart-lab", client))
            prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
            result = await executor.preview(prepared, timeout_seconds=1)
            assert result.reason == reason and not result.write_outcome_unknown
            assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "BLOCK"
    asyncio.run(scenario())
    assert len(calls) == 1 and calls[0].url.params["dryRun"] == "All"


def test_wire_bytes_headers_and_preview_flag_are_consistent():
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(200, json={})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
            lab, executor = fixture(KubernetesTransport("restart-lab", client))
            prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
            copy = prepared.body()
            copy[-1]["value"] = "tampered"
            assert prepared.body()[-1]["value"] != "tampered"
            await executor.preview(prepared, timeout_seconds=1)
            await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)
    asyncio.run(scenario())
    assert calls[0].content == calls[1].content
    assert calls[0].method == calls[1].method == "PATCH"
    assert calls[0].url.path == calls[1].url.path
    assert calls[0].headers["content-type"] == "application/json-patch+json"
    assert not calls[1].url.params


def test_restart_rejects_model_marker_and_unregistered_or_out_of_scope_operations():
    lab, executor = fixture()
    for value in ({"marker": "approved"}, {"patch": []}, {"dry_run": False}):
        with pytest.raises(ValueError):
            request(arguments=value)
    with pytest.raises(ValueError):
        executor.prepare(request(name="outside"), deployment=lab.deployments["checkout-api"])
    with pytest.raises(ValueError):
        executor.prepare(request(namespace="other"), deployment=lab.deployments["checkout-api"])
    with pytest.raises(ValueError):
        default_registry().register("restart", lambda *_: None)


def test_restart_supports_absent_annotations_without_changing_other_template_fields():
    lab, executor = fixture()
    del lab.deployments["checkout-api"]["spec"]["template"]["metadata"]["annotations"]
    prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    async def scenario():
        await executor.preview(prepared, timeout_seconds=1)
        assert (await executor.execute(prepared, decision="ALLOW", timeout_seconds=1)).status == "APPLIED"
    asyncio.run(scenario())
    assert lab.deployments["checkout-api"]["spec"]["template"]["metadata"]["labels"] == {"app":"checkout-api"}


def test_cancellation_during_write_latches_executor_closed():
    async def scenario():
        entered = asyncio.Event()
        class SlowWrite:
            async def send(self, mutation, *, dry_run, timeout_seconds):
                if not dry_run:
                    entered.set()
                    await asyncio.Event().wait()
                return {}
        lab, executor = fixture(SlowWrite())
        prepared = executor.prepare(request(), deployment=lab.deployments["checkout-api"])
        await executor.preview(prepared, timeout_seconds=1)
        work = asyncio.create_task(executor.execute(prepared, decision="ALLOW", timeout_seconds=10))
        await entered.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert executor.write_outcome_unknown
        with pytest.raises(ValueError, match="reconcile"):
            executor.prepare(request(), deployment=lab.deployments["checkout-api"])
    asyncio.run(scenario())
