"""JSON routes: the browser chat API (session cookie) and the OpenAI-compatible ``/v1`` (bearer)."""

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .agent import Agent
from .auth import bearer_ok
from .config import AssistantConfig, AssistantSecrets
from .memory import MemoryStore

MAX_MESSAGE_CHARS = 4000
TITLE_CHARS = 60
MESSAGES_LIMIT = 500
MAX_BODY_BYTES = 65536
MAX_ID = 2**63
_INVALID = object()  # body was not parseable JSON
_CHAT_KEYS = frozenset({"conversation_id", "message"})
_DIGITS = re.compile(r"[0-9]{1,18}")
SEED_LIMIT = 20
_SEED_ROLES = frozenset({"user", "assistant"})


def _error(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _valid_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= MAX_MESSAGE_CHARS:
        return value
    return None


def _title(text: str) -> str:
    return " ".join(text.split())[:TITLE_CHARS]


def _id_ok(cid: int) -> bool:
    """Ids SQLite can hold; anything else cannot name a stored conversation."""
    return 0 < cid < MAX_ID


async def _json_body(request: Request) -> Any:
    """The parsed JSON body; an error ``Response`` for a wrong type or size; else ``_INVALID``."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return _error("content type must be application/json", 415)
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error("request body too large", 413)
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return _INVALID


def _parse_chat(body: Any) -> tuple[int | None, str] | str:
    """``(conversation_id, message)`` or an error message."""
    if not isinstance(body, dict) or not set(body) <= _CHAT_KEYS or "message" not in body:
        return "body must be {conversation_id, message}"
    cid = body.get("conversation_id")
    if cid is not None and (not isinstance(cid, int) or isinstance(cid, bool)):
        return "conversation_id must be an integer or null"
    text = _valid_text(body["message"])
    if text is None:
        return f"message must be 1-{MAX_MESSAGE_CHARS} characters"
    return cid, text


def chat_routes(
    agent: Agent,
    memory: MemoryStore,
    session_ok: Callable[[Request], bool],
    origin_ok: Callable[[Request], bool],
) -> list[Route]:
    """``/api/chat`` and the history routes, behind ``session_ok`` (POSTs also ``origin_ok``)."""

    async def chat(request: Request) -> Response:
        if not session_ok(request):
            return _error("login required", 401)
        if not origin_ok(request):
            return _error("cross-origin request refused", 403)
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        parsed = _parse_chat(body)
        if isinstance(parsed, str):
            return _error(parsed, 422)
        cid, text = parsed
        if cid is None:
            cid = memory.new_conversation(_title(text))
        elif not _id_ok(cid) or memory.get_conversation(cid) is None:
            return _error("no such conversation", 404)
        reply = await agent.reply(cid, text)
        return JSONResponse(
            {
                "conversation_id": cid,
                "reply": reply.text,
                "offline": reply.offline,
                "tool_calls": list(reply.tool_calls_made),
                "step_capped": reply.step_capped,
            }
        )

    async def conversations(request: Request) -> Response:
        if not session_ok(request):
            return _error("login required", 401)
        rows = memory.list_conversations()
        items = [{"id": c.id, "created_at": c.created_at, "title": c.title} for c in rows]
        return JSONResponse({"conversations": items})

    async def messages(request: Request) -> Response:
        if not session_ok(request):
            return _error("login required", 401)
        cid = request.path_params["conversation_id"]
        if not _id_ok(cid) or memory.get_conversation(cid) is None:
            return _error("no such conversation", 404)
        rows = memory.recent_messages(cid, limit=MESSAGES_LIMIT)
        keys = ("role", "content", "tool_name", "created_at")
        return JSONResponse({"messages": [{k: getattr(m, k) for k in keys} for m in rows]})

    return [
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/conversations", conversations, methods=["GET"]),
        Route("/api/conversations/{conversation_id:int}/messages", messages, methods=["GET"]),
    ]


def _content_text(content: Any) -> str | None:
    """A message's text: a string, or the joined ``text`` parts of a content-part list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return None


def _last_user_index(messages: Any) -> int | None:
    """Index of the last ``user`` message, if ``messages`` is a list holding one."""
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return index
    return None


def _prior_turns(messages: list[Any]) -> list[tuple[str, str]]:
    """The request's earlier ``user``/``assistant`` texts (last ``SEED_LIMIT``), in order."""
    turns: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in _SEED_ROLES:
            continue
        text = _content_text(message.get("content"))
        if text and text.strip():
            turns.append((message["role"], text[:MAX_MESSAGE_CHARS]))
    return turns[-SEED_LIMIT:]


def _completion(model: str, text: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _seeded_conversation(memory: MemoryStore, text: str, prior: list[Any]) -> int:
    """A new conversation holding the request's own earlier turns, so the agent sees them."""
    cid = memory.new_conversation(_title(text))
    for role, content in _prior_turns(prior):
        memory.add_message(cid, role, content)
    return cid


def openai_routes(
    config: AssistantConfig, secrets: AssistantSecrets, agent: Agent, memory: MemoryStore
) -> list[Route]:
    """``GET /v1/models`` and non-streaming ``POST /v1/chat/completions`` behind a bearer token."""

    async def authorized(request: Request) -> bool:
        header = request.headers.get("authorization")  # PBKDF2 is slow: keep it off the loop
        return await anyio.to_thread.run_sync(bearer_ok, header, secrets.api_token_hashes)

    def unauthorized() -> Response:
        return _error("a valid bearer token is required", 401, {"WWW-Authenticate": "Bearer"})

    async def models(request: Request) -> Response:
        if not await authorized(request):
            return unauthorized()
        data = [{"id": config.model, "object": "model", "owned_by": "local"}]
        return JSONResponse({"object": "list", "data": data})

    async def completions(request: Request) -> Response:
        if not await authorized(request):
            return unauthorized()
        body = await _json_body(request)
        if isinstance(body, Response):
            return body
        if not isinstance(body, dict):
            return _error("body must be a JSON object", 400)
        if body.get("stream") is True:
            return _error("streaming is not supported", 400)
        messages = body.get("messages")
        last = _last_user_index(messages)
        text = None if last is None else _valid_text(_content_text(messages[last].get("content")))
        if text is None:
            return _error(f"the last user message must be 1-{MAX_MESSAGE_CHARS} characters", 400)
        user = body.get("user")
        if isinstance(user, str) and _DIGITS.fullmatch(user):
            cid = int(user)
            if memory.get_conversation(cid) is None:
                return _error("no such conversation", 404)
        else:
            cid = _seeded_conversation(memory, text, messages[:last])
        reply = await agent.reply(cid, text)
        return JSONResponse(_completion(config.model, reply.text))

    return [
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/chat/completions", completions, methods=["POST"]),
    ]
