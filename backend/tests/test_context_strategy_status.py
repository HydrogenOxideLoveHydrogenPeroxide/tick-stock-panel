from pathlib import Path
import unittest
from unittest.mock import patch
from datetime import date
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.services.context_strategy_status import context_strategy_status
from app.api.quant_assistant import router
from app.strategy.engine import StrategyEngine


class ContextStatusTests(unittest.TestCase):
    def test_research_status_never_claims_missing_inputs_are_live(self):
        data=context_strategy_status()
        self.assertEqual('blocked_missing_data',data['full_status'])
        self.assertEqual(6,len(data['components']))
        if data['report_available']:
            self.assertEqual(54,data['scenarios'])
            self.assertAlmostEqual(77.65292154315743,data['buffett']['ratio_pct'])
            self.assertEqual('annual_reference_only',data['buffett']['status'])
            self.assertFalse(data['core_gate_passed'])

    def test_explicit_core_adapter_retains_its_actual_score_column(self):
        file=Path(__file__).resolve().parents[3]/'quant-rsi/quant/index/context_core_panel.py'
        engine=StrategyEngine(lambda _:pl.DataFrame(),strategy_files=[file])
        model=engine.get('context_sentiment_bull_core')
        self.assertEqual('cx_core_score',model.meta['order_by'])
        self.assertIn('研究子模型',model.meta['name'])
        self.assertEqual('raw',model.meta['price_basis'])

    @unittest.skipUnless((Path(__file__).resolve().parents[2]/'data/backtest_history/manifest.json').exists(),
                         'requires separately prepared, verified research index archive')
    def test_live_benchmark_appends_new_session_without_rewriting_archive(self):
        file=Path(__file__).resolve().parents[3]/'quant-rsi/quant/index/context_core_panel.py'
        model=StrategyEngine(lambda _:pl.DataFrame(),strategy_files=[file]).get('context_sentiment_bull_core')
        prepare=model.prepare_history_fn
        raw=pl.DataFrame({'symbol':['600000.SH'],'date':[date(2026,11,5)],'cx_core_score':[70.]})
        benchmark=pl.DataFrame({'symbol':['000300.SH','000300.SH'],'date':[date(2026,11,4),date(2026,11,5)],'close':[999999.,4113.411]})
        cx=prepare.__globals__['cx']
        with patch.object(cx,'daily_features',return_value=raw) as compute:
            prepared=prepare(raw,{},benchmark=benchmark)
        index=compute.call_args.args[1]
        self.assertEqual(4113.411,index.filter(pl.col('date')==date(2026,11,5))['close'][0])
        self.assertNotEqual(999999.,index.filter((pl.col('date')==date(2026,11,4)) & (pl.col('symbol')=='000300.SH'))['close'][0])
        self.assertEqual(70.,prepared['score'][0])
        with self.assertRaisesRegex(ValueError,'缺少.*指数数据'):
            prepare(raw,{},benchmark=benchmark.head(1))

    def test_context_status_endpoint(self):
        app=FastAPI(); app.include_router(router)
        response=TestClient(app).get('/api/quant-assistant/research-context')
        self.assertEqual(200,response.status_code)
        self.assertEqual('research_only',response.json()['promotion'])


if __name__=='__main__':unittest.main()
