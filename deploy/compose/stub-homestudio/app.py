"""Fixture-only stand-in for the Home Studio read API (see its ``docs/api.md``).

Serves the six read endpoints the Home Studio MCP server calls, in the same envelope and
shapes, with fixed made-up data. It exists so ``docker compose up`` works without the real
server on the Pi.
"""

from datetime import datetime, timezone
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_GPU = {
    "video_id": "gpuMem00001",
    "title": "How GPU Memory Works",
    "channel": "Example Engineering",
    "thumbnail_url": None,
    "duration_s": 1834,
    "chapters": [{"title": "Intro", "start_s": 0.0}, {"title": "Caches", "start_s": 312.0}],
}
_BOARD = {
    "video_id": "boardTear01",
    "title": "Teardown of a Small Single-Board Computer",
    "channel": "Example Hardware",
    "thumbnail_url": None,
    "duration_s": 1260,
    "chapters": [],
}

_RATING = {
    "liked": True,
    "satisfaction": 8,
    "notes": "loved the cache section",
    "created_at": "2026-11-01T14:21:05.123456+00:00",
}

HISTORY: list[dict[str, Any]] = [  # newest first, as the real API returns them
    {
        "play_id": 18,
        "video": _BOARD,
        "started_at": "2026-11-01T15:10:41.000000+00:00",
        "seconds_listened": 1260.0,
        "end_reason": "finished",
        "source": "morning",
        "rating": None,
    },
    {
        "play_id": 17,
        "video": _GPU,
        "started_at": "2026-11-01T14:03:22.512345+00:00",
        "seconds_listened": 912.5,
        "end_reason": "skipped",
        "source": "agent",
        "rating": {k: _RATING[k] for k in ("liked", "satisfaction", "notes", "created_at")},
    },
]

RATINGS: list[dict[str, Any]] = [  # newest first
    {
        "id": 12,
        "video_id": _GPU["video_id"],
        "title": _GPU["title"],
        "channel": _GPU["channel"],
        **_RATING,
    },
]

NOW_PLAYING: dict[str, Any] = {
    "state": "idle",
    "item": None,
    "position_s": 0.0,
    "duration_s": None,
    "output_device": "Echo",
}

QUEUE_ITEMS: list[dict[str, Any]] = [
    {
        "id": 42,
        "video_id": "queueAlpha1",
        "title": "Why Latency Beats Bandwidth",
        "channel": "Example Engineering",
        "thumbnail_url": None,
        "duration_s": 1500,
        "source": "agent",
        "reason": "Deep dive on hardware, matches the taste profile",
        "chapters": [],
    },
    {
        "id": 43,
        "video_id": "queueBravo2",
        "title": "Reading a Datasheet",
        "channel": "Example Hardware",
        "thumbnail_url": None,
        "duration_s": 900,
        "source": "user",
        "reason": None,
        "chapters": [],
    },
]

TASTE: dict[str, Any] = {
    "summary": "Prefers long-form engineering deep dives",
    "likes": ["hardware teardowns", "long-form"],
    "dislikes": ["clickbait"],
    "updated_at": "2026-11-01T09:00:00+00:00",
}

STATUS: dict[str, Any] = {
    "player": "idle",
    "bluetooth": "connected",
    "cookies": "ok",
    "youtube_account": "connected",
    "claude": "ok",
    "source": "ok",
    "ytdlp_version": "stub",
    "quota_used_today": 0,
    "quota_ceiling": 9500,
    "events": [],
}


class QueryError(ValueError):
    """A query parameter was invalid; the message is shown to the caller."""


def _envelope(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "error": None}, status_code=status)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "data": None, "error": message}, status_code=status)


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        raise QueryError("limit: Input should be a valid integer") from None
    if not 1 <= value <= MAX_LIMIT:
        raise QueryError(f"limit: Input should be between 1 and {MAX_LIMIT}")
    return value


def _since(request: Request) -> str | None:
    """Return ``since`` as a UTC ISO string comparable with the fixture timestamps."""
    raw = request.query_params.get("since")
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise QueryError("since: must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _recent(rows: list[dict[str, Any]], key: str, request: Request) -> list[dict[str, Any]]:
    limit = _limit(request)
    since = _since(request)
    kept = [row for row in rows if since is None or row[key] >= since]
    return kept[:limit]


async def history(request: Request) -> JSONResponse:
    return _envelope({"entries": _recent(HISTORY, "started_at", request)})


async def ratings(request: Request) -> JSONResponse:
    return _envelope({"ratings": _recent(RATINGS, "created_at", request)})


async def now(request: Request) -> JSONResponse:
    return _envelope(NOW_PLAYING)


async def queue(request: Request) -> JSONResponse:
    remaining = sum(item["duration_s"] or 0 for item in QUEUE_ITEMS)
    return _envelope({"items": QUEUE_ITEMS, "remaining_s": remaining})


async def taste(request: Request) -> JSONResponse:
    return _envelope(TASTE)


async def status(request: Request) -> JSONResponse:
    return _envelope(STATUS)


async def query_error(request: Request, exc: Exception) -> JSONResponse:
    return _error(str(exc), 422)


async def http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    message = "not found" if exc.status_code == 404 else "method not allowed"
    return _error(message, exc.status_code)


app = Starlette(
    routes=[
        Route("/api/taste", taste, methods=["GET"]),
        Route("/api/history", history, methods=["GET"]),
        Route("/api/ratings", ratings, methods=["GET"]),
        Route("/api/now", now, methods=["GET"]),
        Route("/api/queue", queue, methods=["GET"]),
        Route("/api/status", status, methods=["GET"]),
    ],
    exception_handlers={QueryError: query_error, HTTPException: http_error},
)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8765)


if __name__ == "__main__":
    main()
