"""Opt-in, bounded public news context; never imported from quant/OpenBB.

Protocol/JSONP shape and Sina flash title/stock tags adapted from
quant-rsi/quant/data/_sources/news.py (eastmoney_stock, sina_flash, flash_title).
Only list metadata is fetched, never article HTML. Callers must explicitly opt
in and apply their mode/as-of cutoff; these results are not historical PIT data.
DNS preflight is not IP pinning: safe_http documents the egress requirement.
"""
from __future__ import annotations

import asyncio
import copy
import html
import json
import re
import time
from collections import OrderedDict
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.services.safe_http import validate_public_https_url

SHANGHAI = ZoneInfo("Asia/Shanghai")
EASTMONEY_URL = "https://search-api-web.eastmoney.com/search/jsonp"
SINA_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
_ALLOWED_ENDPOINTS = {EASTMONEY_URL, SINA_URL}
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
CACHE_TTL_SECONDS = 300.0
MAX_CACHE_ENTRIES = 128
MIN_REQUEST_INTERVAL_SECONDS = 1.0
_cache: OrderedDict = OrderedDict()
_gate = asyncio.Semaphore(1)
_rate_lock = asyncio.Lock()
_last_request = float("-inf")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "div", "li", "tr", "td", "hr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "br", "div", "li", "tr", "td", "hr"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    # Decode before stripping so encoded markup is not retained as HTML.
    parser.feed(html.unescape(value))
    text = re.sub(r"<[^>]*>", "", "".join(parser.parts))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _article_url(value) -> str:
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("新闻链接无效")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("新闻链接必须为无 userinfo 的 HTTP(S)")
    _ = parsed.port  # Reject malformed ports, too. These links are never fetched.
    return value


def _published(value, now: datetime) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?", value
    ):
        raise ValueError("新闻时间缺失或非完整日期时间")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    dt = dt.astimezone(SHANGHAI)
    if dt > now:
        raise ValueError("未来新闻时间已拒绝")
    return dt.isoformat()


def _code(value) -> str:
    match = re.fullmatch(r"(?:sh|sz|bj)?(\d{6})(?:\.(?:SH|SZ|BJ))?", str(value), re.I)
    return match[1] if match else ""


def _mention(title, summary, code, name, stocks=()) -> str:
    # A stock tag is direct evidence; arbitrary article IDs are not stock codes.
    if isinstance(stocks, list):
        for stock in stocks:
            candidates = [stock.get(k, "") for k in ("symbol", "code", "stock_code")] if isinstance(stock, dict) else [stock]
            if any(_code(candidate) == code for candidate in candidates):
                return "direct"
    for field, label in ((title, "title"), (summary, "summary")):
        if re.search(r"(?<![0-9])" + re.escape(code) + r"(?![0-9])", field) or (name and name in field):
            return label
    return ""


def _parse(text, source, code, name, now):
    if source == "eastmoney":
        match = re.fullmatch(r"\s*tick_news\s*\((.*)\)\s*;?\s*", text, re.S)
        if not match:
            raise ValueError("东财 JSONP 格式改变（仅允许 JSON，不执行脚本）")
        data = json.loads(match[1])
        if not isinstance(data, dict) or data.get("code", 0) not in (0, "0"):
            raise ValueError("东财搜索返回错误状态")
        result = data.get("result")
        if not isinstance(result, dict):
            raise ValueError("东财未返回文章列表，可能限流或格式变化")
        rows = result.get("cmsArticleWebOld")
    else:
        data = json.loads(text)
        try:
            result = data["result"]
            if result["status"]["code"] not in (0, "0"):
                raise ValueError("新浪快讯返回错误状态")
            rows = result["data"]["feed"]["list"]
        except (KeyError, TypeError) as exc:
            raise ValueError("新浪快讯缺少列表字段") from exc
    if not isinstance(rows, list):
        raise ValueError("新闻列表结构改变")
    items, rejected, unmatched = [], 0, 0
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError("新闻记录不是对象")
            stocks = ()
            if source == "eastmoney":
                title, summary = _clean(row.get("title")), _clean(row.get("content"))
                published, url = row.get("date"), row.get("url")
            else:
                summary = _clean(row.get("rich_text"))
                head = re.match(r"【([^】]{2,80})】", summary)
                title = head[1] if head else re.split(r"(?<=[。！？!?])", summary, maxsplit=1)[0]
                ext = row.get("ext") or {}
                if isinstance(ext, str):
                    ext = json.loads(ext)
                if not isinstance(ext, dict):
                    raise ValueError("新浪 ext 字段无效")
                stocks = ext.get("stocks", [])
                published = row.get("create_time")
                # When the flash has no article link, link to its provider page.
                url = ext.get("docurl") or row.get("docurl") or "https://finance.sina.com.cn/7x24/"
            mention = _mention(title, summary, code, name, stocks)
            if not mention:
                unmatched += 1
                continue
            if not title:
                raise ValueError("新闻标题为空")
            items.append({"title": title[:200], "summary": summary[:300], "source": source,
                          "published_at": _published(published, now), "url": _article_url(url), "mention": mention})
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected += 1
    unique = {(i["title"], i["published_at"], i["url"]): i for i in items}
    warnings = []
    if rejected:
        warnings.append(f"{source}: {rejected} 条无效或未来新闻已忽略")
    if not unique:
        warnings.append(f"{source}: 未找到精确提及 {code}" + (f" / {name}" if name else "") + " 的新闻；不以市场快讯替代")
    elif unmatched:
        warnings.append(f"{source}: {unmatched} 条未精确匹配标的的新闻已忽略")
    return sorted(unique.values(), key=lambda item: item["published_at"], reverse=True)[:5], warnings


async def _get_text(url, referer):
    base = url.split("?", 1)[0]
    if base not in _ALLOWED_ENDPOINTS:
        raise ValueError("新闻请求不在固定 HTTPS 白名单")

    async def request():
        global _last_request
        async with _rate_lock:
            delay = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            _last_request = time.monotonic()
        target = await asyncio.to_thread(validate_public_https_url, url)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", target, headers={"Accept-Encoding": "identity", "Referer": referer}) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("新闻请求禁止重定向")
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise ValueError("新闻响应禁止压缩")
                length = response.headers.get("content-length")
                if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                    raise ValueError("新闻响应超过 1 MiB")
                payload = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("新闻响应超过 1 MiB")
                    payload.extend(chunk)
                return payload.decode("utf-8-sig")
    # Includes DNS/thread dispatch, rate wait, connection and streamed body.
    return await asyncio.wait_for(request(), timeout=REQUEST_TIMEOUT_SECONDS)


def _eastmoney_url(keyword):
    params = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web",
              "clientType": "web", "clientVersion": "curr", "param": {"cmsArticleWebOld": {
                  "searchScope": "default", "sort": "time", "pageIndex": 1,
                  "pageSize": 10, "preTag": "", "postTag": ""}}}
    return EASTMONEY_URL + "?" + urlencode({"cb": "tick_news", "param": json.dumps(params, ensure_ascii=False, separators=(",", ":"))})


async def fetch_news_context(symbol: str, name: str = "", now=None) -> dict:
    """Return at most five exact-mention items; failures never break analysis.

    No request is made until a caller explicitly invokes this optional service.
    Naive 'now' is Shanghai local time. Caller owns any backtest/mode cutoff.
    """
    clock = datetime.now(SHANGHAI)
    result = {"items": [], "warnings": [], "source": "none", "fetched_at": clock.isoformat()}
    try:
        clock = now if now is not None else clock
        if not isinstance(clock, datetime):
            raise ValueError("now 必须为 datetime")
        clock = clock.replace(tzinfo=SHANGHAI) if clock.tzinfo is None else clock.astimezone(SHANGHAI)
        result["fetched_at"] = clock.isoformat()
        code = _code(symbol)
        if not code:
            raise ValueError("新闻查询仅支持六位证券代码")
        name = _clean(name)
        # Caller supplies the real display name, never a made-up theme keyword.
        if name in {"ETF", "etf", "LOF", "lof", "纳指", "纳斯达克", "股票", "基金", code, str(symbol)}:
            name = ""
        if len(name) > 80:
            raise ValueError("证券名称过长")
        key = (code, name)

        def cached():
            entry = _cache.get(key)
            if entry and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
                snapshot = copy.deepcopy(entry[1])
                snapshot["items"] = [i for i in snapshot["items"] if datetime.fromisoformat(i["published_at"]) <= clock]
                if not snapshot["items"]:
                    snapshot["source"] = "none"
                    snapshot["warnings"].append("缓存中无当前时间可用的精确匹配新闻")
                snapshot["warnings"].append("新闻使用 5 分钟缓存；fetched_at 为原抓取时间")
                return snapshot
            return None

        async def collect():
            hit = cached()
            if hit is not None:
                return hit
            async with _gate:
                hit = cached()  # Coalesce queued requests for the same symbol.
                if hit is not None:
                    return hit
                warnings = result["warnings"]
                # A single code search; optional real-name retry only if needed.
                for keyword in dict.fromkeys((code, name)):
                    if not keyword:
                        continue
                    try:
                        text = await _get_text(_eastmoney_url(keyword), "https://so.eastmoney.com/")
                        items, notes = _parse(text, "eastmoney", code, name, clock)
                        warnings.extend(notes)
                        if items:
                            result.update(items=items, warnings=warnings, source="eastmoney")
                            break
                    except Exception as exc:
                        warnings.append(f"eastmoney: 新闻查询失败 ({type(exc).__name__}: {_clean(str(exc))[:160]})")
                else:
                    try:
                        text = await _get_text(SINA_URL + "?" + urlencode({"zhibo_id": 152, "page_size": 50, "dire": "f"}), "https://finance.sina.com.cn/7x24/")
                        items, notes = _parse(text, "sina", code, name, clock)
                        warnings.extend(notes)
                        result.update(items=items, warnings=warnings, source="sina" if items else "none")
                    except Exception as exc:
                        warnings.append(f"sina: 新闻查询失败 ({type(exc).__name__}: {_clean(str(exc))[:160]})")
                        result["warnings"] = warnings
                _cache[key] = (time.monotonic(), copy.deepcopy(result))
                _cache.move_to_end(key)
                while len(_cache) > MAX_CACHE_ENTRIES:
                    _cache.popitem(last=False)
                return result
        return await asyncio.wait_for(collect(), timeout=TOTAL_TIMEOUT_SECONDS)
    except Exception as exc:
        result["warnings"].append(f"新闻上下文不可用 ({type(exc).__name__}: {_clean(str(exc))[:160]})")
        return result
