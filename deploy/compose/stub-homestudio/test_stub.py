import httpx
import pytest

from app import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as http:
        yield http


ROUTES = [
    "/api/taste",
    "/api/history",
    "/api/ratings",
    "/api/now",
    "/api/queue",
    "/api/status",
]


@pytest.mark.parametrize("path", ROUTES)
async def test_envelope_on_every_route(client, path):
    response = await client.get(path)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "data", "error"}
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"] is not None


async def test_unknown_route_uses_the_error_envelope(client):
    response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "data": None, "error": "not found"}


async def test_fixture_shapes(client):
    now = (await client.get("/api/now")).json()["data"]
    assert now == {"state": "idle", "item": None, "position_s": 0.0, "duration_s": None,
                   "output_device": "Echo"}

    queue = (await client.get("/api/queue")).json()["data"]
    assert len(queue["items"]) == 2
    assert queue["remaining_s"] == sum(i["duration_s"] for i in queue["items"])
    assert {"id", "video_id", "title", "channel", "thumbnail_url", "duration_s", "source",
            "reason", "chapters"} <= set(queue["items"][0])

    taste = (await client.get("/api/taste")).json()["data"]
    assert set(taste) == {"summary", "likes", "dislikes", "updated_at"}

    history = (await client.get("/api/history")).json()["data"]["entries"]
    assert len(history) == 2
    assert history[0]["started_at"] > history[1]["started_at"]
    assert {"play_id", "video", "started_at", "seconds_listened", "end_reason", "source",
            "rating"} <= set(history[0])
    assert history[1]["rating"]["liked"] is True

    ratings = (await client.get("/api/ratings")).json()["data"]["ratings"]
    assert len(ratings) == 1
    assert set(ratings[0]) == {"id", "video_id", "title", "channel", "liked", "satisfaction",
                               "notes", "created_at"}


async def test_history_since_filters(client):
    everything = (await client.get("/api/history")).json()["data"]["entries"]
    newest = everything[0]["started_at"]

    after = await client.get("/api/history", params={"since": newest})
    assert [e["play_id"] for e in after.json()["data"]["entries"]] == [everything[0]["play_id"]]

    later = await client.get("/api/history", params={"since": "2026-12-01T00:00:00Z"})
    assert later.json()["data"]["entries"] == []

    bad = await client.get("/api/history", params={"since": "yesterday"})
    assert bad.status_code == 422
    assert bad.json()["error"] == "since: must be an ISO 8601 timestamp"


async def test_history_limit(client):
    response = await client.get("/api/history", params={"limit": 1})

    entries = response.json()["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["play_id"] == 18


async def test_ratings_limit_and_since(client):
    one = await client.get("/api/ratings", params={"limit": 1})
    assert len(one.json()["data"]["ratings"]) == 1

    for bad in (0, 201, "many"):
        response = await client.get("/api/ratings", params={"limit": bad})
        assert response.status_code == 422
        assert response.json()["ok"] is False

    later = await client.get("/api/ratings", params={"since": "2026-12-01T00:00:00+00:00"})
    assert later.json()["data"]["ratings"] == []
