"""Offline SSRF and bounded-stream regression tests (stdlib unittest)."""
import asyncio
import os
import socket
import unittest
from unittest.mock import patch

import httpx

from app.services import safe_http


def dns(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


class URLTests(unittest.TestCase):
    def test_reject_unsafe_urls(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://@example.com", "https://example.com\\@evil.com", "https://example.com/\n", "https://[fe80::1%25eth0]"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_http.validate_public_https_url(url)

    def test_reject_any_non_public_dns_answer(self):
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "fc00::1", "fe80::1", "ff02::1", "::ffff:127.0.0.1", "2001:db8::1"):
            with self.subTest(ip=ip), patch.object(socket, "getaddrinfo", return_value=dns("8.8.8.8", ip)), self.assertRaises(ValueError):
                safe_http.validate_public_https_url("https://example.com")

    def test_accept_public_and_fail_closed_dns(self):
        with patch.object(socket, "getaddrinfo", return_value=dns("8.8.8.8", "2606:4700:4700::1111")):
            self.assertEqual(safe_http.validate_public_https_url("https://example.com").host, "example.com")
        for result in ([],):
            with patch.object(socket, "getaddrinfo", return_value=result), self.assertRaises(ValueError):
                safe_http.validate_public_https_url("https://example.com")
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror()), self.assertRaises(ValueError):
            safe_http.validate_public_https_url("https://example.com")


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 6
        yield b"x" * 6


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"ENABLE_EXTERNAL_PULLS": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_disabled_by_default_before_dns_or_http(self):
        for value in (None, "0", "true", ""):
            with patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["ENABLE_EXTERNAL_PULLS"] = value
                with patch.object(socket, "getaddrinfo") as resolver, patch.object(httpx, "AsyncClient") as client:
                    with self.assertRaisesRegex(ValueError, "本地加固版默认禁用"):
                        await safe_http.safe_fetch_json("https://example.com")
                    resolver.assert_not_called()
                    client.assert_not_called()

    async def fetch_with(self, handler, **kwargs):
        real_client = httpx.AsyncClient
        def factory(**options):
            self.assertFalse(options["follow_redirects"])
            self.assertFalse(options["trust_env"])
            self.assertEqual(options["timeout"], safe_http.REQUEST_TIMEOUT_SECONDS)
            return real_client(transport=httpx.MockTransport(handler), **options)
        with patch.object(socket, "getaddrinfo", return_value=dns("8.8.8.8")), patch.object(httpx, "AsyncClient", side_effect=factory):
            return await safe_http.safe_fetch_json("https://example.com", **kwargs)

    async def test_get_post_and_headers_not_mutated(self):
        headers = {}
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.content, b"{}")
            self.assertEqual(request.headers["content-type"], "application/json")
            return httpx.Response(200, json=[{"symbol": "000001.SZ"}])
        self.assertEqual(await self.fetch_with(handler, method="post", body="{}", headers=headers), [{"symbol": "000001.SZ"}])
        self.assertEqual(headers, {})
        self.assertEqual(await self.fetch_with(lambda r: httpx.Response(200, json=[])), [])

    async def test_redirect_errors_invalid_json_and_limits(self):
        handlers = [lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks()), lambda r: httpx.Response(302, headers={"location": "https://127.0.0.1"}), lambda r: httpx.Response(200, content=b"not json"), lambda r: httpx.Response(200, headers={"content-length": "11"}, content=b"[]"), lambda r: httpx.Response(200, stream=Chunks())]
        with patch.object(safe_http, "MAX_RESPONSE_BYTES", 10):
            for handler in handlers:
                with self.subTest(handler=handler), self.assertRaises(ValueError):
                    await self.fetch_with(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.fetch_with(lambda r: httpx.Response(500))

    async def test_method_host_and_total_timeout(self):
        for kwargs in ({"method": "DELETE"}, {"headers": {"Host": "localhost"}}):
            with self.assertRaises(ValueError):
                await safe_http.safe_fetch_json("https://example.com", **kwargs)
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=[])
        with patch.object(safe_http, "REQUEST_TIMEOUT_SECONDS", 0.01), self.assertRaises(asyncio.TimeoutError):
            await self.fetch_with(slow)


if __name__ == "__main__":
    unittest.main()
