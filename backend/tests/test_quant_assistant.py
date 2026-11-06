"""Offline strict contract, safe storage, budget and source-quality regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, time
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.api.quant_assistant import router
from app.services import quant_assistant as q

NOW = datetime(2026, 11, 4, 19, 0, tzinfo=q.SHANGHAI)


def holding(symbol="600000", quantity=0, available=None, cost=None, **kwargs):
    return dict(symbol=symbol, quantity=quantity, available_quantity=quantity if available is None else available,
                cost_price=(10 if quantity else 0) if cost is None else cost, **kwargs)


def context(symbol="600000.SH", mode="morning", price=10, ma20=9, ma60=8, rsi=50):
    period = {"morning": "day", "noon": "m60", "tail": "m5"}[mode]
    if mode == "morning":
        stamps, cursor = [], NOW.replace(hour=15) - timedelta(days=1)
        while len(stamps) < 65:
            if cursor.weekday() < 5:
                stamps.append(cursor.date().isoformat())
            cursor -= timedelta(days=1)
        stamps.reverse()
        actual = datetime.combine(datetime.fromisoformat(stamps[-1]).date(), time(15), q.SHANGHAI)
    elif mode == "noon":
        actual = NOW.replace(hour=11, minute=30)
        stamps = [actual.replace(hour=10).isoformat(), actual.isoformat()]
    else:
        actual = NOW.replace(hour=14, minute=50)
        stamps = []
        for begin, end in ((actual.replace(hour=9, minute=35), actual.replace(hour=11, minute=30)),
                           (actual.replace(hour=13, minute=5), actual)):
            while begin <= end:
                stamps.append(begin.isoformat())
                begin += timedelta(minutes=5)
    rows = [dict(date=s, close=price) for s in stamps]
    indicators = dict(period=period, as_of=stamps[-1], available_bars=65,
                      ma={"20": ma20, "60": ma60}, rsi14=rsi)
    snapshot = dict(close=price, as_of=actual.isoformat(), period_end=actual.isoformat(),
                    partial=False, coverage_complete=True, coverage_missing=[], indicators=indicators)
    return dict(symbol=symbol, mode=mode, period=period, as_of=actual.isoformat(),
                fetched_at=NOW.isoformat(), source=("https://web.ifzq.gtimg.cn" +
                    ("/appstock/app/fqkline/get?param=" + symbol[-2:].lower() + symbol[:6] + ",day,,,320,"
                     if mode == "morning" else "/appstock/app/kline/mkline?param=" + symbol[-2:].lower() + symbol[:6] + "," + period + ",,320")),
                adjust="none", rows=rows, snapshot=snapshot, warnings=[], quote=None, news=[])


class ValidationTests(unittest.TestCase):
    def test_defaults_and_normalization(self):
        p = q.Portfolio()
        self.assertEqual(p.model_dump(), {"cash": 0, "holdings": [], "strategy": {
            "kind": "trend", "screener_strategy_id": "", "stop_loss_pct": 8, "take_profit_pct": 20,
            "max_position_pct": 25, "target_position_pct": 15, "cash_reserve_pct": 10}})

    def test_screener_catalog_and_strategy_id(self):
        from app.services.screener import PRESET_STRATEGIES
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(directory)))
            cat = q.get_screener_catalog(repo)
        ids = {s["id"] for s in cat["strategies"]}
        self.assertEqual(ids, set(PRESET_STRATEGIES))
        self.assertEqual(cat["mode"], "screener")
        self.assertTrue(cat["disclaimer"])
        for sid in ids | {"quant_weekly_selection", "pullback_ma20_bounce"}:
            self.assertEqual(q.Strategy(screener_strategy_id=sid).screener_strategy_id, sid)
        for bad in ("../bad", "with space", "x" * 129, 123):
            with self.assertRaises(ValidationError):
                q.Strategy(screener_strategy_id=bad)
        self.assertEqual(q.Strategy().screener_strategy_id, "")
        for raw, canonical in (("600000", "600000.SH"), ("000001", "000001.SZ"),
                               ("513100.sh", "513100.SH"), ("159915", "159915.SZ")):
            h = q.Holding(**holding(raw))
            self.assertEqual(h.symbol, canonical)
            self.assertEqual((h.name, h.note, h.lot_size), ("", "", 100))

    def test_invalid_holding_fields(self):
        cases = [dict(symbol="../600000"), dict(symbol="600000.SZ"), dict(symbol="800001"),
                 dict(quantity=-1), dict(quantity=1.5), dict(quantity=True), dict(quantity="100"),
                 dict(quantity=1_000_000_001), dict(available_quantity=1), dict(cost_price=float("nan")),
                 dict(cost_price=float("inf")), dict(cost_price=True), dict(cost_price="10"),
                 dict(cost_price=-1), dict(cost_price=1_000_001), dict(lot_size=1), dict(lot_size=100.0),
                 dict(name="x" * 101), dict(note="x" * 1001), dict(script="bad")]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                q.Holding(**(holding() | bad))
        with self.assertRaises(ValidationError):
            q.Holding(**holding(quantity=100, cost=0))

    def test_portfolio_and_strategy_limits(self):
        for bad in [dict(cash=-1), dict(cash=True), dict(cash="1"), dict(cash=1e12 + 1),
                    dict(cash=float("inf")), dict(cash=float("nan")), dict(unexpected=1),
                    dict(holdings=[holding("600000"), holding("600000.SH")]),
                    dict(holdings=[holding(f"600{i:03}") for i in range(21)])]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                q.Portfolio(**bad)
        for key, bad in [("stop_loss_pct", 0), ("stop_loss_pct", 51), ("take_profit_pct", 201),
                         ("max_position_pct", 0), ("cash_reserve_pct", 101), ("cash_reserve_pct", -1),
                         ("target_position_pct", 26), ("target_position_pct", 0),
                         ("kind", "script"), ("cash_reserve_pct", True), ("stop_loss_pct", float("nan")),
                         ("extra", 1)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValidationError):
                q.Strategy(**{key: bad})
        with self.assertRaises(ValidationError):
            q.SuggestRequest(portfolio=q.Portfolio(), mode="auto")
        with self.assertRaises(ValidationError):
            q.SuggestRequest(portfolio=q.Portfolio(), mode="morning", save=True)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(q.settings, "data_dir", self.root)
        self.patch.start()
        app = FastAPI()
        app.state.repo = object()
        app.include_router(router)
        self.client = TestClient(app)
        self.path = self.root / "user_data" / "quant_assistant.json"

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.temp.cleanup()

    def test_get_no_quotes_put_roundtrip_permissions(self):
        with patch.object(q, "fetch_market_context", new=AsyncMock()) as fetch:
            self.assertEqual(self.client.get("/api/quant-assistant/portfolio").json(), q.Portfolio().model_dump())
            draft = dict(cash=12345, holdings=[holding("513100")])
            response = self.client.put("/api/quant-assistant/portfolio", json=draft)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["holdings"][0]["symbol"], "513100.SH")
            self.assertEqual(self.client.get("/api/quant-assistant/portfolio").json(), response.json())
            fetch.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corruption_fail_closed_get_and_put(self):
        self.path.parent.mkdir()
        for raw in ['{oops', '{"cash":1,"cash":2}', '{"cash":NaN}', '{"holdings":[],"extra":1}']:
            self.path.write_text(raw)
            self.assertEqual(self.client.get("/api/quant-assistant/portfolio").status_code, 409)
            self.assertEqual(self.client.put("/api/quant-assistant/portfolio", json={}).status_code, 409)
            self.assertEqual(self.path.read_text(), raw)

    def test_symlink_file_directory_ancestor_and_lock_rejected(self):
        target = self.root / "outside.json"
        target.write_text('{}')
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        self.assertEqual(self.client.get("/api/quant-assistant/portfolio").status_code, 409)
        self.assertEqual(self.client.put("/api/quant-assistant/portfolio", json={}).status_code, 409)
        self.assertEqual(target.read_text(), '{}')
        self.path.unlink()
        lock = self.path.parent / ".quant_assistant.lock"
        lock.unlink()
        lock.symlink_to(target)
        self.assertEqual(self.client.get("/api/quant-assistant/portfolio").status_code, 409)
        lock.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.root)
        self.assertEqual(self.client.get("/api/quant-assistant/portfolio").status_code, 409)
        self.path.parent.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with patch.object(q.settings, "data_dir", alias):
            self.assertEqual(self.client.get("/api/quant-assistant/portfolio").status_code, 409)

    def test_atomic_failure_keeps_original_and_concurrent_saves(self):
        q.save_portfolio(q.Portfolio(cash=5))
        with patch.object(q.os, "replace", side_effect=OSError("mock failure")):
            with self.assertRaises(q.PortfolioStorageError):
                q.save_portfolio(q.Portfolio(cash=6))
        self.assertEqual(q.load_portfolio().cash, 5)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda n: q.save_portfolio(q.Portfolio(cash=n)), range(20)))
        self.assertIn(q.load_portfolio().cash, range(20))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_post_uses_draft_never_autosaves_and_bad_body_422(self):
        q.save_portfolio(q.Portfolio(cash=5))
        draft = q.Portfolio(cash=10000, holdings=[holding()])
        with patch.object(q, "fetch_market_context", new=AsyncMock(return_value=context())) as fetch:
            response = self.client.post("/api/quant-assistant/suggest", json={"portfolio": draft.model_dump(), "mode": "morning"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["summary"]["cash"], 10000)
            fetch.assert_awaited_once()
        self.assertEqual(q.load_portfolio().cash, 5)
        self.assertEqual(self.client.put("/api/quant-assistant/portfolio", json={"cash": "1"}).status_code, 422)
        self.assertEqual(self.client.post("/api/quant-assistant/suggest", json={"portfolio": {}, "mode": "auto"}).status_code, 422)


class SuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def run_suggest(self, holdings=None, cash=100000, strategy=None, contexts=None, mode="morning", clock=NOW):
        p = q.Portfolio(cash=cash, holdings=holdings or [holding()], strategy=strategy or q.Strategy())
        bundle = contexts or {h.symbol: context(h.symbol, mode) for h in p.holdings}
        async def fetch(repo, symbol, requested_mode, include_news=False):
            self.assertEqual(requested_mode, mode)
            self.assertFalse(include_news)
            data = bundle[symbol]
            if isinstance(data, Exception):
                raise data
            return data
        with patch.object(q, "fetch_market_context", side_effect=fetch):
            result = await q.suggest_portfolio(object(), p, mode, now=clock)
        json.dumps(result, allow_nan=False)
        self.assertEqual(set(result), {"generated_at", "mode", "summary", "suggestions", "warnings"})
        for item in result["suggestions"]:
            self.assertEqual(set(item), {"symbol", "name", "action", "quantity", "price", "market_value", "position_pct", "pnl_pct", "reason", "warnings", "data_context"})
            self.assertIs(type(item["quantity"]), int)
            self.assertGreaterEqual(item["quantity"], 0)
        return result

    async def test_candidates_and_two_buys_shared_buffered_budget(self):
        s = q.Strategy(target_position_pct=100, max_position_pct=100, cash_reserve_pct=10)
        result = await self.run_suggest(holdings=[holding("600000"), holding("600001")], cash=10000, strategy=s)
        buys = result["suggestions"]
        self.assertEqual([i["action"] for i in buys], ["buy", "blocked"])
        self.assertEqual(buys[0]["quantity"], 800)
        self.assertEqual(result["summary"]["buy_budget"], 9000)
        self.assertLessEqual(sum(i["quantity"] * (i["price"] or 0) * q.BUFFER for i in buys), 9000)
        self.assertTrue(any("LLM" in warning for warning in result["warnings"]))
        self.assertTrue(any("不是实时价格" in warning for warning in result["warnings"]))

    async def test_two_actual_buys_budget_not_reused(self):
        result = await self.run_suggest(holdings=[holding("600000"), holding("600001")], cash=100000)
        self.assertEqual([i["quantity"] for i in result["suggestions"]], [1500, 1500])
        self.assertTrue(all(i["action"] == "buy" for i in result["suggestions"]))
        self.assertLessEqual(sum(i["quantity"] * i["price"] * q.BUFFER for i in result["suggestions"]), result["summary"]["buy_budget"])

    async def test_cash_reserve_includes_held_assets_and_full_reserve_blocks(self):
        result = await self.run_suggest(holdings=[holding(quantity=1000), holding("600001")], cash=1000)
        self.assertEqual(result["summary"]["buy_budget"], 0)
        self.assertEqual(result["suggestions"][1]["action"], "blocked")
        result = await self.run_suggest(strategy=q.Strategy(cash_reserve_pct=100))
        self.assertEqual(result["suggestions"][0]["action"], "blocked")

    async def test_all_buys_blocked_if_any_held_unpriced_not_candidate(self):
        result = await self.run_suggest(holdings=[holding(quantity=100), holding("600001")], contexts={
            "600000.SH": ValueError("unpriced"), "600001.SH": context("600001.SH")})
        self.assertIsNone(result["summary"]["total_assets"])
        self.assertIsNone(result["summary"]["market_value"])
        self.assertIsNone(result["summary"]["position_pct"])
        self.assertTrue(all(i["action"] == "blocked" for i in result["suggestions"]))
        result = await self.run_suggest(holdings=[holding(), holding("600001")], contexts={
            "600000.SH": ValueError("candidate missing"), "600001.SH": context("600001.SH")})
        self.assertEqual(result["suggestions"][1]["action"], "buy")

    async def test_stop_loss_first_boundary_and_t1_oddlot_full_available(self):
        result = await self.run_suggest(holdings=[holding(quantity=1000, available=155)], contexts={
            "600000.SH": context(price=9.2, ma20=9, ma60=8)})
        item = result["suggestions"][0]
        self.assertEqual((item["action"], item["quantity"], item["pnl_pct"]), ("sell", 155, -8))
        self.assertIn("止损", item["reason"][0])
        result = await self.run_suggest(holdings=[holding(quantity=100, available=0)], contexts={"600000.SH": context(price=8)})
        self.assertEqual((result["suggestions"][0]["action"], result["suggestions"][0]["quantity"]), ("blocked", 0))

    async def test_takeprofit_boundary_half_lot_round_and_oddlot_partial_block(self):
        result = await self.run_suggest(holdings=[holding(quantity=1100)], contexts={"600000.SH": context(price=12)})
        self.assertEqual((result["suggestions"][0]["action"], result["suggestions"][0]["quantity"]), ("reduce", 500))
        result = await self.run_suggest(holdings=[holding(quantity=100)], contexts={"600000.SH": context(price=12)})
        self.assertEqual((result["suggestions"][0]["action"], result["suggestions"][0]["quantity"]), ("blocked", 0))

    async def test_overmax_down_target_and_lot200(self):
        result = await self.run_suggest(holdings=[holding(quantity=5000, lot_size=200)], cash=50000)
        self.assertEqual((result["suggestions"][0]["action"], result["suggestions"][0]["quantity"]), ("reduce", 3600))
        result = await self.run_suggest(holdings=[holding(lot_size=200)], cash=10000)
        self.assertEqual(result["suggestions"][0]["action"], "blocked")

    async def test_trendweak_meanrev_and_hold(self):
        for kind, price, ma20, ma60, rsi, expected in [
            ("trend", 10, 11, 12, 50, "sell"), ("trend", 10, 10, 10, 50, "hold"),
            ("mean_reversion", 10, 11, 12, 29, "buy"), ("mean_reversion", 10, 11, 12, 71, "reduce")]:
            with self.subTest(kind=kind, expected=expected):
                result = await self.run_suggest(holdings=[holding(quantity=1000)], strategy=q.Strategy(kind=kind),
                    contexts={"600000.SH": context(price=price, ma20=ma20, ma60=ma60, rsi=rsi)})
                self.assertEqual(result["suggestions"][0]["action"], expected)
                self.assertFalse(any(word in result["suggestions"][0]["reason"][0] for word in ("金叉", "死叉")))

    async def test_sells_not_credited_to_batch_buys(self):
        result = await self.run_suggest(holdings=[holding(quantity=1000), holding("600001")], cash=0,
            contexts={"600000.SH": context(price=8), "600001.SH": context("600001.SH")})
        self.assertEqual([i["action"] for i in result["suggestions"]], ["sell", "blocked"])
        self.assertEqual(result["summary"]["buy_budget"], 0)

    async def test_badfields_partial_short_future_stale_periods_and_gaps(self):
        edits = [lambda c: c.update(period="m5"), lambda c: c.update(source="https://evil.test/a"),
                 lambda c: c.update(source=c["source"].replace("sh600000", "sh600001")),
                 lambda c: c.update(source=c["source"] + "&extra=bad"),
                 lambda c: c.update(warnings="bad"),
                 lambda c: c.update(as_of=(NOW + timedelta(days=1)).isoformat()),
                 lambda c: c.update(fetched_at=(NOW + timedelta(seconds=1)).isoformat()),
                 lambda c: c.update(fetched_at=(NOW - timedelta(minutes=3)).isoformat()),
                 lambda c: c["snapshot"].update(partial=True),
                 lambda c: c["snapshot"].update(close=float("nan")),
                 lambda c: c["snapshot"].update(close=0),
                 lambda c: c["snapshot"].update(as_of="bad"),
                 lambda c: c["snapshot"]["indicators"].update(available_bars=59),
                 lambda c: c["snapshot"]["indicators"].update(available_bars=True),
                 lambda c: c["snapshot"]["indicators"].update(period="m60"),
                 lambda c: c["snapshot"]["indicators"].update(as_of="2026-11-01"),
                 lambda c: c["snapshot"]["indicators"].update(rsi14=101),
                 lambda c: c["snapshot"]["indicators"]["ma"].update({"60": None}),
                 lambda c: c["rows"].reverse()]
        for edit in edits:
            data = context()
            edit(data)
            with self.subTest(edit=edit):
                result = await self.run_suggest(contexts={"600000.SH": data})
                self.assertEqual((result["suggestions"][0]["action"], result["suggestions"][0]["quantity"]), ("blocked", 0))
                self.assertIsNone(result["suggestions"][0]["price"])

    async def test_daily_history_holiday_gap_does_not_block_reference(self):
        data = context()
        data["rows"].pop(3)  # Historical holidays/suspensions cannot be inferred from weekday gaps.
        result = await self.run_suggest(contexts={"600000.SH": data})
        self.assertEqual(result["suggestions"][0]["action"], "buy")
        self.assertTrue(any("历史交易日连续性" in w for w in result["warnings"]))

    async def test_daily_reference_stale_and_same_day_blocked(self):
        for clock in (NOW + timedelta(days=9), NOW - timedelta(days=1)):
            data = context()
            data["fetched_at"] = clock.isoformat()
            result = await self.run_suggest(contexts={"600000.SH": data}, clock=clock)
            self.assertEqual(result["suggestions"][0]["action"], "blocked")

    async def test_noon_tail_homogeneous_and_missing_partial_cutoff_blocked(self):
        for mode in ("noon", "tail"):
            result = await self.run_suggest(mode=mode)
            self.assertEqual(result["suggestions"][0]["action"], "buy")
            for edit in (lambda c: c["snapshot"].update(partial=True),
                         lambda c: c["snapshot"].update(coverage_missing=["gap"]),
                         lambda c: c["rows"].pop(0),
                         lambda c: c["snapshot"]["indicators"].update(period="day"),
                         lambda c: c["snapshot"].update(period_end=NOW.isoformat())):
                data = context(mode=mode)
                edit(data)
                result = await self.run_suggest(mode=mode, contexts={"600000.SH": data})
                self.assertEqual(result["suggestions"][0]["action"], "blocked")
            result = await self.run_suggest(mode=mode, clock=NOW.replace(hour=10))
            self.assertEqual(result["suggestions"][0]["action"], "blocked")

    async def test_empty_portfolio_never_fetches(self):
        with patch.object(q, "fetch_market_context", new=AsyncMock()) as fetch:
            result = await q.suggest_portfolio(None, q.Portfolio(), "noon", now=NOW)
            fetch.assert_not_called()
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["summary"], dict(cash=0, total_assets=0, market_value=0, position_pct=0, buy_budget=0))

    async def test_numeric_extremes_never_emit_nonfinite_or_oversized_shares(self):
        result = await self.run_suggest(cash=1e12, contexts={"600000.SH": context(price=1e-200, ma20=1e-201, ma60=1e-202)})
        self.assertLessEqual(result["suggestions"][0]["quantity"], 1_000_000_000)
        result = await self.run_suggest(holdings=[holding(quantity=100, cost=5e-324)], contexts={"600000.SH": context()})
        self.assertEqual(result["suggestions"][0]["action"], "blocked")
        self.assertIsNone(result["suggestions"][0]["pnl_pct"])

    async def test_batch_deadline_blocks_remainder_and_is_sequential(self):
        active, peak, calls = 0, 0, 0
        gate = asyncio.Event()
        async def hanging(*args, **kwargs):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            try:
                await gate.wait()
            finally:
                active -= 1
        portfolio = q.Portfolio(holdings=[holding(f"600{i:03}") for i in range(20)])
        with patch.object(q, "fetch_market_context", side_effect=hanging), patch.object(q, "BATCH_TIMEOUT_SECONDS", .025), patch.object(q, "EACH_TIMEOUT_SECONDS", .01):
            start = asyncio.get_running_loop().time()
            result = await q.suggest_portfolio(None, portfolio, "morning", now=NOW)
            elapsed = asyncio.get_running_loop().time() - start
        self.assertLess(elapsed, .5)
        self.assertEqual(peak, 1)
        self.assertLess(calls, 20)
        self.assertTrue(all(i["action"] == "blocked" and i["quantity"] == 0 for i in result["suggestions"]))


if __name__ == "__main__":
    unittest.main()
