"""Bounded HTTP access for FF14 data sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from nanobot.security.network import PinnedDNSAsyncTransport, resolve_url_target

USER_AGENT = "nanobot-ffxiv/0.3 (+https://github.com/HKUDS/nanobot)"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class FetchError(RuntimeError):
    """A bounded or unsafe source request failed."""


class SafeHttpClient:
    """Fetch allowlisted sources with redirect, SSRF, and size guards."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_redirects: int = 5,
        inner_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._inner_transport = inner_transport

    @staticmethod
    def _normalize_host(host: str) -> str:
        try:
            return host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise FetchError("Invalid hostname") from exc

    def _validate_url(self, url: str, allowed_hosts: frozenset[str]) -> str:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise FetchError("Invalid URL") from exc

        allowed_schemes = {"https"}
        if self._inner_transport is not None:
            allowed_schemes.add("http")
        if parsed.scheme.lower() not in allowed_schemes:
            raise FetchError(f"URL scheme {parsed.scheme or 'missing'} is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise FetchError("URL credentials are not allowed")
        if parsed.hostname is None:
            raise FetchError("URL hostname is missing")

        hostname = self._normalize_host(parsed.hostname)
        normalized_allowed = {self._normalize_host(host) for host in allowed_hosts}
        if hostname not in normalized_allowed:
            raise FetchError(f"Host {hostname} is not allowed")

        ok, error, _resolved_ips = resolve_url_target(url)
        if not ok:
            raise FetchError(error or f"Unsafe URL for {hostname}")
        return hostname

    async def get_bytes(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int = DEFAULT_MAX_BYTES,
        etag: str | None = None,
    ) -> FetchResponse:
        if not allowed_hosts:
            raise FetchError("allowed_hosts must not be empty")
        if max_bytes <= 0:
            raise FetchError("max_bytes must be positive")

        transport = self._inner_transport or PinnedDNSAsyncTransport()
        headers = {"User-Agent": USER_AGENT}
        if etag is not None:
            headers["If-None-Match"] = etag

        current_url = url
        redirect_count = 0
        retry_count = 0
        async with httpx.AsyncClient(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            while True:
                hostname = self._validate_url(current_url, allowed_hosts)
                try:
                    async with client.stream("GET", current_url, headers=headers) as response:
                        response_headers = dict(response.headers.items())
                        if response.status_code == 304:
                            return FetchResponse(
                                url=str(response.url),
                                status_code=304,
                                headers=response_headers,
                                body=b"",
                            )
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError(
                                    f"HTTP {response.status_code} from {hostname}"
                                )
                            if redirect_count >= self._max_redirects:
                                raise FetchError(
                                    f"Too many redirects: exceeded limit of {self._max_redirects}"
                                )
                            current_url = urljoin(str(response.url), location)
                            redirect_count += 1
                            continue

                        if retry_count == 0 and response.status_code in {502, 503, 504}:
                            retry_count += 1
                            continue
                        if not 200 <= response.status_code < 300:
                            raise FetchError(f"HTTP {response.status_code} from {hostname}")

                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise FetchError(f"Response exceeds {max_bytes} bytes")
                            chunks.append(chunk)
                        return FetchResponse(
                            url=str(response.url),
                            status_code=response.status_code,
                            headers=response_headers,
                            body=b"".join(chunks),
                        )
                except FetchError:
                    raise
                except httpx.RequestError as exc:
                    # GET is idempotent. Retry one transient connection failure;
                    # retain redirect limits and revalidate the target each time.
                    if retry_count == 0 and isinstance(exc, httpx.TimeoutException | httpx.NetworkError):
                        retry_count += 1
                        continue
                    raise FetchError(
                        f"Request failed for {hostname}: {type(exc).__name__}"
                    ) from exc
