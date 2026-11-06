"""Actual Screener filters, shared configuration, and portfolio risk priority."""
from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.quant_assistant import router
from app.api.screener import strategies as screener_catalog
from app.services import quant_assistant as q
from app.services.screener import ScreenerService
from app.services.screener_strategies import run_screener_strategy
from app.strategy import config
from app.strategy.engine import StrategyEngine, StrategyResult
from test_quant_assistant import NOW, context, holding


def frame(hit=True):
    return pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 11, 3)],
                         "close": [10.], "ma60": [8.], "vol_ratio_5d": [2.],
                         "signal_ma_golden_5_20": [hit], "momentum_20d": [.1]})


class SharedStrategyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(self.temp.name).resolve()))
        self.engine = StrategyEngine(lambda day: frame(), strategy_dirs=[
            Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"],
            strategy_files=[Path(__file__).resolve().parents[3] / "quant-rsi" / "quant" / "index" / "panel.py"])

    def test_shared_catalog_includes_loaded_strategies_and_saved_names(self):
        config.save_override(self.repo.store.data_dir, "ma_golden_cross", {"name": "我的金叉"})
        catalog = q.get_screener_catalog(self.repo, self.engine)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=self.repo, strategy_engine=self.engine)))
        self.assertEqual(catalog["strategies"], screener_catalog(request)["presets"])
        ids = {s["id"] for s in catalog["strategies"]}
        self.assertTrue({"broken_board_recovery", "consecutive_limit_ups", "low_volatility_leader", "pullback_ma20_bounce", "quant_weekly_selection"} <= ids)
        self.assertEqual(len(ids), 21)
        self.assertEqual(next(s for s in catalog["strategies"] if s["id"] == "ma_golden_cross")["name"], "我的金叉")

    def test_shared_runner_honors_saved_filter_and_matches_screener(self):
        overrides = {"basic_filter": {"price_min": 11}, "display_limit": 1}
        config.save_override(self.repo.store.data_dir, "ma_golden_cross", overrides)
        with patch.object(ScreenerService, "_load_enriched_for_date", return_value=frame()):
            actual, applied = run_screener_strategy(self.repo, self.engine, "ma_golden_cross", date(2026, 11, 3))
            expected = ScreenerService(self.repo).run_preset("ma_golden_cross", date(2026, 11, 3), basic_filter=overrides["basic_filter"], display_limit=1)
        self.assertEqual(actual.rows, expected.rows)
        self.assertEqual(actual.total, 0)
        self.assertEqual(applied, overrides)

    async def suggest(self, hit=True, strategy_id="ma_golden_cross", qty=0, price=10, empty=False):
        portfolio = q.Portfolio(cash=100000, holdings=[holding(quantity=qty)],
                                strategy=q.Strategy(screener_strategy_id=strategy_id))
        async def fetch(*args, **kwargs):
            return context(price=price, rsi=90)
        with patch.object(ScreenerService, "_load_enriched_for_date", return_value=pl.DataFrame() if empty else frame(hit)), patch.object(q, "fetch_market_context", side_effect=fetch):
            return await q.suggest_portfolio(self.repo, portfolio, "morning", now=NOW, strategy_engine=self.engine)

    async def test_real_cross_controls_buys_instead_of_old_approximation(self):
        missed = (await self.suggest(hit=False))["suggestions"][0]
        self.assertEqual(missed["action"], "hold")
        self.assertFalse(missed["data_context"]["screener"]["matched"])
        matched = (await self.suggest(hit=True))["suggestions"][0]
        self.assertEqual(matched["action"], "buy")
        self.assertGreater(matched["quantity"], 0)
        self.assertEqual(matched["data_context"]["screener"]["as_of"], "2026-11-03")
        self.assertTrue(matched["data_context"]["screener"]["matched"])

    async def test_missing_data_or_unloaded_strategy_blocks_without_fallback(self):
        for kwargs in ({"empty": True}, {"strategy_id": "removed_strategy"}):
            result = (await self.suggest(**kwargs))["suggestions"][0]
            self.assertEqual(result["action"], "blocked")
            self.assertEqual(result["quantity"], 0)
            self.assertIn("error", result["data_context"]["screener"])

    async def test_stop_loss_precedes_strategy_and_misses_do_not_force_sells(self):
        stopped = (await self.suggest(qty=100, price=9))["suggestions"][0]
        self.assertEqual(stopped["action"], "sell")
        self.assertEqual(stopped["quantity"], 100)
        missed = (await self.suggest(hit=False, qty=100))["suggestions"][0]
        self.assertEqual(missed["action"], "hold")

    def test_engine_only_strategy_uses_same_engine_and_limits(self):
        engine = SimpleNamespace(has=lambda sid: True, run=lambda *args, **kwargs: StrategyResult(
            as_of=args[1], strategy_id=args[0], rows=[{"symbol": "600000.SH"}, {"symbol": "600001.SH"}], total=2))
        config.save_override(self.repo.store.data_dir, "quant_weekly_selection", {"display_limit": 1})
        result, _ = run_screener_strategy(self.repo, engine, "quant_weekly_selection", date(2026, 11, 3))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.total, 1)

    def test_unknown_strategy_cannot_replace_saved_portfolio(self):
        app = FastAPI()
        app.include_router(router)
        app.state.repo = self.repo
        app.state.strategy_engine = self.engine
        with patch.object(q.settings, "data_dir", self.repo.store.data_dir):
            client = TestClient(app)
            old = q.Portfolio(cash=1234)
            q.save_portfolio(old)
            data = old.model_dump()
            data["strategy"]["screener_strategy_id"] = "removed_strategy"
            self.assertEqual(client.put("/api/quant-assistant/portfolio", json=data).status_code, 422)
            self.assertEqual(q.load_portfolio(), old)
            response = client.get("/api/quant-assistant/strategies")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["strategies"]), 21)


if __name__ == "__main__":
    unittest.main()
