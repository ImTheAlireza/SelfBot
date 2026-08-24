"""Shared async HTTP client.

Replaces the blocking ``requests`` calls that ran directly inside async
handlers. Keeping network I/O in one async client prevents a slow upstream
request from freezing the bot's event loop.

A single :class:`aiohttp.ClientSession` is reused process-wide, with retries
and exponential backoff for transient failures.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..errors import ProviderError

logger = logging.getLogger(__name__)

__all__ = ["HttpClient", "close_client", "get_client"]

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class HttpClient:
    """An aiohttp session with retries, timeouts and friendly errors."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 0.5,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self._timeout = timeout
        self._retries = max(0, retries)
        self._backoff = backoff
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self._timeout),
                        headers={"User-Agent": self._user_agent},
                        raise_for_status=False,
                    )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- core --------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """Perform a request with retries. The response body is preloaded."""
        session = await self.session()
        attempts = self._retries if retries is None else retries
        request_timeout = aiohttp.ClientTimeout(total=timeout or self._timeout)
        last_error: Exception | None = None

        for attempt in range(attempts + 1):
            try:
                response = await session.request(
                    method, url, timeout=request_timeout, **kwargs
                )
                await response.read()  # buffer before the context closes
                self._count("http_requests")

                if response.status in _RETRY_STATUSES and attempt < attempts:
                    delay = self._retry_delay(response, attempt)
                    logger.debug(
                        "HTTP %s from %s, retrying in %.1fs (%d/%d)",
                        response.status, url, delay, attempt + 1, attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status >= 500 or response.status in _RETRY_STATUSES:
                    self._record_failure(url, f"HTTP {response.status}", response.status)
                return response

            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt >= attempts:
                    self._record_failure(url, "timeout", None)
                    raise ProviderError(
                        f"Request to {_host(url)} timed out after {timeout or self._timeout:.0f}s."
                    ) from exc
            except aiohttp.ClientError as exc:
                last_error = exc
                if attempt >= attempts:
                    self._record_failure(url, type(exc).__name__, None)
                    raise ProviderError(
                        f"Could not reach {_host(url)}: {type(exc).__name__}"
                    ) from exc

            await asyncio.sleep(self._backoff * (2**attempt))

        raise ProviderError(f"Request to {_host(url)} failed: {last_error}")

    @staticmethod
    def _count(name: str) -> None:
        """Best-effort counter increment against the process-wide metrics."""
        try:
            from selfbot.bot import _current_metrics  # type: ignore[attr-defined]

            metrics = _current_metrics()
            if metrics is not None:
                metrics.incr(name)
        except Exception:
            pass

    @staticmethod
    def _record_failure(url: str, message: str, status: int | None) -> None:
        try:
            from selfbot.bot import _current_metrics  # type: ignore[attr-defined]

            metrics = _current_metrics()
            if metrics is not None:
                metrics.record_failure(_host(url), message, status=status)
                metrics.incr("http_failures")
        except Exception:
            pass

    def _retry_delay(self, response: aiohttp.ClientResponse, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), 30.0)
            except ValueError:
                pass
        return self._backoff * (2**attempt)

    # -- conveniences ------------------------------------------------------

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return await _json_or_raise(response, url)

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("POST", url, **kwargs)
        return await _json_or_raise(response, url)

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        response = await self.request("GET", url, **kwargs)
        if response.status >= 400:
            raise ProviderError(f"{_host(url)} returned HTTP {response.status}")
        return await response.read()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        if response.status >= 400:
            raise ProviderError(f"{_host(url)} returned HTTP {response.status}")
        return await response.text()

    async def download(
        self,
        url: str,
        destination: Any,
        *,
        max_bytes: int | None = None,
        progress: Any = None,
        chunk_size: int = 1 << 16,
        **kwargs: Any,
    ) -> int:
        """Stream a URL to disk, enforcing a size cap. Returns bytes written."""
        session = await self.session()
        async with session.get(url, **kwargs) as response:
            if response.status >= 400:
                raise ProviderError(f"{_host(url)} returned HTTP {response.status}")

            declared = response.content_length
            if max_bytes and declared and declared > max_bytes:
                raise ProviderError(
                    f"File is {declared / 1048576:.1f} MiB, over the "
                    f"{max_bytes / 1048576:.0f} MiB limit."
                )

            written = 0
            # Buffered 64 KiB writes to a local file are fast enough that a
            # thread hop per chunk would cost more than it saves.
            with open(destination, "wb") as sink:  # noqa: ASYNC230
                async for chunk in response.content.iter_chunked(chunk_size):
                    sink.write(chunk)
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ProviderError(
                            f"Download exceeded the {max_bytes / 1048576:.0f} MiB limit."
                        )
                    if progress is not None:
                        await progress(written, declared or 0)
            return written


async def _json_or_raise(response: aiohttp.ClientResponse, url: str) -> Any:
    if response.status >= 400:
        body = (await response.text())[:200]
        raise ProviderError(f"{_host(url)} returned HTTP {response.status}: {body}")
    try:
        return await response.json(content_type=None)
    except Exception as exc:
        body = (await response.text())[:200]
        raise ProviderError(f"{_host(url)} sent invalid JSON: {body}") from exc


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


_client: HttpClient | None = None


def get_client() -> HttpClient:
    """Return the process-wide HTTP client."""
    global _client
    if _client is None:
        _client = HttpClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
