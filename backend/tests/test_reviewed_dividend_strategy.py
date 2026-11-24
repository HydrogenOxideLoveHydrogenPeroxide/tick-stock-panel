import unittest
from datetime import date
from pathlib import Path
import polars as pl
from app.strategy.engine import StrategyEngine

PATH = Path(__file__).resolve().parents[1] / 'app/strategy/reviewed/ai_mwum51zl.py'

class ReviewedDividendTests(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyEngine._load_file(PATH)
        self.base = dict(symbol='600001.SH', date=date(2026,11,23), raw_close=4.,
            close=100., dividend_per_share_12m=.16, net_profit=100., total_shares=100.,
            is_st=False, is_delisting=False, is_suspended=False, listed_trading_days=300,
            fundamentals_available_date=date(2026,10,30), score=999.)

    def test_missing_data_raises_in_engine(self):
        frame=pl.DataFrame([{'symbol':'600001.SH','date':date(2026,11,23)}])
        engine=StrategyEngine(lambda d:frame,lambda d,n:frame,strategy_files=[PATH])
        engine.get("ai_mwum51zl").load_history_fn=lambda *args:frame
        with self.assertRaisesRegex(ValueError,'缺少历史时点数据'):
            engine.run('ai_mwum51zl',date(2026,11,23))

    def test_filters_raw_prices_states_and_cap_order(self):
        rows=[self.base, {**self.base,'symbol':'600002.SH','total_shares':50.}]
        for i,change in enumerate([dict(is_st=True),dict(is_delisting=True),dict(is_suspended=True),
                dict(listed_trading_days=249),dict(net_profit=-1.),dict(raw_close=10.),
                dict(dividend_per_share_12m=.01),dict(symbol='300001.SZ'),dict(symbol='518800.SH')]):
            rows.append({**self.base,'symbol':f'6001{i:02}.SH',**change})
        result=self.strategy.filter_history_fn(pl.DataFrame(rows),{})
        self.assertEqual(result['symbol'].to_list(),['600002.SH','600001.SH'])
        self.assertEqual(result['market_cap'].to_list(),[200.,400.])
        self.assertNotIn('score',result.columns)
        self.assertEqual(self.strategy.entry_signals,[])
        self.assertIsNone(self.strategy.stop_loss)

    def test_web_defaults_use_monthly_open_and_historical_tax(self):
        defaults = self.strategy.meta['backtest_defaults']
        self.assertEqual(defaults['entry_fill'], 'open_t+1')
        self.assertEqual(defaults['exit_fill'], 'open_t+1')
        self.assertIsNone(defaults['stamp_tax_pct'])
        self.assertEqual(defaults['position_sizing'], 'equal')
        self.assertEqual(self.strategy.meta['historical_research'], 'dividend_monthly_v1')

    def test_future_fundamentals_rejected(self):
        frame=pl.DataFrame([{**self.base,'fundamentals_available_date':date(2026,11,24)}])
        with self.assertRaisesRegex(ValueError,'未来数据'):
            self.strategy.filter_history_fn(frame,{})

    def test_top_n_per_date_tie_break_and_engine_ranking(self):
        frame=pl.DataFrame([{**self.base,'symbol':f'6000{i:02}.SH'} for i in range(12,0,-1)])
        engine=StrategyEngine(lambda d:frame,lambda d,n:frame,strategy_files=[PATH])
        engine.get("ai_mwum51zl").load_history_fn=lambda *args:frame
        result=engine.run('ai_mwum51zl',date(2026,11,23),params={'top_n':5})
        self.assertEqual([r['symbol'] for r in result.rows],[f'6000{i:02}.SH' for i in range(1,6)])

if __name__=='__main__': unittest.main()
