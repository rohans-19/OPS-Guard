import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("groq")
pytest.importorskip("dotenv")

from opsguard.agent.groq_model import GroqModel,groq_key,response_schema
from opsguard.agent.interfaces import Message,Step


def response(value,usage=None):
    call=SimpleNamespace(type="function",function=SimpleNamespace(name="submit_response",
        arguments=json.dumps({"response":value})))
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
        message=SimpleNamespace(tool_calls=[call]))],usage=usage)


def test_groq_adapter_preserves_schema_usage_and_explicit_limits():
    calls=[]
    async def complete(**kwargs):
        calls.append(kwargs)
        return response({"kind":"finish","summary":"Done"},SimpleNamespace(prompt_tokens=12,completion_tokens=6))
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    adapter=GroqModel("test-model",client=client,seed=7)
    reply=asyncio.run(adapter.complete([Message("system","Return JSON")],schema={"type":"object"},timeout_seconds=2))
    assert reply.input_tokens==12 and reply.output_tokens==6
    assert 0 < calls[0]["timeout"] <= 2 and calls[0]["seed"]==7
    assert calls[0]["tool_choice"]=={"type":"function","function":{"name":"submit_response"}}
    assert calls[0]["tools"][0]["function"]["parameters"]["properties"]["response"]=={"type":"object"}


def test_key_loading_never_needs_committed_configuration(tmp_path,monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY",raising=False)
    path=tmp_path/".env"
    path.write_text("GROQ_API_KEY=\n")
    with pytest.raises(ValueError,match="configure"):
        groq_key(path)
    path.write_text('GROQ_API_KEY="synthetic-test-key"\n')
    assert groq_key(path)=="synthetic-test-key"


def test_truncated_model_responses_fail_before_proposing_actions():
    async def complete(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    with pytest.raises(ValueError,match="complete"):
        asyncio.run(GroqModel("test",client=client).complete([],schema={},timeout_seconds=1))


def test_function_transport_preserves_protocol_and_pairs_history_with_results():
    original=Step.json_schema()
    wire=response_schema(original)
    assert wire["$defs"]==original["$defs"]
    assert wire["properties"]["response"]["oneOf"]==original["oneOf"]
    calls=[]
    step={"kind":"action","request":{"verb":"restart","resource":"deployment",
          "namespace":"shop","name":"checkout","arguments":{}},"observation_ids":[]}
    async def complete(**kwargs):
        calls.append(kwargs)
        return response(step)
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    reply=asyncio.run(GroqModel("openai/gpt-oss-20b",client=client).complete(
        [Message("assistant",json.dumps(step)),Message("user",'{"gateway_observation":{"status":"ALLOW"}}')],
        schema=original,timeout_seconds=2))
    assert Step.validate_json(reply.content).request.verb=="restart"
    history=calls[0]["messages"]
    tool_call=history[-2]["tool_calls"][0]
    assert json.loads(tool_call["function"]["arguments"])["response"]==step
    assert history[-1]["role"]=="tool" and history[-1]["tool_call_id"]==tool_call["id"]


def test_rate_limit_retries_are_bounded_audited_and_do_not_retry_other_errors(monkeypatch):
    import httpx
    from groq import RateLimitError
    waits=[]
    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr("opsguard.agent.groq_model.asyncio.sleep",sleep)
    count=0
    async def complete(**kwargs):
        nonlocal count
        count+=1
        if count==1:
            raise RateLimitError("test",response=httpx.Response(429,headers={"retry-after":"2"},
                request=httpx.Request("POST","https://example.test")),body={})
        return response({})
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    model=GroqModel("test",client=client)
    asyncio.run(model.complete([],schema={},timeout_seconds=10))
    assert count==2 and waits==[2.25]
    assert [event["event"] for event in model.audit]==["rate_limit","rate_wait","response"]
    count=0
    with pytest.raises(RateLimitError):
        asyncio.run(model.complete([],schema={},timeout_seconds=1))
    assert count==1


def test_unregistered_or_parallel_function_calls_never_become_actions():
    async def complete(**kwargs):
        result=response({"kind":"finish","summary":"Done"})
        result.choices[0].message.tool_calls[0].function.name="shell"
        return result
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    with pytest.raises(ValueError,match="registered response"):
        asyncio.run(GroqModel("test",client=client).complete([],schema={},timeout_seconds=1))


@pytest.mark.parametrize("code", ["tool_use_failed", "output_parse_failed"])
def test_provider_rejected_generation_counts_as_one_invalid_model_turn(code):
    import httpx
    from groq import BadRequestError
    calls=[]
    async def complete(**kwargs):
        calls.append(kwargs)
        raise BadRequestError("invalid generation",response=httpx.Response(400,
            request=httpx.Request("POST","https://example.test")),
            body={"code":code,"failed_generation":"must never execute"})
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    model=GroqModel("test",client=client)
    reply=asyncio.run(model.complete([],schema={},timeout_seconds=1))
    assert len(calls)==1 and reply.content=="{}"
    with pytest.raises(ValueError):
        Step.validate_json(reply.content)
    assert model.audit[0]["code"]==code and "failed_generation" not in model.audit[0]
