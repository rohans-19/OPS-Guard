"""Groq SDK adapter for the existing bounded agent protocol."""

from dataclasses import asdict
from copy import deepcopy
import asyncio
import json
import math
import os
import re
from time import monotonic

import httpx

from opsguard.agent.interfaces import ModelReply
from opsguard.contracts import parse_json


def response_schema(schema):
    """Place the unchanged protocol union inside a function parameter object."""
    body = deepcopy(schema)
    definitions = body.pop("$defs", {})
    return {"type": "object", "properties": {"response": body},
            "required": ["response"], "additionalProperties": False, "$defs": definitions}


def groq_key(env_file=None):
    key = os.environ.get("GROQ_API_KEY")
    if not key and env_file is not None and env_file.is_file():
        from dotenv import dotenv_values
        key = dotenv_values(env_file).get("GROQ_API_KEY")
    if not key or not key.strip() or key.strip() in ("your_key", "your_key_here"):
        raise ValueError("configure GROQ_API_KEY in the environment or the ignored env file")
    return key.strip()


class GroqModel:
    """One local response function; only the gateway can execute proposals."""

    def __init__(self, model, *, api_key=None, env_file=None, seed=42, max_tokens=1024, client=None):
        if not isinstance(model,str) or not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,200}",model):
            raise ValueError("explicit Groq model ID required")
        self.model_id, self.seed, self.max_tokens = model, seed, max_tokens
        self._key = api_key or (groq_key(env_file) if client is None else None)
        self._client = client
        self.audit = []

    async def _request(self, client, options, timeout_seconds):
        """Retry only rejected model requests, honoring bounded Retry-After.

        This never retries a Kubernetes write. Waiting is included in task
        timing and recorded separately; long quota waits stop the trial.
        """
        from groq import APIStatusError, RateLimitError
        deadline = monotonic() + timeout_seconds
        for attempt in range(4):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("model request deadline")
            try:
                response = await client.chat.completions.create(**{**options,"timeout":remaining})
                usage = getattr(response,"usage",None)
                details = getattr(usage,"prompt_tokens_details",None)
                self.audit.append({"event":"response","model_id":self.model_id,
                    "input_tokens":getattr(usage,"prompt_tokens",None),
                    "output_tokens":getattr(usage,"completion_tokens",None),
                    "cached_tokens":getattr(details,"cached_tokens",None)})
                return response
            except RateLimitError as error:
                try:
                    delay = float(error.response.headers.get("retry-after","nan"))
                except ValueError:
                    delay = math.nan
                self.audit.append({"event":"rate_limit","model_id":self.model_id,
                    "retry_after_seconds":delay if math.isfinite(delay) else None})
                if attempt == 3 or not math.isfinite(delay) or not 0 <= delay <= 60 or delay + .25 >= deadline-monotonic():
                    raise
                started = monotonic()
                await asyncio.sleep(delay + .25)
                self.audit.append({"event":"rate_wait","model_id":self.model_id,
                                   "elapsed_ms":round((monotonic()-started)*1000,3)})
            except APIStatusError as error:
                body=error.body if isinstance(error.body,dict) else {}
                detail=body.get("error",body)
                code=detail.get("code") if isinstance(detail,dict) else None
                self.audit.append({"event":"api_error","model_id":self.model_id,"status":error.status_code,
                    "code":code if isinstance(code,str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}",code) else None})
                # Groq rejects malformed model function/JSON output at the API
                # boundary. Count this as one invalid model turn, just as the
                # local adapter does, rather than executing failed_generation
                # or retrying an action. Other API errors remain infrastructure
                # errors and stop the run.
                if error.status_code==400 and code in ("tool_use_failed","json_validate_failed","output_parse_failed"):
                    return None
                raise

    async def complete(self, messages, *, schema, timeout_seconds):
        from groq import AsyncGroq
        wire = [asdict(message) for message in messages]
        instruction = ("Use the submit_response function exactly once to return the next protocol object, including finish. "
                       "Each protocol example above belongs inside the function's response argument. "
                       "The function submits a proposal; the application separately validates and executes it.")
        pending=None
        for index,item in enumerate(wire):
            if item["role"]=="assistant":
                pending=f"opsguard_turn_{index}"
                wire[index]={"role":"assistant","tool_calls":[{"id":pending,"type":"function","function":{
                    "name":"submit_response","arguments":json.dumps({"response":parse_json(item["content"])})}}]}
            elif pending is not None and item["role"]=="user":
                wire[index]={"role":"tool","tool_call_id":pending,"content":item["content"]}
                pending=None
        if wire and wire[0]["role"] == "system":
            wire[0]["content"] += "\n"+instruction
        else:
            wire.insert(0,{"role":"system","content":instruction})
        options = dict(model=self.model_id,messages=wire,
                       tools=[{"type":"function","function":{"name":"submit_response",
                           "description":"Submit one protocol response for local validation.","parameters":response_schema(schema)}}],
                       tool_choice={"type":"function","function":{"name":"submit_response"}},parallel_tool_calls=False,
                       temperature=0,seed=self.seed,max_completion_tokens=self.max_tokens,timeout=timeout_seconds)
        if self.model_id.startswith("openai/gpt-oss-"):
            options["reasoning_effort"] = "low"
        if self._client is not None:
            response = await self._request(self._client,options,timeout_seconds)
        else:
            async with AsyncGroq(api_key=self._key,max_retries=0,
                http_client=httpx.AsyncClient(trust_env=False,follow_redirects=False)) as client:
                response = await self._request(client,options,timeout_seconds)
        if response is None:
            return ModelReply("{}")
        if len(response.choices)!=1 or response.choices[0].finish_reason!="tool_calls":
            raise ValueError("Groq response did not complete normally")
        calls=response.choices[0].message.tool_calls
        if not calls or len(calls)!=1 or calls[0].type!="function" or calls[0].function.name!="submit_response":
            raise ValueError("Groq must return exactly the registered response function")
        text = calls[0].function.arguments
        if not isinstance(text,str) or len(text.encode())>65_536:
            raise ValueError("missing or oversized Groq response")
        envelope = parse_json(text)
        if not isinstance(envelope,dict) or set(envelope)!={"response"}:
            raise ValueError("invalid Groq response envelope")
        text = json.dumps(envelope["response"])
        usage = response.usage
        counts = [getattr(usage,name,None) for name in ("prompt_tokens","completion_tokens")]
        return ModelReply(text,*(n if type(n) is int and n>=0 else None for n in counts))
