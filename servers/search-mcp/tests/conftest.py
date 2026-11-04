"""Shared fixtures: a SearxClient pointed at a fake base URL (respx mocks the transport)."""

import pytest

from pdl_search.searx import SearxClient

BASE_URL = "http://searx.example.com"


@pytest.fixture
async def client():
    c = SearxClient(BASE_URL)
    yield c
    await c.aclose()
