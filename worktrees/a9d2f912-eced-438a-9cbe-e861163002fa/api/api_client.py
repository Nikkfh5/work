"""Async HTTP client for JSONPlaceholder API."""

import httpx

_BASE_URL = "https://jsonplaceholder.typicode.com"
_TIMEOUT = 10.0
_MAX_RETRIES = 3
_RETRYABLE_MIN_STATUS = 500


class ApiClientError(Exception):
    """Base error for ApiClient."""


class ApiTimeoutError(ApiClientError):
    """Request timed out after retries."""


class ApiHTTPError(ApiClientError):
    """Non-retryable HTTP error."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class ApiClient:
    """Async client for JSONPlaceholder API with retry on 5xx errors."""

    def __init__(
        self,
        base_url: str = _BASE_URL,
        timeout: float = _TIMEOUT,
        max_retries: int = _MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
        )
        self._max_retries = max_retries

    async def __aenter__(self) -> "ApiClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Execute request with retry on 5xx errors."""
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise ApiTimeoutError(
                        f"Request timed out after {self._max_retries} attempts"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= _RETRYABLE_MIN_STATUS:
                    last_exc = exc
                    if attempt == self._max_retries:
                        raise ApiHTTPError(
                            exc.response.status_code,
                            f"Server error after {self._max_retries} attempts",
                        ) from exc
                else:
                    raise ApiHTTPError(
                        exc.response.status_code,
                        exc.response.text,
                    ) from exc
        # Should not reach here, but satisfy type checker
        raise ApiClientError("Unexpected retry exhaustion") from last_exc

    async def get_users(self) -> list[dict]:
        """Fetch all users."""
        response = await self._request("GET", "/users")
        return response.json()

    async def get_user_posts(self, user_id: int) -> list[dict]:
        """Fetch posts for a specific user."""
        response = await self._request("GET", "/posts", params={"userId": user_id})
        return response.json()

    async def create_post(self, user_id: int, title: str, body: str) -> dict:
        """Create a new post."""
        response = await self._request(
            "POST",
            "/posts",
            json={"userId": user_id, "title": title, "body": body},
        )
        return response.json()
