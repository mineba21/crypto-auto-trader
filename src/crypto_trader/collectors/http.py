"""Async HTTP client with per-host rate limiting, bounded retries and explicit access-block detection.

Access blocks (HTTP 451, geo 403) are surfaced as ``AccessBlocked`` and never retried or worked around.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class AccessBlocked(ApiError):
    """Exchange refuses service from this network/region. Do not bypass; report BLOCKED."""


class RateLimited(ApiError):
    pass


class RateLimiter:
    """Simple async token bucket."""

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        self.rate = rate_per_sec
        self.capacity = burst or max(1, int(rate_per_sec))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def _is_geo_block(status: int, body: str) -> bool:
    if status == 451:
        return True
    low = body.lower()
    return status == 403 and ("country" in low or "restricted location" in low or "region" in low)


@dataclass
class CallStats:
    calls: int = 0
    errors: int = 0
    last_status: int | None = None
    last_error: str = ""
    last_ok_ts: float | None = None
    latencies_ms: list[float] = field(default_factory=list)


class HttpClient:
    def __init__(
        self,
        base_url: str,
        rate_per_sec: float = 5.0,
        timeout: float = 10.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rate_per_sec)
        self.max_retries = max_retries
        self.stats = CallStats()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", **(headers or {})},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Send a request. ``retry=False`` must be used for non-idempotent calls (order submit)."""
        attempts = self.max_retries if retry else 1
        delay = 0.5
        for attempt in range(1, attempts + 1):
            await self.limiter.acquire()
            t0 = time.monotonic()
            self.stats.calls += 1
            try:
                resp = await self._client.request(method, path, params=params, json=json, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.stats.errors += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                if attempt >= attempts:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
                continue
            self.stats.latencies_ms.append((time.monotonic() - t0) * 1000)
            del self.stats.latencies_ms[:-500]
            self.stats.last_status = resp.status_code
            if resp.status_code < 400:
                self.stats.last_ok_ts = time.time()
                if not resp.content:
                    return None
                return resp.json()
            body = resp.text
            self.stats.errors += 1
            self.stats.last_error = body[:300]
            url = str(resp.request.url)
            if _is_geo_block(resp.status_code, body):
                raise AccessBlocked(resp.status_code, body, url)
            if resp.status_code in (418, 429):
                if attempt >= attempts:
                    raise RateLimited(resp.status_code, body, url)
                retry_after = float(resp.headers.get("Retry-After", delay))
                await asyncio.sleep(max(retry_after, delay))
                delay *= 2
                continue
            if resp.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise ApiError(resp.status_code, body, url)
        raise RuntimeError("unreachable")

    async def get(self, path: str, params: Any = None, **kw: Any) -> Any:
        return await self.request("GET", path, params=params, **kw)
