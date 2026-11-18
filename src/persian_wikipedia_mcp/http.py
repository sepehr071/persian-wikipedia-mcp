"""Shared async HTTP client for fa.wikipedia.org (MediaWiki Action API and REST API).

Every tool goes through `fetch` (or `action`), which caps concurrency, caches answers briefly and turns HTTP
failures into `ToolError` messages the model can act on. Wikimedia asks for a descriptive User-Agent, so this
client does not pose as a browser.
"""

from __future__ import annotations

import asyncio
import copy
import json as json_lib
import os
import random
import time
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

SITE = "https://fa.wikipedia.org"
ACTION = f"{SITE}/w/api.php"
REST = f"{SITE}/api/rest_v1"

HEADERS = {
    "User-Agent": "persian-wikipedia-mcp/0.1 (+https://github.com/sepehr071/persian-wikipedia-mcp)",
    "Accept": "application/json",
    "Accept-Language": "fa",
}

MAX_CONCURRENCY = 2
JITTER = (0.05, 0.25)  # seconds before each uncached call
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
CACHE_TTL = 60  # seconds
CACHE_MAX = 128
RETRY_DELAY = 0.5  # seconds, doubled per retry
MAX_BODY = 5_000_000  # bytes; larger responses are refused
HOSTS = {"fa.wikipedia.org"}  # allowlist; redirects must stay on it

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_proxy_index = 0
_retired: set[asyncio.Task] = set()
_cache: dict[tuple, tuple[float, Any]] = {}


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit, _proxy_index
    _transport, _client, _limit, _proxy_index = transport, None, None, 0
    _cache.clear()


def _proxies() -> list[str]:
    """PERSIAN_WIKIPEDIA_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("PERSIAN_WIKIPEDIA_MCP_PROXY", "").split(",") if p.strip()]


def _next_proxy(client: httpx.AsyncClient) -> None:
    """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

    Only the client that failed may rotate. The old client is closed late because other requests may still use it.
    """
    global _client, _limit, _proxy_index
    if len(_proxies()) < 2 or client is not _client:
        return
    _client, _limit, _proxy_index = None, None, _proxy_index + 1
    t = asyncio.create_task(_close_later(client))
    _retired.add(t)
    t.add_done_callback(_retired.discard)


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(30)  # longer than the 20 s request timeout
    await client.aclose()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # System proxy settings are ignored; the proxy only comes from PERSIAN_WIKIPEDIA_MCP_PROXY.
        proxies = _proxies()
        _client = httpx.AsyncClient(
            transport=_transport,
            proxy=proxies[_proxy_index % len(proxies)] if proxies and _transport is None else None,
            headers=HEADERS,
            timeout=httpx.Timeout(20, connect=5),
            limits=httpx.Limits(max_connections=2 * MAX_CONCURRENCY),
            follow_redirects=True,
            max_redirects=3,
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


async def action(**params: Any) -> Any:
    """Call the MediaWiki Action API (JSON, formatversion 2); an API-level `error` becomes an ApiError."""
    body = await fetch(ACTION, {**params, "format": "json", "formatversion": 2})
    if isinstance(body, dict) and "error" in body:
        err = body["error"]
        if err.get("code") == "missingtitle":
            raise ApiError("No Persian Wikipedia article with that title. Try wiki_search.", 404)
        raise ApiError(f"Wikipedia API error: {err.get('info') or err.get('code')}", 400)
    return body


async def fetch(url: str, params: dict[str, Any] | None = None, cache: bool = True) -> Any:
    """GET an endpoint and return the parsed JSON body.

    Retried up to MAX_RETRIES times on timeout, network error, 5xx or 429, with jittered exponential
    backoff or after Retry-After when sent. 403 is never retried. `cache=False` skips the short cache (random).
    """
    query = dict(params or {})
    host = httpx.URL(url).host
    if host not in HOSTS:
        raise ApiError(f"Refusing to call unexpected host {host}.")
    key = (url, tuple(sorted(query.items())))
    if cache and (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL:
        return copy.deepcopy(hit[1])
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        wait = RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
        try:
            r, content = await _request(url, query, host)
        except ApiError as e:
            if last or e.status is not None:
                raise
            await asyncio.sleep(min(wait, MAX_RETRY_WAIT))
            continue
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            await asyncio.sleep(min(_retry_after(r) or wait, MAX_RETRY_WAIT))
            continue
        break

    if r.status_code >= 400:
        raise ApiError(_status_message(r, host), r.status_code)
    try:
        body = json_lib.loads(content)
    except ValueError as e:
        raise ApiError(f"{host} returned a non-JSON response (HTTP {r.status_code}).", r.status_code) from e
    if not cache:
        return body
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), copy.deepcopy(body))
    return body


async def _request(url, query, host) -> tuple[httpx.Response, bytes]:
    client, limit = _get_client()
    try:
        async with limit:
            await asyncio.sleep(random.uniform(*JITTER))
            async with client.stream("GET", url, params=query) as r:
                if r.url.host not in HOSTS:
                    raise ApiError(f"Refusing to follow a redirect to {r.url.host}.")
                content = await _read_capped(r, host)
    except httpx.TimeoutException as e:
        raise ApiError(f"{host} did not answer in time. Try again in a moment.") from e
    except httpx.RequestError as e:
        if isinstance(e, httpx.ConnectError | httpx.ProxyError):
            _next_proxy(client)
        raise ApiError(
            f"Could not reach {host} ({type(e).__name__}). Check the internet connection "
            "(or the PERSIAN_WIKIPEDIA_MCP_PROXY setting if you use one)."
        ) from e
    if r.status_code in (403, 429):
        _next_proxy(client)  # blocked: try the next proxy on the following call
    return r, content


async def _read_capped(r: httpx.Response, host: str) -> bytes:
    """Read the body, refusing anything over MAX_BODY so one huge answer cannot flood memory or context."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(f"{host} sent a response over {MAX_BODY // 1_000_000} MB; refused.", r.status_code)
    return bytes(buf)


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 429/503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code in (429, 503) and v.replace(".", "", 1).isdigit() else None


def _status_message(r: httpx.Response, host: str) -> str:
    code = r.status_code
    if code == 403:
        return f"{host} blocked the request (HTTP 403). Try another network or set PERSIAN_WIKIPEDIA_MCP_PROXY."
    if code == 404:
        return f"{host} found nothing at that address (HTTP 404). Check the title."
    if code == 429:
        return f"{host} is rate limiting requests (HTTP 429). Wait a minute before retrying."
    if code >= 500:
        return f"{host} had a server error (HTTP {code}). Try again in a moment."
    return f"{host} rejected the request (HTTP {code})."
