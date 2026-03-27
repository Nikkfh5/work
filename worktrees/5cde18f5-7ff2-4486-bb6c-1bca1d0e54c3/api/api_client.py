"""Async HTTP client for JSONPlaceholder API."""

import httpx


class ApiClientError(Exception):
    """Base exception for ApiClient errors."""


class ApiClient:
    """Async HTTP client for JSONPlaceholder (https://jsonplaceholder.typicode.com)."""

    def __init__(
        self, base_url: str = "https://jsonplaceholder.typicode.com", timeout: int = 10
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = 3

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Execute HTTP request with retry on 5xx errors."""
        last_exc: Exception | None = None

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        ) as client:
            for attempt in range(self._max_retries):
                try:
                    response = await client.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code >= 500:
                        last_exc = exc
                        continue
                    raise ApiClientError(str(exc)) from exc
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    continue

        raise ApiClientError(str(last_exc)) from last_exc

    async def get_users(self) -> list[dict]:
        """Fetch all users."""
        response = await self._request("GET", "/users")
        return response.json()

    async def get_user_posts(self, user_id: int) -> list[dict]:
        """Fetch all posts for a given user."""
        response = await self._request("GET", "/posts", params={"userId": user_id})
        return response.json()

    async def create_post(self, user_id: int, title: str, body: str) -> dict:
        """Create a new post for a given user."""
        response = await self._request(
            "POST",
            "/posts",
            json={"userId": user_id, "title": title, "body": body},
        )
        return response.json()
