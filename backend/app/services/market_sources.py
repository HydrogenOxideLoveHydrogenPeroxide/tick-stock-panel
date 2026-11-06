"""Read-only, bounded Tencent market contexts; never substitute daily bars intraday.

Parameter/field parsing adapted from quant-rsi/quant/data/_sources/tencent.py.
Only pure parsing is copied: its unbounded urllib transport is NOT imported.
DNS preflight is not IP pinning; safe_http's egress/rebinding caveat still applies.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time as monotonic_time
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.services.safe_http import validate_public_https_url
from app.services.market_metrics import price_metrics

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 12
_HOSTS = ("web.ifzq.gtimg.cn", "ifzq.gtimg.cn")
_DAY_PATH = "/appstock/app/fqkline/get"
_MINUTE_PATH = "/appstock/app/kline/mkline"
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ)\Z")
CACHE_TTL_SECONDS = 30
MAX_CACHE_ENTRIES = 64
_cache: OrderedDict = OrderedDict()
_fetch_gate = asyncio.Semaphore(1)
# Cache only raw bounded payloads, never validated contexts: every call rechecks
# its own mode/date/cutoff/freshness, so caching cannot bypass time boundaries.
# No quant/OpenBB install is required.


def _ticker(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("证券代码必须为六位数字.SH或.SZ（仅沪深股票/ETF）")
    return symbol[-2:].lower() + symbol[:6]


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now必须是带时区的时间")
    return now.astimezone(SHANGHAI)


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), SHANGHAI)


async def _fetch_bytes(url: str) -> bytes:
    """No arbitrary hosts/paths, env proxies, redirects, or decoded body inflation."""
    parts = urlsplit(url)
    allowed = (parts.hostname in _HOSTS and parts.path in {_DAY_PATH, _MINUTE_PATH}
               and re.fullmatch(r"param=[a-zA-Z0-9%,._-]+", parts.query))
    allowed = allowed or (parts.hostname == "qt.gtimg.cn"
                          and re.fullmatch(r"/q=(sh|sz)[0-9]{6}", parts.path)
                          and not parts.query)
    if (not allowed or parts.scheme != "https" or parts.netloc != parts.hostname
            or parts.fragment):
        raise ValueError("仅允许固定腾讯HTTPS行情主机和路径")

    async def fetch() -> bytes:
        try:
            target = await asyncio.to_thread(validate_public_https_url, url)
        except ValueError as exc:
            raise ValueError("腾讯行情DNS安全预检失败") from exc
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("GET", target, headers={
                "Accept-Encoding": "identity", "Referer": "https://gu.qq.com/",
            }) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("腾讯行情禁止重定向")
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise ValueError("腾讯行情禁止压缩响应")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError as exc:
                        raise ValueError("腾讯行情响应长度非法") from exc
                    if size < 0 or size > MAX_RESPONSE_BYTES:
                        raise ValueError("腾讯行情响应超过2MB上限")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("腾讯行情响应超过2MB上限")
                    body.extend(chunk)
                return bytes(body)
    async def cached_fetch() -> bytes:
        async with _fetch_gate:
            entry = _cache.get(url)
            if entry and monotonic_time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
                _cache.move_to_end(url)
                if isinstance(entry[1], Exception):
                    raise ValueError(str(entry[1]))
                return entry[1]
            try:
                body = await fetch()
            except (ValueError, httpx.HTTPError, OSError) as exc:
                body = ValueError(f"腾讯行情请求失败：{exc}")
            _cache[url] = (monotonic_time.monotonic(), body)
            _cache.move_to_end(url)
            while len(_cache) > MAX_CACHE_ENTRIES:
                _cache.popitem(last=False)
            if isinstance(body, Exception):
                raise body
            return body
    try:
        return await asyncio.wait_for(cached_fetch(), REQUEST_TIMEOUT_SECONDS)
    except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        raise ValueError("腾讯行情网络请求失败或超时") from exc


def _number(value, label: str, positive: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"行情{label}不是有效数值") from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError(f"行情{label}非有限或范围非法")
    return result


def _ohlcv(item: dict) -> dict:
    result = {key: _number(item.get(key), key, True) for key in ("open", "high", "low", "close")}
    result["volume"] = _number(item.get("volume"), "volume")
    if result["low"] > min(result["open"], result["close"]) or result["high"] < max(result["open"], result["close"]) or result["low"] > result["high"]:
        raise ValueError("行情OHLC高低范围矛盾")
    return result


def _parse_rows(items: list, period: str, now: datetime) -> list[dict]:
    rows, seen = [], set()
    if not isinstance(items, list):
        raise ValueError("腾讯行情缺少K线列表")
    for item in items:
        try:
            pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}" if period == "day" else r"[0-9]{12}"
            if not isinstance(item, list) or len(item) < 6 or not isinstance(item[0], str) or not re.fullmatch(pattern, item[0]):
                raise ValueError()
            stamp = datetime.strptime(item[0], "%Y-%m-%d" if period == "day" else "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
            values = dict(zip(("open", "close", "high", "low", "volume"), item[1:6]))
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("腾讯K线格式或时间非法") from exc
        if stamp > now:
            raise ValueError("腾讯K线包含未来数据")
        if stamp in seen:
            raise ValueError("腾讯K线包含重复时间")
        seen.add(stamp)
        row = _ohlcv(values)
        row["date"] = stamp.date().isoformat() if period == "day" else stamp.isoformat()
        rows.append(row)
    return sorted(rows, key=lambda row: row["date"])


async def _kline(symbol: str, period: str, now: datetime) -> tuple[list[dict], str]:
    ticker = _ticker(symbol)
    path = _DAY_PATH if period == "day" else _MINUTE_PATH
    param = f"{ticker},day,,,320," if period == "day" else f"{ticker},{period},,320"
    errors = []
    for host in _HOSTS:
        url = f"https://{host}{path}?param={quote(param, safe='')}"
        try:
            body = await _fetch_bytes(url)
            payload = json.loads(body)
            node = payload.get("data", {}).get(ticker, {})
            items = node.get(period)
        except (ValueError, AttributeError, TypeError) as exc:
            errors.append(str(exc))
            continue
        # Data integrity errors do not silently fail over to another payload.
        rows = _parse_rows(items, period, now)
        if not rows:
            raise ValueError("腾讯行情缺失K线，不能用旧日K替代盘中")
        return rows, url
    raise ValueError("腾讯行情不可用：" + "; ".join(errors))


async def _quote(symbol: str, now: datetime, cutoff: datetime) -> dict:
    ticker = _ticker(symbol)
    url = f"https://qt.gtimg.cn/q={ticker}"
    raw = (await _fetch_bytes(url)).decode("gbk", "strict")
    match = re.fullmatch(r'\s*v_' + ticker + r'="([^"\r\n]*)";\s*', raw)
    if not match:
        raise ValueError("腾讯快照代码或格式非法")
    fields = match[1].split("~")
    try:
        if fields[2] != symbol[:6]:
            raise ValueError()
        stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        values = {"open": fields[5], "close": fields[3], "high": fields[33],
                  "low": fields[34], "volume": fields[6]}
        result = _ohlcv(values)
        result.update(name=fields[1], price=result["close"],
                      prev_close=_number(fields[4], "prev_close", True))
    except (ValueError, IndexError) as exc:
        raise ValueError("腾讯快照字段或来源时间非法") from exc
    if stamp.date() != now.date() or stamp > now or stamp > cutoff:
        raise ValueError("腾讯快照不同日或晚于模式截止时间，已省略")
    if cutoff - stamp >= timedelta(minutes=10):
        raise ValueError("腾讯快照超过10分钟，已省略")
    result.update(as_of=stamp.isoformat(), source=url, adjust="none")
    return result


def _snapshot(rows: list[dict], cutoff: datetime, period_end: datetime) -> dict:
    return {"open": rows[0]["open"], "close": rows[-1]["close"],
            "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows),
            "volume": sum(row["volume"] for row in rows),
            "as_of": rows[-1]["date"], "requested_cutoff": cutoff.isoformat(),
            "period_end": period_end.isoformat(),
            "partial": rows[-1]["date"] != period_end.isoformat(),
            "volume_unit": "source_raw_unverified", "amount": None}


async def fetch_market_context(repo, symbol, mode, include_news=False, now=None) -> dict:
    """JSON-safe context; now must be timezone-aware. Does not write repository."""
    _ticker(symbol)
    if mode not in {"morning", "noon", "tail"}:
        raise ValueError("mode必须为morning/noon/tail")
    clock = _clock(now)
    today = clock.date()
    warnings = ["成交量/金额单位尚未核验；不计算量比或跨来源量价指标"]
    period = {"morning": "day", "noon": "m60", "tail": "m5"}[mode]
    quote_data = None
    asset_type = "etf" if symbol.startswith(("5", "15", "16")) else "stock"
    if mode == "morning":
        end = today - timedelta(days=1)
        try:
            frame = await asyncio.to_thread(repo.get_daily_asset, asset_type, symbol,
                                             today - timedelta(days=365), end)
            records = frame.to_dicts()
        except Exception as exc:
            records = []
            warnings.append(f"本地日K读取失败（{type(exc).__name__}），采用腾讯验证数据")
        local = {}
        for item in records:
            day = item["date"].date() if isinstance(item["date"], datetime) else item["date"]
            if isinstance(day, str):
                day = date.fromisoformat(day)
            if day >= today:
                continue
            key = day.isoformat()
            if key in local:
                raise ValueError("本地日K包含重复日期")
            local[key] = dict(date=key, **_ohlcv(item))
        remote, source = await _kline(symbol, "day", clock)
        remote = [row for row in remote if row["date"] < today.isoformat()]
        if not remote or today - date.fromisoformat(remote[-1]["date"]) > timedelta(days=7):
            raise ValueError("上一交易日日K缺失或超过7天，不能采用旧行情")
        if local and max(local) > remote[-1]["date"]:
            raise ValueError("腾讯最近日K落后本地，无法验证上一交易日")
        # Repo adjustment metadata is unknown: validate its latest date only.
        # Return a homogeneous unadjusted Tencent window, never mixed OHLC.
        rows = remote[-90:]
        adjust = "none"
        warnings.append("本地日K仅作最新日期校验；分析窗口为腾讯最近最多90根未复权日K，未混合本地口径")
        warnings.append("上一交易日取腾讯最近已完成日K；未接入交易日历，节假日未独立核验")
        actual = _at(date.fromisoformat(rows[-1]["date"]), 15, 0)
        cutoff = clock
        snapshot = dict(rows[-1], as_of=actual.isoformat(), partial=False,
                        period_end=actual.isoformat(), volume_unit="source_raw_unverified", amount=None)
        snapshot["indicators"] = price_metrics(remote, period)
    else:
        period_end = _at(today, 11, 30) if mode == "noon" else _at(today, 14, 50)
        cutoff = min(clock, period_end)
        if mode == "noon" and clock < period_end:
            raise ValueError("午间行情尚未到11:30，上午两根60分钟K线未收齐")
        all_rows, source = await _kline(symbol, period, clock)
        rows = [row for row in all_rows if datetime.fromisoformat(row["date"]).date() == today
                and datetime.fromisoformat(row["date"]) <= cutoff]
        if mode == "noon":
            wanted = {_at(today, 10, 30), _at(today, 11, 30)}
            rows = [row for row in rows if datetime.fromisoformat(row["date"]) in wanted]
        else:
            wanted = set()
            for begin, stop in ((_at(today, 9, 35), _at(today, 11, 30)),
                                (_at(today, 13, 5), period_end)):
                while begin <= min(stop, cutoff):
                    wanted.add(begin)
                    begin += timedelta(minutes=5)
            rows = [row for row in rows if datetime.fromisoformat(row["date"]) in wanted]
        got = {datetime.fromisoformat(row["date"]) for row in rows}
        if not wanted or not rows or (mode == "noon" and period_end not in got):
            raise ValueError("当日分钟行情缺失或未覆盖11:30；禁止用旧日K/快照替代")
        actual = datetime.fromisoformat(rows[-1]["date"])
        # During lunch freshness is measured at the last trading-minute cutoff.
        trading_cutoff = min(cutoff, _at(today, 11, 30)) if cutoff < _at(today, 13, 5) else cutoff
        if trading_cutoff - actual > timedelta(minutes=10):
            raise ValueError("当日分钟行情过旧（超过10分钟），不能采用旧行情")
        if mode == "tail":
            recent = {stamp for stamp in wanted
                      if trading_cutoff - timedelta(minutes=60) < stamp <= actual}
            if not recent.issubset(got):
                raise ValueError("当日最近60分钟K线覆盖不全；禁止用快照掩盖分钟缺失")
        missing = sorted(stamp.isoformat() for stamp in wanted - got)
        snapshot = _snapshot(rows, cutoff, period_end)
        history = [row for row in all_rows if datetime.fromisoformat(row["date"]) <= actual]
        snapshot["indicators"] = price_metrics(history, period)
        snapshot.update(coverage_missing=missing, coverage_scope="returned_records_only",
                        coverage_complete=not missing, partial=snapshot["partial"] or bool(missing))
        adjust = "none"
        if missing:
            window = "上午" if mode == "noon" else "当日"
            warnings.append(f"{window}窗口不完整，只分析已返回记录；未确认缺项是否停牌/源缺失")
            warnings.append("未计算全天/完整窗口成交量或涨幅；快照OHLC仅代表已返回记录")
            snapshot["volume_observed"] = snapshot["volume"]
            snapshot["volume"] = None
        if mode == "tail" and snapshot["partial"]:
            warnings.append(f"尾盘为部分覆盖，实际数据截止{actual.isoformat()}")
        if mode == "tail":
            try:
                quote_data = await _quote(symbol, clock, cutoff)
            except (ValueError, UnicodeError) as exc:
                warnings.append(str(exc))
    news = []
    if include_news:
        from app.services.market_news import fetch_news_context
        news_cutoff = min(clock, _at(today, 12, 0)) if mode == "noon" else cutoff
        name = quote_data.get("name", "") if quote_data else ""
        try:
            instruments = await asyncio.to_thread(repo.get_instruments_asset, asset_type)
            matches = [item for item in instruments.to_dicts() if item.get("symbol") == symbol]
            if matches:
                name = str(matches[0].get("name") or name)
        except Exception:
            warnings.append("证券简称未能从本地资料读取，新闻仅按证券代码匹配")
        # Fetch against real clock; apply report cutoff below, not during cache parsing.
        bundle = await fetch_news_context(symbol, name=name, now=clock)
        warnings.extend(bundle.get("warnings", []))
        for item in bundle.get("items", []):
            try:
                stamp = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp > news_cutoff:
                    continue
            except (ValueError, TypeError, KeyError):
                warnings.append("新闻发布时间缺失或非法，已省略")
                continue
            news.append(item)
    result = dict(mode=mode, symbol=symbol, period=period, as_of=actual.isoformat(),
                  fetched_at=datetime.now(SHANGHAI).isoformat(), source=source, adjust=adjust,
                  rows=rows, snapshot=snapshot, quote=quote_data, news=news, warnings=warnings)
    # Enforce finite JSON at the boundary, including optional external news fields.
    try:
        return json.loads(json.dumps(result, allow_nan=False, ensure_ascii=False))
    except (ValueError, TypeError) as exc:
        raise ValueError("行情或新闻包含非JSON安全数据") from exc
