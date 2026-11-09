"""Deterministic scripted models and a loopback-only Ollama adapter."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict
import ipaddress
import re

import httpx

from opsguard.agent.interfaces import Message, ModelReply
from opsguard.contracts import parse_json


class ScriptedModel:
    model_id = "scripted"

    def __init__(self, responses: Iterable[str]) -> None:
        self._responses = iter(responses)

    async def complete(
        self, messages: Sequence[Message], *, schema: dict, timeout_seconds: float,
    ) -> ModelReply:
        try:
            return ModelReply(next(self._responses))
        except StopIteration as error:
            raise RuntimeError("scripted model ran out of responses") from error


class OllamaModel:
    """Calls a local server only. Start Ollama with cloud features disabled."""

    def __init__(
        self, model: str, *, host: str = "127.0.0.1", port: int = 11434, seed: int = 42,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_loopback or not 1 <= port <= 65535:
            raise ValueError("Ollama endpoint must be a loopback IP and valid port")
        if (not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,200}", model) or
                re.search(r"(?:[:-]cloud)(?:$|[-:])", model, re.IGNORECASE)):
            raise ValueError("provide a locally installed model name")
        authority = f"[{address}]" if address.version == 6 else str(address)
        self._url = f"http://{authority}:{port}/api/chat"
        self.model_id = model
        self.seed = seed
        self._transport = transport

    async def complete(
        self, messages: Sequence[Message], *, schema: dict, timeout_seconds: float,
    ) -> ModelReply:
        body = {
            "model": self.model_id,
            "messages": [asdict(message) for message in messages],
            "stream": False, "format": schema,
            "options": {"temperature": 0, "seed": self.seed, "num_ctx": 4096, "num_predict": 256},
        }
        async with httpx.AsyncClient(
            timeout=timeout_seconds, trust_env=False, follow_redirects=False, transport=self._transport,
        ) as client:
            async with client.stream("POST", self._url, json=body) as response:
                response.raise_for_status()
                output = bytearray()
                async for chunk in response.aiter_bytes():
                    output.extend(chunk)
                    if len(output) > 65_536:
                        raise ValueError("Ollama response exceeds size limit")
        parsed = parse_json(output.decode("utf-8"), max_bytes=65_536)
        if not isinstance(parsed, dict) or parsed.get("done") is not True:
            raise ValueError("Ollama did not complete the response")
        message = parsed.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("Ollama response has no text content")
        counts = [parsed.get(key) for key in ("prompt_eval_count", "eval_count")]
        return ModelReply(message["content"], *(n if type(n) is int and n >= 0 else None for n in counts))
