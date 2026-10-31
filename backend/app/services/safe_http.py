"""Bounded public-HTTPS pulls. DNS preflight is not connection IP pinning.

HTTPX resolves again at connect time: an egress firewall or a controlled proxy
that blocks non-public destinations is still required against DNS rebinding.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30


def validate_public_https_url(url: str) -> httpx.URL:
    """Reject ambiguous URLs and every non-public result, not just the first IP."""
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("URL contains invalid characters")
    try:
        target = httpx.URL(url)
        if target.scheme != "https" or not target.host:
            raise ValueError("Only HTTPS URLs with a hostname are allowed")
        if target.userinfo or urlsplit(url).username is not None:
            raise ValueError("URL userinfo is forbidden")
        if target.fragment or "%" in target.host:
            raise ValueError("URL fragments and scoped hosts are forbidden")
        port = target.port or 443
        addresses = socket.getaddrinfo(target.host, port, type=socket.SOCK_STREAM)
    except (httpx.InvalidURL, UnicodeError, OSError) as exc:
        raise ValueError("Invalid URL or DNS resolution failed") from exc
    if not addresses:
        raise ValueError("DNS returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified:
            raise ValueError("URL resolves to a non-public address")
        if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
            raise ValueError("IPv6 transition addresses are forbidden")
    return target


async def safe_fetch_json(
    url: str, method: str = "GET", headers: dict[str, str] | None = None,
    body: str | None = None,
) -> Any:
    """Fetch at most 10 MiB of decoded JSON without redirects or env proxies."""
    if os.environ.get("ENABLE_EXTERNAL_PULLS") != "1":
        raise ValueError("本地加固版默认禁用外部扩展拉取；仅在出口网络安全策略就绪后设置 ENABLE_EXTERNAL_PULLS=1")
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise ValueError("Only GET and POST are allowed")
    request_headers = dict(headers or {})
    if any(key.lower() == "host" for key in request_headers):
        raise ValueError("Custom Host header is forbidden")
    # Disable compression to prevent decompression bombs before the stream limit.
    request_headers = {k: v for k, v in request_headers.items() if k.lower() != "accept-encoding"}
    request_headers["Accept-Encoding"] = "identity"
    kwargs: dict[str, Any] = {"headers": request_headers}
    if method == "POST" and body:
        kwargs["content"] = body
        if "content-type" not in {key.lower() for key in request_headers}:
            request_headers["Content-Type"] = "application/json"

    async def fetch() -> Any:
        target = await asyncio.to_thread(validate_public_https_url, url)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, target, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("HTTP redirects are forbidden")
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise ValueError("Compressed responses are forbidden")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError as exc:
                        raise ValueError("Invalid Content-Length") from exc
                    if size < 0 or size > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeds the 10 MiB limit")
                payload = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeds the 10 MiB limit")
                    payload.extend(chunk)
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Response is not valid JSON") from exc

    return await asyncio.wait_for(fetch(), timeout=REQUEST_TIMEOUT_SECONDS)
