"""Offline fixed-time market context and bounded HTTP regression tests."""
import asyncio
import json
import socket
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import httpx
import polars as pl

from app.services import market_sources as m

SYMBOL = "513100.SH"
DAY = date(2026, 11, 4)


def now(hour=19, minute=0):
    return m._at(DAY, hour, minute)


def bar(stamp, price="2", volume="100"):
    return [stamp, price, price, "3", "1", volume]


def minutes(stop=None):
    result = []
    for start, end in ((m._at(DAY, 9, 35), m._at(DAY, 11, 30)),
                       (m._at(DAY, 13, 5), m._at(DAY, 15, 0))):
        while start <= end:
            if stop is None or start <= stop:
                result.append(bar(start.strftime("%Y%m%d%H%M")))
            start += timedelta(minutes=5)
    return result


def quote_body(stamp, code="513100"):
    fields = [""] * 40
    for index, value in {1: "测试ETF", 2: code, 3: "2", 4: "2", 5: "2", 6: "100",
                         30: stamp.strftime("%Y%m%d%H%M%S"), 33: "3", 34: "1"}.items():
        fields[index] = value
    return ('v_sh513100="' + "~".join(fields) + '";').encode("gbk")


class Bytes(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        mid = len(self.body) // 2
        yield self.body[:mid]
        yield self.body[mid:]


class MarketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        m._cache.clear()
        self.real_client = httpx.AsyncClient
        self.requests = []

    async def call(self, mode="noon", items=None, clock=None, repo=None, quote_at=None, include_news=False):
        m._cache.clear()
        if items is None:
            items = [bar("202611041030"), bar("202611041130"),
                     bar("202611041400"), bar("202611041500")]
        def handler(request):
            self.requests.append(request)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            if request.url.host == "qt.gtimg.cn":
                body = quote_body(quote_at or now(15))
            else:
                param = parse_qs(request.url.query.decode())["param"][0]
                ticker, period = param.split(",")[:2]
                body = json.dumps({"data": {ticker: {period: items}}}).encode()
            return httpx.Response(200, stream=Bytes(body))
        def factory(**options):
            self.assertFalse(options["trust_env"])
            self.assertFalse(options["follow_redirects"])
            self.assertEqual(options["timeout"], m.REQUEST_TIMEOUT_SECONDS)
            return self.real_client(transport=httpx.MockTransport(handler), **options)
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), patch.object(httpx, "AsyncClient", side_effect=factory):
            result = await m.fetch_market_context(repo, SYMBOL, mode, now=clock or now(), include_news=include_news)
        json.dumps(result, allow_nan=False)
        return result

    async def test_noon_excludes_afternoon_and_aggregates(self):
        result = await self.call()
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["as_of"], now(11, 30).isoformat())
        self.assertFalse(result["snapshot"]["partial"])
        self.assertEqual(result["snapshot"]["volume"], 200)
        self.assertIsNone(result["quote"])
        self.assertEqual(result["adjust"], "none")
        self.assertEqual(set(result), {"mode", "symbol", "period", "as_of", "fetched_at", "source", "adjust", "rows", "snapshot", "quote", "news", "warnings"})

    async def test_noon_partial_missing_open_and_latest_required(self):
        result = await self.call(items=[bar("202611041130")])
        self.assertTrue(result["snapshot"]["partial"])
        self.assertEqual(result["snapshot"]["coverage_missing"], [now(10,30).isoformat()])
        self.assertIsNone(result["snapshot"]["volume"])
        self.assertTrue(any("未确认缺项是否停牌/源缺失" in w for w in result["warnings"]))
        for items in ([bar("202611041030")], [bar("202611031030"), bar("202611031130")], []):
            with self.subTest(items=items), self.assertRaisesRegex(ValueError, "缺失|覆盖"):
                await self.call(items=items)
        with self.assertRaisesRegex(ValueError, "尚未到11:30"):
            await self.call(clock=now(11, 20))

    async def test_minute_indicators_use_prior_bars_without_cutoff_leak(self):
        items=[bar("202611031500",price="1.5"),bar("202611041130"),bar("202611041400",price="2.5")]
        result=await self.call(items=items)
        indicators=result["snapshot"]["indicators"]
        self.assertEqual(indicators["available_bars"],2)
        self.assertEqual(indicators["as_of"],now(11,30).isoformat())
        tail=await self.call("tail",items=[bar("202611031500")]+minutes())
        self.assertEqual(tail["snapshot"]["indicators"]["available_bars"],47)
        self.assertEqual(tail["snapshot"]["indicators"]["as_of"],now(14,50).isoformat())

    async def test_tail_cutoff_after_close_omits_later_quote(self):
        result = await self.call("tail", items=minutes())
        self.assertEqual(result["as_of"], now(14, 50).isoformat())
        self.assertEqual(len(result["rows"]), 46)
        self.assertFalse(result["snapshot"]["partial"])
        self.assertIsNone(result["quote"])
        self.assertTrue(any("晚于模式截止" in w for w in result["warnings"]))

    async def test_tail_partial_uses_actual_cutoff(self):
        result = await self.call("tail", items=minutes(now(14, 45)), clock=now(14, 47), quote_at=now(14, 47))
        self.assertEqual(result["as_of"], now(14, 45).isoformat())
        self.assertTrue(result["snapshot"]["partial"])
        self.assertEqual(result["snapshot"]["requested_cutoff"], now(14, 47).isoformat())
        self.assertEqual(result["quote"]["as_of"], now(14, 47).isoformat())

    async def test_tail_gaps_stale_and_previous_day_rejected_before_quote(self):
        full = minutes(now(14, 50))
        for items in (full[:-3], full[:-3] + full[-2:], [bar("202611031450")]):
            self.requests = []
            with self.subTest(items=len(items)), self.assertRaisesRegex(ValueError, "缺失|过旧|覆盖"):
                await self.call("tail", items=items)
            self.assertFalse(any(r.url.host == "qt.gtimg.cn" for r in self.requests))

    async def test_tail_opening_missing_partial_and_delay_boundary(self):
        result = await self.call("tail", items=minutes()[12:])
        self.assertEqual(result["as_of"], now(14,50).isoformat())
        self.assertTrue(result["snapshot"]["partial"])
        self.assertEqual(len(result["snapshot"]["coverage_missing"]),12)
        self.assertEqual(result["snapshot"]["coverage_missing"][0],now(9,35).isoformat())
        self.assertIsNone(result["snapshot"]["volume"])
        delayed = await self.call("tail",items=minutes(now(14,40)))
        self.assertEqual(delayed["as_of"],now(14,40).isoformat())
        self.assertTrue(delayed["snapshot"]["partial"])
        self.assertEqual(len(delayed["snapshot"]["coverage_missing"]),2)

    async def test_bad_nonfinite_ohlc_volume_future_duplicate(self):
        bad = [bar("202611041030", price="nan"), bar("202611041030", price="inf"),
               bar("202611041030", price="4"), bar("202611041030", volume="-1"),
               bar("202611051030"), ["bad", "2"]]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                await self.call(items=[item, bar("202611041130")])
        with self.assertRaisesRegex(ValueError, "重复"):
            await self.call(items=[bar("202611041030")] * 2)

    async def test_morning_excludes_today_prefers_tencent_and_never_writes(self):
        frame = pl.DataFrame([{"date": DAY, "open": 9., "close": 9., "high": 10., "low": 8., "volume": 1.},
                              {"date": DAY - timedelta(days=1), "open": 1., "close": 1., "high": 2., "low": 1., "volume": 1.}])
        repo = Mock()
        repo.get_daily_asset.return_value = frame
        result = await self.call("morning", repo=repo, items=[bar("2026-11-03"), bar("2026-11-04")])
        self.assertEqual([r["date"] for r in result["rows"]], ["2026-11-03"])
        self.assertEqual(result["rows"][0]["close"], 2)
        self.assertEqual(result["as_of"], m._at(DAY - timedelta(days=1), 15, 0).isoformat())
        args = repo.get_daily_asset.call_args.args
        self.assertEqual(args[0], "etf")
        self.assertEqual(args[-1], DAY - timedelta(days=1))
        self.assertEqual(repo.method_calls, [repo.method_calls[0]])

    async def test_morning_only_homogeneous_tencent_latest_90(self):
        repo = Mock()
        repo.get_daily_asset.return_value = pl.DataFrame([{
            "date":date(2025,1,1),"open":99.,"close":99.,"high":100.,"low":98.,"volume":1.}])
        items = [bar((DAY-timedelta(days=i)).isoformat()) for i in range(1,121)]
        result = await self.call("morning",repo=repo,items=items)
        self.assertEqual(len(result["rows"]),90)
        self.assertEqual(result["adjust"],"none")
        self.assertEqual(result["rows"][0]["date"],(DAY-timedelta(days=90)).isoformat())
        self.assertTrue(all(row["close"]==2 for row in result["rows"]))
        self.assertNotIn("repo+",result["source"])
        self.assertEqual(result["snapshot"]["indicators"]["available_bars"],120)
        self.assertEqual(result["snapshot"]["indicators"]["as_of"],(DAY-timedelta(days=1)).isoformat())

    async def test_morning_stale_or_unverified_latest_rejected(self):
        repo = Mock()
        repo.get_daily_asset.return_value = pl.DataFrame()
        with self.assertRaisesRegex(ValueError, "超过7天"):
            await self.call("morning", repo=repo, items=[bar("2026-10-26")])
        repo.get_daily_asset.return_value = pl.DataFrame([{"date": DAY - timedelta(days=1), "open": 2., "close": 2., "high": 3., "low": 1., "volume": 1.}])
        with self.assertRaisesRegex(ValueError, "落后本地"):
            await self.call("morning", repo=repo, items=[bar("2026-11-02")])

    async def test_shanghai_day_not_utc_day(self):
        repo = Mock()
        repo.get_daily_asset.return_value = pl.DataFrame()
        clock = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
        result = await self.call("morning", repo=repo, items=[bar("2026-11-03"), bar("2026-11-04")], clock=clock)
        self.assertEqual(result["rows"][-1]["date"], "2026-11-03")

    async def test_quote_same_day_fresh_and_not_future(self):
        for stamp in (m._at(DAY-timedelta(days=1),14,50), now(14,40), now(14,51)):
            result = await self.call("tail", items=minutes(now(14,50)), quote_at=stamp)
            self.assertIsNone(result["quote"])
        with patch.object(m, "_fetch_bytes", return_value=quote_body(now(14,50),code="510300")), self.assertRaises(ValueError):
            await m._quote(SYMBOL, now(), now(14,50))

    async def test_symbols_modes_and_naive_now_rejected_before_network(self):
        with patch.object(m, "_fetch_bytes") as network:
            for symbol in ("513100", "513100.sh", "513100.BJ", "sh513100", "513100.SH/x", "１２３４５６.SH", "https://evil"):
                with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                    await m.fetch_market_context(None, symbol, "noon", now=now())
            for mode, clock in (("bad", now()), ("noon", datetime(2026,11,4))):
                with self.assertRaises(ValueError):
                    await m.fetch_market_context(None, SYMBOL, mode, now=clock)
            network.assert_not_called()

    async def test_news_cutoff_and_timestamp_filter(self):
        service = AsyncMock(return_value={"items": [
            {"published_at": now(11,59).isoformat(), "title": "valid"},
            {"published_at": now(12,1).isoformat(), "title": "future"},
            {"published_at": "bad", "title": "invalid"}], "warnings": ["新闻来源提示"]})
        module = types.ModuleType("app.services.market_news")
        module.fetch_news_context = service
        with patch.dict(sys.modules, {"app.services.market_news": module}):
            repo = Mock()
            repo.get_instruments_asset.return_value = pl.DataFrame({"symbol":[SYMBOL],"name":["纳指ETF国泰"]})
            result = await self.call(include_news=True,repo=repo)
        self.assertEqual([n["title"] for n in result["news"]], ["valid"])
        self.assertEqual(service.call_args.kwargs["now"], now())
        self.assertEqual(service.call_args.kwargs["name"], "纳指ETF国泰")
        self.assertIn("新闻来源提示", result["warnings"])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        m._cache.clear()

    async def test_fixed_hosts_paths_reject_before_dns(self):
        for url in ("http://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param=x", "https://evil/appstock/app/kline/mkline?param=x", "https://web.ifzq.gtimg.cn/other?param=x", "https://qt.gtimg.cn/q=sh513100&evil", "https://qt.gtimg.cn:443/q=sh513100", "https://user@qt.gtimg.cn/q=sh513100"):
            with self.subTest(url=url), patch.object(m,"validate_public_https_url") as dns, self.assertRaisesRegex(ValueError,"固定腾讯"):
                await m._fetch_bytes(url)
            dns.assert_not_called()

    async def fetch(self, response, limit=2097152):
        m._cache.clear()
        real = httpx.AsyncClient
        with patch.object(socket,"getaddrinfo",return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",443))]), patch.object(m,"MAX_RESPONSE_BYTES",limit), patch.object(httpx,"AsyncClient",side_effect=lambda **kw: real(transport=httpx.MockTransport(lambda request: response),**kw)):
            return await m._fetch_bytes("https://qt.gtimg.cn/q=sh513100")

    async def test_redirect_compression_declared_and_stream_limits(self):
        for response in (httpx.Response(302,headers={"location":"https://evil"}),
                         httpx.Response(200,headers={"content-encoding":"gzip"},stream=Bytes(b"abc")),
                         httpx.Response(200,headers={"content-length":"11"},stream=Bytes(b"x")),
                         httpx.Response(200,headers={"content-length":"bad"},stream=Bytes(b"x")),
                         httpx.Response(200,stream=Bytes(b"x"*11))):
            with self.subTest(response=response), self.assertRaises(ValueError):
                await self.fetch(response,limit=10)
        self.assertEqual(await self.fetch(httpx.Response(200,stream=Bytes(b"abc"))),b"abc")

    async def test_cache_limits_concurrency_expiry_and_mode_time_revalidation(self):
        real = httpx.AsyncClient
        requests = []
        active = 0
        maximum = 0
        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(.001)
            requests.append(request.url)
            active -= 1
            param = parse_qs(request.url.query.decode())["param"][0]
            ticker, period = param.split(",")[:2]
            items = minutes(now(14,50)) if period == "m5" else [bar("202611041030"),bar("202611041130")]
            return httpx.Response(200,stream=Bytes(json.dumps({"data":{ticker:{period:items}}}).encode()))
        with patch.object(socket,"getaddrinfo",return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("8.8.8.8",443))]), patch.object(httpx,"AsyncClient",side_effect=lambda **kw: real(transport=httpx.MockTransport(handler),**kw)):
            await asyncio.gather(*[m._kline(SYMBOL,"m60",now()) for _ in range(4)])
            self.assertEqual(len(requests),1)
            self.assertEqual(maximum,1)
            # A cached source is revalidated against an earlier clock; no future leak.
            with self.assertRaisesRegex(ValueError,"未来"):
                await m._kline(SYMBOL,"m60",now(10,30))
            await m._kline(SYMBOL,"m5",now())
            self.assertEqual(len(requests),2)
            await m._kline("510300.SH","m60",now())
            self.assertEqual(len(requests),3)
            with patch.object(m,"CACHE_TTL_SECONDS",0):
                await m._kline(SYMBOL,"m60",now())
            self.assertEqual(len(requests),4)
            with patch.object(m,"MAX_CACHE_ENTRIES",2):
                await m._kline("510500.SH","m60",now())
                self.assertLessEqual(len(m._cache),2)

    async def test_dns_failure_and_timeout_fail_closed(self):
        with patch.object(socket,"getaddrinfo",return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]), patch.object(httpx,"AsyncClient") as client, self.assertRaises(ValueError):
            await m._fetch_bytes("https://qt.gtimg.cn/q=sh513100")
        client.assert_not_called()
        m._cache.clear()
        def slow(url):
            import time
            time.sleep(.03)
            return httpx.URL(url)
        with patch.object(m,"REQUEST_TIMEOUT_SECONDS",.001), patch.object(m,"validate_public_https_url",side_effect=slow), self.assertRaisesRegex(ValueError,"超时"):
            await m._fetch_bytes("https://qt.gtimg.cn/q=sh513100")


if __name__ == "__main__":
    unittest.main()
