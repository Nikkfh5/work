"""Tests for ApiClient — all HTTP requests are mocked, no real network calls."""

import json

import pytest
import httpx

from api_client import ApiClient, ApiClientError, ApiHTTPError, ApiTimeoutError

FAKE_USERS = [
    {"id": 1, "name": "Alice", "email": "alice@example.com"},
    {"id": 2, "name": "Bob", "email": "bob@example.com"},
]

FAKE_POSTS = [
    {"userId": 1, "id": 1, "title": "Post 1", "body": "Body 1"},
    {"userId": 1, "id": 2, "title": "Post 2", "body": "Body 2"},
]

CREATED_POST = {"userId": 1, "id": 101, "title": "New", "body": "Content"}


# ── helpers ──────────────────────────────────────────────────────────


class _FakeTransport(httpx.AsyncBaseTransport):
    """Transport that returns canned responses based on registered routes."""

    def __init__(self):
        self._routes: list[tuple] = []
        self._call_count: dict[str, int] = {}
        self._requests: list[httpx.Request] = []

    def add(self, method: str, path: str, *, status: int = 200, json=None, exc=None):
        self._routes.append((method.upper(), path, status, json, exc))
        return self

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._requests.append(request)
        key = f"{request.method} {request.url.raw_path.decode()}"
        self._call_count[key] = self._call_count.get(key, 0) + 1

        for method, path, status, json_body, exc in self._routes:
            raw = request.url.raw_path.decode().split("?")[0]
            if request.method == method and raw == path:
                if exc is not None:
                    raise exc
                body = b"" if json_body is None else json.dumps(json_body).encode()
                return httpx.Response(
                    status_code=status,
                    content=body,
                    headers={"content-type": "application/json"},
                )

        return httpx.Response(status_code=404, content=b"Not Found")

    def count(self, method: str, path: str) -> int:
        total = 0
        for key, cnt in self._call_count.items():
            m, p = key.split(" ", 1)
            if m == method.upper() and p.split("?")[0] == path:
                total += cnt
        return total


def _make_client(transport: _FakeTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        base_url="https://jsonplaceholder.typicode.com",
        timeout=httpx.Timeout(1.0),
    )


# ── get_users ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_users_success():
    t = _FakeTransport().add("GET", "/users", json=FAKE_USERS)
    async with ApiClient(client=_make_client(t)) as api:
        users = await api.get_users()
    assert users == FAKE_USERS
    assert len(users) == 2


@pytest.mark.asyncio
async def test_get_users_empty():
    t = _FakeTransport().add("GET", "/users", json=[])
    async with ApiClient(client=_make_client(t)) as api:
        assert await api.get_users() == []


# ── get_user_posts ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_posts_success():
    t = _FakeTransport().add("GET", "/posts", json=FAKE_POSTS)
    async with ApiClient(client=_make_client(t)) as api:
        posts = await api.get_user_posts(user_id=1)
    assert posts == FAKE_POSTS
    # Verify that userId query parameter was actually sent
    assert len(t._requests) == 1
    assert t._requests[0].url.params["userId"] == "1"


@pytest.mark.asyncio
async def test_get_user_posts_empty():
    t = _FakeTransport().add("GET", "/posts", json=[])
    async with ApiClient(client=_make_client(t)) as api:
        assert await api.get_user_posts(user_id=999) == []


# ── create_post ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_post_success():
    t = _FakeTransport().add("POST", "/posts", status=201, json=CREATED_POST)
    async with ApiClient(client=_make_client(t)) as api:
        result = await api.create_post(user_id=1, title="New", body="Content")
    assert result["id"] == 101
    assert result["userId"] == 1
    assert result["title"] == "New"


# ── retry on 5xx ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_on_500_eventually_succeeds():
    """First 2 calls return 500, third succeeds."""
    call_count = 0

    class _RetryTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(
                    500,
                    content=b"Server Error",
                    headers={"content-type": "text/plain"},
                    request=request,
                )
            body = json.dumps(FAKE_USERS).encode()
            return httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )

    client = httpx.AsyncClient(
        transport=_RetryTransport(),
        base_url="https://jsonplaceholder.typicode.com",
        timeout=httpx.Timeout(1.0),
    )
    async with ApiClient(client=client, max_retries=3) as api:
        users = await api.get_users()
    assert users == FAKE_USERS
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_exhausted_on_500():
    """All 3 attempts return 500 → ApiHTTPError."""
    t = _FakeTransport().add("GET", "/users", status=500)
    async with ApiClient(client=_make_client(t), max_retries=3) as api:
        with pytest.raises(ApiHTTPError) as exc_info:
            await api.get_users()
    assert exc_info.value.status_code == 500


# ── no retry on 4xx ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_retry_on_404():
    """4xx errors are NOT retried — fail immediately."""
    call_count = 0

    class _CountTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                404,
                content=b"Not Found",
                headers={"content-type": "text/plain"},
                request=request,
            )

    client = httpx.AsyncClient(
        transport=_CountTransport(),
        base_url="https://jsonplaceholder.typicode.com",
        timeout=httpx.Timeout(1.0),
    )
    async with ApiClient(client=client, max_retries=3) as api:
        with pytest.raises(ApiHTTPError) as exc_info:
            await api.get_users()
    assert exc_info.value.status_code == 404
    assert call_count == 1  # no retries


# ── timeout handling ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_raises_api_timeout_error():
    t = _FakeTransport().add("GET", "/users", exc=httpx.ConnectTimeout("timeout"))
    async with ApiClient(client=_make_client(t), max_retries=2) as api:
        with pytest.raises(ApiTimeoutError, match="timed out"):
            await api.get_users()


@pytest.mark.asyncio
async def test_timeout_retries_then_succeeds():
    """First call times out, second succeeds."""
    call_count = 0

    class _TimeoutThenOk(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadTimeout("timeout")
            body = json.dumps(FAKE_USERS).encode()
            return httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )

    client = httpx.AsyncClient(
        transport=_TimeoutThenOk(),
        base_url="https://jsonplaceholder.typicode.com",
        timeout=httpx.Timeout(1.0),
    )
    async with ApiClient(client=client, max_retries=3) as api:
        users = await api.get_users()
    assert users == FAKE_USERS
    assert call_count == 2


# ── context manager ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager_closes_owned_client():
    """When ApiClient creates its own client, close() shuts it down."""
    api = ApiClient(base_url="https://jsonplaceholder.typicode.com", timeout=1.0)
    await api.close()
    assert api._client.is_closed


@pytest.mark.asyncio
async def test_context_manager_does_not_close_injected_client():
    """When an external client is injected, ApiClient does NOT close it."""
    t = _FakeTransport().add("GET", "/users", json=[])
    ext_client = _make_client(t)
    async with ApiClient(client=ext_client) as api:
        await api.get_users()
    # ext_client should still be open
    assert not ext_client.is_closed
    await ext_client.aclose()


# ── error hierarchy ──────────────────────────────────────────────────


def test_error_hierarchy():
    assert issubclass(ApiTimeoutError, ApiClientError)
    assert issubclass(ApiHTTPError, ApiClientError)


def test_http_error_attributes():
    err = ApiHTTPError(422, "Unprocessable")
    assert err.status_code == 422
    assert "422" in str(err)
