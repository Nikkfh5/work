"""Tests for ApiClient using respx to mock HTTP requests."""

import httpx
import pytest
import respx

from api_client import ApiClient, ApiClientError


FAKE_USERS = [
    {"id": 1, "name": "Alice", "email": "alice@example.com"},
    {"id": 2, "name": "Bob", "email": "bob@example.com"},
]

FAKE_POSTS = [
    {"userId": 1, "id": 1, "title": "Post 1", "body": "Body 1"},
    {"userId": 1, "id": 2, "title": "Post 2", "body": "Body 2"},
]

FAKE_CREATED_POST = {"userId": 1, "id": 101, "title": "New", "body": "Content"}

BASE_URL = "https://jsonplaceholder.typicode.com"


@pytest.fixture
def client() -> ApiClient:
    return ApiClient(base_url=BASE_URL, timeout=5)


# ── get_users ────────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_users_success(client: ApiClient):
    respx.get(f"{BASE_URL}/users").mock(
        return_value=httpx.Response(200, json=FAKE_USERS)
    )
    users = await client.get_users()
    assert users == FAKE_USERS
    assert len(users) == 2


# ── get_user_posts ───────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_get_user_posts_success(client: ApiClient):
    respx.get(f"{BASE_URL}/posts", params={"userId": 1}).mock(
        return_value=httpx.Response(200, json=FAKE_POSTS)
    )
    posts = await client.get_user_posts(user_id=1)
    assert posts == FAKE_POSTS
    assert all(p["userId"] == 1 for p in posts)


@respx.mock
@pytest.mark.asyncio
async def test_get_user_posts_empty(client: ApiClient):
    respx.get(f"{BASE_URL}/posts", params={"userId": 999}).mock(
        return_value=httpx.Response(200, json=[])
    )
    posts = await client.get_user_posts(user_id=999)
    assert posts == []


# ── create_post ──────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_create_post_success(client: ApiClient):
    respx.post(f"{BASE_URL}/posts").mock(
        return_value=httpx.Response(201, json=FAKE_CREATED_POST)
    )
    post = await client.create_post(user_id=1, title="New", body="Content")
    assert post == FAKE_CREATED_POST
    assert post["userId"] == 1


# ── retry on 5xx ─────────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(client: ApiClient):
    route = respx.get(f"{BASE_URL}/users")
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, json=FAKE_USERS),
    ]
    users = await client.get_users()
    assert users == FAKE_USERS
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhausted_on_500(client: ApiClient):
    respx.get(f"{BASE_URL}/users").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    with pytest.raises(ApiClientError) as exc_info:
        await client.get_users()
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
    assert exc_info.value.__cause__.response.status_code == 502


# ── 4xx errors are NOT retried ───────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_no_retry_on_404(client: ApiClient):
    route = respx.get(f"{BASE_URL}/users")
    route.mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(ApiClientError) as exc_info:
        await client.get_users()
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
    assert exc_info.value.__cause__.response.status_code == 404
    assert route.call_count == 1


# ── timeout handling ─────────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_timeout_retries_then_raises(client: ApiClient):
    route = respx.get(f"{BASE_URL}/users")
    route.side_effect = httpx.ConnectTimeout("Connection timed out")
    with pytest.raises(ApiClientError) as exc_info:
        await client.get_users()
    assert isinstance(exc_info.value.__cause__, httpx.TimeoutException)
    assert route.call_count == 3


@respx.mock
@pytest.mark.asyncio
async def test_timeout_then_success(client: ApiClient):
    route = respx.get(f"{BASE_URL}/users")
    route.side_effect = [
        httpx.ConnectTimeout("Connection timed out"),
        httpx.Response(200, json=FAKE_USERS),
    ]
    users = await client.get_users()
    assert users == FAKE_USERS
    assert route.call_count == 2


# ── custom base_url and timeout ──────────────────────────────────


def test_client_init_defaults():
    c = ApiClient()
    assert c._base_url == "https://jsonplaceholder.typicode.com"
    assert c._timeout == 10


def test_client_init_custom():
    c = ApiClient(base_url="https://example.com/", timeout=30)
    assert c._base_url == "https://example.com"
    assert c._timeout == 30
