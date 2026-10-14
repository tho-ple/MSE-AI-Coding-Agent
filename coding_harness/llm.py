"""Model adapters: a real Ollama chat model, and a scripted model for repeatable tests."""

from __future__ import annotations

import itertools
import uuid
from typing import Any, Callable, Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from .config import ModelSettings


class ChatModel:
    """Minimal protocol the controller depends on: ``invoke(messages) -> AIMessage``."""

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:  # pragma: no cover - protocol
        raise NotImplementedError


def tool_call(name: str, args: Any = None, call_id: str | None = None) -> dict:
    return {"name": name, "args": {} if args is None else args, "id": call_id or f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}


def ai(*calls: dict, content: str = "") -> AIMessage:
    """Build a scripted assistant reply containing the given tool calls."""
    return AIMessage(content=content, tool_calls=list(calls))


Reply = AIMessage | Exception | Callable[[Sequence[BaseMessage]], AIMessage]


class ScriptedModel(ChatModel):
    """Returns pre-written replies in order. No network, no API key.

    A reply may be an ``AIMessage``, an ``Exception`` (raised, to test retries), or a
    callable that builds a reply from the conversation so far. With ``repeat_last=True``
    the final reply repeats forever (used to test the action limit).
    """

    def __init__(self, replies: Iterable[Reply], repeat_last: bool = False):
        replies = list(replies)
        self._it = itertools.chain(replies, itertools.repeat(replies[-1])) if repeat_last else iter(replies)
        self.calls: list[list[BaseMessage]] = []

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        try:
            reply = next(self._it)
        except StopIteration:
            raise RuntimeError("scripted model has no more replies") from None
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages)
        # Fresh ids so repeated replies don't collide in the message history.
        return AIMessage(content=reply.content,
                         tool_calls=[{**tc, "id": f"{tc.get('id') or 'call'}_{uuid.uuid4().hex[:6]}"} for tc in reply.tool_calls],
                         invalid_tool_calls=list(reply.invalid_tool_calls))


class LangChainModel(ChatModel):
    def __init__(self, chat_model, tools: list[dict]):
        self._model = chat_model.bind_tools(tools)

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self._model.invoke(list(messages))


def make_model(settings: ModelSettings, tools: list[dict]) -> ChatModel:
    if settings.provider == "ollama":
        from langchain_ollama import ChatOllama

        chat = ChatOllama(model=settings.name, base_url=settings.base_url,
                          temperature=settings.temperature, num_ctx=settings.num_ctx)
        return LangChainModel(chat, tools)
    raise ValueError(f"unknown model provider {settings.provider!r} (supported: ollama)")
