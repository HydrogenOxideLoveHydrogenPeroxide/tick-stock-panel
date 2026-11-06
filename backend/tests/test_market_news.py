"""Offline news/security regressions; optional RUN_LIVE_MARKET_NEWS=1 smoke."""
import asyncio
import json
import os
import socket
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx

from app.services import market_news as news

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=news.SHANGHAI)


def eastmoney(rows):
    return "tick_news(" + json.dumps({"code": 0, "result": {"cmsArticleWebOld": rows}}) + ");"


def row(**changes):
    value = {"title": "513100 纳指ETF国泰交易提示", "content": "公告摘要", "date": "2026-11-04 14:00:00", "url": "https://finance.example/news/1"}
    value.update(changes)
    return value


def sina(rows):
    return json.dumps({"result": {"status": {"code": 0}, "data": {"feed": {"list": rows}}}})


class ParsingTests(unittest.TestCase):
    def test_html_limits_sort_dedup_mentions(self):
        rows = [row(title="<b>513100</b> " + "标" * 250, content="&lt;b&gt;摘要&lt;/b&gt;<script>bad()</script>" + "文" * 400)]
        rows += [row(title=f"纳指ETF国泰新闻 {i}", date=f"2026-11-04 13:0{i}:00") for i in range(7)]
        rows.append(rows[0])
        items, warnings = news._parse(eastmoney(rows), "eastmoney", "513100", "纳指ETF国泰", NOW)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["mention"], "title")
        self.assertEqual(len(items[0]["title"]), 200)
        self.assertEqual(len(items[0]["summary"]), 300)
        self.assertNotIn("<", items[0]["title"] + items[0]["summary"])
        self.assertNotIn("bad", items[0]["summary"])
        self.assertEqual(items[0]["published_at"], "2026-11-04T14:00:00+08:00")
        self.assertFalse(warnings)
        item = news._parse(eastmoney([row(title="行业综述", content="其中纳指ETF国泰上涨")]), "eastmoney", "513100", "纳指ETF国泰", NOW)[0][0]
        self.assertEqual(item["mention"], "summary")

    def test_inline_highlight_preserves_real_name_and_nested_html_is_clean(self):
        items, _ = news._parse(eastmoney([row(title="<span>纳指</span>ETF国泰", content="&amp;lt;img src=x&amp;gt;摘要")]), "eastmoney", "513100", "纳指ETF国泰", NOW)
        self.assertEqual(items[0]["title"], "纳指ETF国泰")
        self.assertNotIn("<", items[0]["summary"])

    def test_market_and_partial_code_do_not_match(self):
        items, warnings = news._parse(eastmoney([row(title="美股市场", content="纳斯达克ETF走强"), row(title="15131000", content="")]), "eastmoney", "513100", "纳指ETF国泰", NOW)
        self.assertEqual(items, [])
        self.assertIn("不以市场快讯替代", " ".join(warnings))

    def test_strict_times_and_unsafe_article_urls(self):
        invalid_dates = ["2026-11-04", "11-04 14:00", "2026-11-04 16:00:00", "2026-02-30 12:00:00", 1793772000, "2026-11-04T08:00:00Z"]
        unsafe_urls = ["javascript:alert(1)", "data:text/html,x", "https://user:pass@example.org/", "https://@example.org/", "//example.org/", "https://example.org/\n", "https://example.org:bad/"]
        rows = [row(date=date) for date in invalid_dates] + [row(url=url) for url in unsafe_urls]
        items, warnings = news._parse(eastmoney(rows), "eastmoney", "513100", "", NOW)
        self.assertEqual(items, [])
        self.assertIn("无效或未来", " ".join(warnings))
        self.assertEqual(news._published("2026-11-04T06:00:00Z", NOW), "2026-11-04T14:00:00+08:00")
        self.assertEqual(news._article_url("http://example.org/"), "http://example.org/")

    def test_jsonp_never_executes_external_text(self):
        for text in ["alert(1)", "tick_news(__import__('os').system('echo bad'));", eastmoney([]) + "alert(1);", "other_callback({});"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                news._parse(text, "eastmoney", "513100", "", NOW)

    def test_sina_exact_stock_tag_and_name(self):
        rows = [{"rich_text": "【交易提示】ETF公告", "create_time": "2026-11-04 14:00:00", "ext": json.dumps({"stocks": [{"symbol": "sh513100"}]})},
                {"rich_text": "【纳指ETF国泰】交易提示", "create_time": "2026-11-04 13:00:00", "ext": {"docurl": "https://finance.example/a"}},
                {"rich_text": "【美股】纳斯达克指数上涨", "create_time": "2026-11-04 14:00:00"}]
        items, warnings = news._parse(sina(rows), "sina", "513100", "纳指ETF国泰", NOW)
        self.assertEqual([i["mention"] for i in items], ["direct", "title"])
        self.assertEqual(items[0]["url"], "https://finance.sina.com.cn/7x24/")
        self.assertTrue(warnings)


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"x" * 6
        yield b"x" * 6


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        news._cache.clear()
        news._gate = asyncio.Semaphore(1)
        news._rate_lock = asyncio.Lock()
        news._last_request = float("-inf")
        interval = patch.object(news, "MIN_REQUEST_INTERVAL_SECONDS", 0)
        interval.start()
        self.addCleanup(interval.stop)

    async def test_success_cache_copy_and_expiry(self):
        with patch.object(news, "_get_text", new=AsyncMock(return_value=eastmoney([row()]))) as fetch:
            first = await news.fetch_news_context("513100.SH", "纳指ETF国泰", NOW)
            self.assertEqual(set(first), {"items", "warnings", "source", "fetched_at"})
            self.assertEqual(first["source"], "eastmoney")
            first["items"].clear()
            second = await news.fetch_news_context("513100", "纳指ETF国泰", NOW)
            self.assertEqual(len(second["items"]), 1)
            self.assertIn("缓存", " ".join(second["warnings"]))
            self.assertEqual(fetch.await_count, 1)
            timestamp, cached = news._cache[("513100", "纳指ETF国泰")]
            news._cache[("513100", "纳指ETF国泰")] = (timestamp - 301, cached)
            await news.fetch_news_context("513100", "纳指ETF国泰", NOW)
            self.assertEqual(fetch.await_count, 2)
            params = json.loads(httpx.URL(fetch.call_args.args[0]).params["param"])
            self.assertEqual(params["keyword"], "513100")

    async def test_name_retry_then_sina_and_no_market_substitution(self):
        flash = {"rich_text": "【513100】交易提示", "create_time": "2026-11-04 14:00:00"}
        with patch.object(news, "_get_text", new=AsyncMock(side_effect=[ValueError("blocked"), eastmoney([]), sina([flash])])) as fetch:
            result = await news.fetch_news_context("513100", "纳指ETF国泰", NOW)
            self.assertEqual(result["source"], "sina")
            self.assertEqual(fetch.await_count, 3)
            self.assertIn("blocked", " ".join(result["warnings"]))
            self.assertEqual(json.loads(httpx.URL(fetch.call_args_list[1].args[0]).params["param"])["keyword"], "纳指ETF国泰")
        news._cache.clear()
        with patch.object(news, "_get_text", new=AsyncMock(side_effect=[eastmoney([]), sina([{**flash, "rich_text": "纳斯达克指数上涨"}])])):
            result = await news.fetch_news_context("513100", "ETF", NOW)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["source"], "none")
            self.assertIn("不以市场快讯替代", " ".join(result["warnings"]))

    async def test_invalid_input_and_all_failures_are_context_not_exception(self):
        with patch.object(news, "_get_text", new=AsyncMock(side_effect=ValueError("DNS failed"))) as fetch:
            invalid = await news.fetch_news_context("../etc/passwd", now=NOW)
            self.assertTrue(invalid["warnings"])
            fetch.assert_not_called()
            failed = await news.fetch_news_context("513100", now=NOW)
            self.assertEqual(failed["items"], [])
            self.assertEqual(fetch.await_count, 2)
            self.assertIn("DNS failed", " ".join(failed["warnings"]))

    async def test_concurrent_same_symbol_singleflight_and_bounded_cache(self):
        async def slow(*args):
            await asyncio.sleep(0.01)
            return eastmoney([row()])
        with patch.object(news, "_get_text", new=AsyncMock(side_effect=slow)) as fetch:
            results = await asyncio.gather(*(news.fetch_news_context("513100", now=NOW) for _ in range(10)))
            self.assertTrue(all(r["items"] for r in results))
            self.assertEqual(fetch.await_count, 1)
        with patch.object(news, "MAX_CACHE_ENTRIES", 2), patch.object(news, "_get_text", new=AsyncMock(return_value=eastmoney([]))):
            for code in ["000001", "000002", "000003"]:
                await news.fetch_news_context(code, now=NOW)
        self.assertEqual(len(news._cache), 2)

    async def request_with(self, handler):
        real_client = httpx.AsyncClient
        def factory(**options):
            self.assertFalse(options["trust_env"])
            self.assertFalse(options["follow_redirects"])
            self.assertEqual(options["timeout"], news.REQUEST_TIMEOUT_SECONDS)
            return real_client(transport=httpx.MockTransport(handler), **options)
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(socket, "getaddrinfo", return_value=addresses), patch.object(httpx, "AsyncClient", side_effect=factory):
            return await news._get_text(news._eastmoney_url("513100"), "https://so.eastmoney.com/")

    async def test_https_whitelist_before_dns(self):
        with patch.object(news, "validate_public_https_url") as dns:
            for url in ["http://search-api-web.eastmoney.com/search/jsonp", "https://evil.org/", news.EASTMONEY_URL + "/other", "https://u:p@search-api-web.eastmoney.com/search/jsonp"]:
                with self.assertRaises(ValueError):
                    await news._get_text(url, "")
            dns.assert_not_called()
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]), patch.object(httpx, "AsyncClient") as client:
            with self.assertRaisesRegex(ValueError, "non-public"):
                await news._get_text(news._eastmoney_url("513100"), "")
            client.assert_not_called()

    async def test_transport_limits_redirects_compression_and_identity(self):
        def good(request):
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(200, content=b"ok")
        self.assertEqual(await self.request_with(good), "ok")
        handlers = [lambda r: httpx.Response(302, headers={"location": "https://127.0.0.1"}),
                    lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks()),
                    lambda r: httpx.Response(200, headers={"content-length": "11"}, content=b"{}"),
                    lambda r: httpx.Response(200, stream=Chunks()),
                    lambda r: httpx.Response(200, headers={"content-length": "bad"}, content=b"{}")]
        with patch.object(news, "MAX_RESPONSE_BYTES", 10):
            for handler in handlers:
                with self.subTest(handler=handler), self.assertRaises(ValueError):
                    await self.request_with(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await self.request_with(lambda r: httpx.Response(503))

    async def test_dns_thread_and_total_deadline(self):
        def slow_dns(url):
            time.sleep(0.05)
            return httpx.URL(url)
        with patch.object(news, "REQUEST_TIMEOUT_SECONDS", 0.005), patch.object(news, "validate_public_https_url", side_effect=slow_dns), patch.object(httpx, "AsyncClient") as client:
            with self.assertRaises(asyncio.TimeoutError):
                await news._get_text(news._eastmoney_url("513100"), "")
            client.assert_not_called()
        async def slow_fetch(*args):
            await asyncio.sleep(1)
        with patch.object(news, "TOTAL_TIMEOUT_SECONDS", 0.01), patch.object(news, "_get_text", side_effect=slow_fetch):
            result = await news.fetch_news_context("513100", now=NOW)
            self.assertEqual(result["items"], [])
            self.assertIn("TimeoutError", " ".join(result["warnings"]))

    async def test_rate_spacing_between_requests(self):
        starts = []
        def validator(url):
            starts.append(time.monotonic())
            return httpx.URL(url)
        real_client = httpx.AsyncClient
        def factory(**options):
            return real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok")), **options)
        with patch.object(news, "MIN_REQUEST_INTERVAL_SECONDS", 0.02), patch.object(news, "validate_public_https_url", side_effect=validator), patch.object(httpx, "AsyncClient", side_effect=factory):
            await news._get_text(news._eastmoney_url("513100"), "")
            await news._get_text(news._eastmoney_url("513100"), "")
        self.assertGreaterEqual(starts[1] - starts[0], 0.018)

    async def test_cache_rechecks_future_against_current_clock(self):
        with patch.object(news, "_get_text", new=AsyncMock(return_value=eastmoney([row()]))):
            await news.fetch_news_context("513100", now=NOW)
            earlier = await news.fetch_news_context("513100", now=NOW - timedelta(hours=2))
            self.assertEqual(earlier["items"], [])


@unittest.skipUnless(os.environ.get("RUN_LIVE_MARKET_NEWS") == "1", "explicit optional live request only; no AI")
class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_513100_without_ai(self):
        news._cache.clear()
        news._gate = asyncio.Semaphore(1)
        news._rate_lock = asyncio.Lock()
        result = await news.fetch_news_context("513100", "纳指ETF国泰")
        self.assertEqual(set(result), {"items", "warnings", "source", "fetched_at"})
        self.assertLessEqual(len(result["items"]), 5)
        self.assertTrue(result["items"] or result["warnings"])
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
