"""Synthetic path checks, never evidence of real alpha or historical PIT coverage."""
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import polars as pl

from app.services import buffett_factors as b
from app.services.quality_value_strategy import make_adapter, attach_value_factor, VARIANTS
from app.strategy.engine import StrategyEngine
from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestService, StrategyBacktestConfig
from test_buffett_factors import fixture, D

BUILTIN = Path(__file__).resolve().parents[1]/'app/strategy/builtin'


def raw_prices(n=10):
    days=[date(2025,1,1)+timedelta(days=i) for i in range(170)]
    days=[day for day in days if day.weekday()<5]
    return pl.DataFrame([dict(symbol=f'{600001+i}.SH',date=day,
        open=10.+j*.02+i*.005,high=10.05+j*.02+i*.005,low=9.95+j*.02+i*.005,
        close=10.01+j*.02+i*.005,volume=5e6,amount=1e8)
        for i in range(n) for j,day in enumerate(days)])


def make_engine(directory, raw):
    return StrategyEngine(lambda _:pl.DataFrame(), strategy_files=[BUILTIN/'weekly_quality_value.py'],
        raw_history_loader=lambda target,lookback:raw.filter(pl.col('date')<=target),data_dir=directory)


class QualityValueStrategyTests(unittest.TestCase):
    def test_blend_precedes_top_n_and_missing_never_becomes_neutral(self):
        adapter=make_adapter('context')
        df=pl.DataFrame(dict(symbol=['600001.SH','600002.SH','600003.SH'],date=[date(2025,6,6)]*3,
            q_eligible=[True]*3,cx_bull=[True]*3,cx_market_allowed=[True]*3,cx_sentiment_allowed=[True]*3,
            qv_available=[True,True,False],qv_score=[64.,80.,None],cx_core_score=[80.,75.,100.],bv_quality_value=[0.,100.,None]))
        picked=adapter['filter_history'](df,{'top_n':1})
        self.assertEqual(['600002.SH'],picked['symbol'].to_list())
        self.assertEqual(80.,picked['qv_score'][0])

    def test_weekly_signal_only_on_friday_after_shared_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/b.DATASET_RELATIVE;p.parent.mkdir();p.write_text(fixture(10).model_dump_json())
            raw=raw_prices();engine=make_engine(root,raw)
            result=engine.run(VARIANTS['weekly'][0],date(2025,6,6),params={'top_n':5})
            self.assertEqual(5,result.total)
            for row in result.rows:
                self.assertAlmostEqual(.8*row['qv_base_score']+.2*row[b.COMPOSITE],row['qv_score'])
                self.assertAlmostEqual(row['qv_score'],row['score'])
            self.assertEqual(0,engine.run(VARIANTS['weekly'][0],date(2025,6,5)).total)

    def test_missing_data_blocks_before_price_load_and_never_rewrites_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            raw=raw_prices();engine=make_engine(Path(directory),raw)
            loader=Mock(side_effect=AssertionError('must not load prices'))
            engine._raw_history_loader=loader
            with self.assertRaisesRegex(ValueError,'尚无个股价值因子数据'):
                engine.run(VARIANTS['weekly'][0],date(2025,6,6))
            loader.assert_not_called()
            backtest_engine=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=Path(directory))),load_raw_panel=loader)
            result=StrategyBacktestService(backtest_engine,engine).run(StrategyBacktestConfig(strategy_id=VARIANTS['weekly'][0],symbols=None,start=date(2025,6,2),end=date(2025,6,13)))
            self.assertIn('依赖数据未就绪',result.error)
            loader.assert_not_called()

    def test_future_observations_block_before_loading_historical_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/b.DATASET_RELATIVE;p.parent.mkdir();p.write_text(fixture(10).model_dump_json())
            engine=make_engine(root,raw_prices());engine._raw_history_loader=Mock()
            with self.assertRaisesRegex(ValueError,'交集不足5家'):
                engine.run(VARIANTS['weekly'][0],date(2025,5,30))
            engine._raw_history_loader.assert_not_called()

    def test_attaching_factor_keeps_every_price_row_and_overwrites_injected_column(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/b.DATASET_RELATIVE;p.parent.mkdir();p.write_text(fixture(10).model_dump_json())
            raw=raw_prices().with_columns(pl.lit(999.).alias(b.COMPOSITE))
            joined=attach_value_factor(raw,root)
            self.assertEqual(raw.height,joined.height)
            self.assertTrue(joined[b.COMPOSITE].is_null().any())
            self.assertLessEqual(joined[b.COMPOSITE].drop_nulls().max(),100)

    def test_saved_parameters_and_explicit_request_have_shared_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/b.DATASET_RELATIVE;p.parent.mkdir();p.write_text(fixture(10).model_dump_json())
            engine=make_engine(root,raw_prices())
            overrides={'params':{'top_n':3,'quality_value_weight':.30}}
            saved=engine.run(VARIANTS['weekly'][0],date(2025,6,6),overrides=overrides)
            self.assertEqual(3,saved.total)
            for row in saved.rows:
                self.assertAlmostEqual(.7*row['qv_base_score']+.3*row[b.COMPOSITE],row['score'])
            explicit=engine.run(VARIANTS['weekly'][0],date(2025,6,6),params={'top_n':2},overrides=overrides)
            self.assertEqual(2,explicit.total)

    def test_real_native_matcher_uses_same_picks_and_scores_on_synthetic_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/b.DATASET_RELATIVE;p.parent.mkdir();p.write_text(fixture(10).model_dump_json())
            raw=raw_prices();engine=make_engine(root,raw)
            class Repo:
                store=SimpleNamespace(data_dir=root)
                def get_raw_daily_range(self,start,end,symbols=None):
                    return raw.filter(pl.col('date').is_between(start,end))
                def get_instruments(self):return pl.DataFrame()
                def get_index_daily(self,*a,**kw):return pl.DataFrame()
            config=StrategyBacktestConfig(strategy_id=VARIANTS['weekly'][0],symbols=None,start=date(2025,6,2),end=date(2025,6,13),params={'top_n':3},overrides={'params':{'quality_value_weight':.30,'top_n':5}},entry_fill='open_t+1',exit_fill='open_t+1',max_positions=3,holding_days=5)
            native=StrategyBacktestService(BacktestEngine(Repo()),engine).run(config)
            self.assertIsNone(native.error,native.error)
            live=engine.run(VARIANTS['weekly'][0],date(2025,6,6),params={'top_n':3},overrides={'params':{'quality_value_weight':.30,'top_n':5}})
            self.assertEqual({row['symbol'] for row in live.rows},{row['symbol'] for row in native.trades})
            score={row['symbol']:row['score'] for row in live.rows}
            for trade in native.trades:
                self.assertEqual('2025-06-09',str(trade['entry_date']))
                self.assertEqual(round(score[trade['symbol']],2),trade['entry_score'])

    def test_context_blend_uses_shared_value_factor_and_retains_price_calendar(self):
        from app.services.quality_value_strategy import _modules
        context=_modules()[-1];raw=raw_prices().head(3).with_columns(pl.Series(b.COMPOSITE,[10.,50.,90.]))
        # Only upstream context values are controlled here; its independent
        # feature/benchmark tests remain in test_context_strategy_status.py.
        prepared=raw.with_columns(pl.lit(70.).alias('cx_core_score'))
        with patch.object(context,'prepare_history',return_value=prepared):
            result=make_adapter('context')['prepare_history'](raw,{'quality_value_weight':.20})
        self.assertEqual(raw.height,result.height)
        self.assertEqual([58.,66.,74.],result['qv_score'].to_list())
        self.assertEqual(result['qv_score'].to_list(),result['score'].to_list())

    def test_zero_weight_and_context_without_factor_are_rejected(self):
        adapter=make_adapter('weekly')
        with self.assertRaisesRegex(ValueError,'5%'):
            adapter['prepare_history'](raw_prices().with_columns(pl.lit(50.).alias(b.COMPOSITE)),{'quality_value_weight':0.})
        with self.assertRaisesRegex(ValueError,'共享策略引擎'):
            make_adapter('context')['prepare_history'](raw_prices(),{})
