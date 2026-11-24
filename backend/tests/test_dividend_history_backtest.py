import unittest
from datetime import date
from app.research.dividend_history import capital_at,monthly_dates
from app.research.dividend_simulator import Config,fees,executable,simulate

D=date.fromisoformat

def bar(d,price=4.,open_=None,preclose=4.,volume=1000000,st='0',status='1'):
    return {'date':str(d),'open':str(price if open_ is None else open_),'close':str(price),'preclose':str(preclose),
            'volume':str(volume),'isST':st,'tradestatus':status}

def prices(dates,**changes):
    result={D('2020-12-'+str(i)):bar(D('2020-12-'+str(i))) for i in range(21,26)}
    result.update({d:bar(d,**changes) for d in dates});return result

def target(trade,code='600001.SH'):
    rows=[{'symbol':code,'revision_unverified':False}]
    return {'trade_date':str(trade),'ranked':rows}

class TemporalTests(unittest.TestCase):
    def test_capital_requires_both_disclosure_and_effective_date(self):
        rows=[{'VARYDATE':'2020-01-01','DECLAREDATE':'2020-01-02','F003N':100},
              {'VARYDATE':'2020-02-01','DECLAREDATE':'2020-03-01','F003N':200},
              {'VARYDATE':'2020-04-01','DECLAREDATE':'2020-03-01','F003N':300},
              {'VARYDATE':'2020-01-03','DECLAREDATE':None,'F003N':999}]
        self.assertEqual(capital_at(rows,D('2020-02-02'))['total_shares'],1000000)
        self.assertEqual(capital_at(rows,D('2020-03-15'))['total_shares'],2000000)
        self.assertEqual(capital_at(rows,D('2020-04-01'))['total_shares'],3000000)

    def test_late_old_period_filing_does_not_undo_newer_capital_change(self):
        rows=[{'VARYDATE':'2020-01-01','DECLAREDATE':'2020-01-02','F003N':100},
              {'VARYDATE':'2020-02-01','DECLAREDATE':'2020-02-01','F003N':200},
              {'VARYDATE':'2020-01-01','DECLAREDATE':'2020-04-01','F003N':100}]
        self.assertEqual(capital_at(rows,D('2020-05-01'))['total_shares'],2000000)

    def test_reusing_archive_for_a_longer_period_is_rejected(self):
        import tempfile
        from app.research.dividend_history import freeze_spec
        with tempfile.TemporaryDirectory() as folder:
            freeze_spec(folder,D('2021-01-01'),D('2026-11-24'))
            with self.assertRaisesRegex(ValueError,'冻结'):
                freeze_spec(folder,D('2020-01-01'),D('2026-11-24'))

    def test_monthly_signal_uses_prior_trading_day_including_december(self):
        days=['2020-12-31','2021-01-04','2021-01-05','2021-01-29','2021-02-01']
        _,months=monthly_dates([{'calendar_date':d,'is_trading_day':'1'} for d in days],D('2021-01-01'),D('2021-02-01'))
        self.assertEqual(months,[{'trade_date':'2021-01-04','signal_date':'2020-12-31'},{'trade_date':'2021-02-01','signal_date':'2021-01-29'}])

class MatchingTests(unittest.TestCase):
    def test_historical_stamp_and_transfer_rates(self):
        config=Config()
        old=fees(100000,D('2021-01-01'),'sell',config)
        new=fees(100000,D('2024-01-01'),'sell',config)
        self.assertEqual(old['stamp'],100);self.assertEqual(new['stamp'],50)
        self.assertEqual(old['transfer'],2);self.assertEqual(new['transfer'],1)
        self.assertEqual(fees(1000,D('2024-01-01'),'buy',config)['commission'],5)

    def test_limit_prices_and_suspension_are_conservative(self):
        d=D('2021-01-04')
        self.assertFalse(executable(bar(d,open_=4.4),'buy')[0])
        self.assertFalse(executable(bar(d,open_=3.6),'sell')[0])
        self.assertFalse(executable(bar(d,open_=4.2,st='1'),'buy')[0])
        self.assertFalse(executable(bar(d,status='0'),'sell')[0])
        self.assertTrue(executable(bar(d,open_=4.39),'buy')[0])

    def test_delayed_exit_is_retried_without_inventing_fill(self):
        dates=list(map(D,['2021-01-04','2021-01-05','2021-02-01','2021-02-02']))
        first=prices(dates);first[dates[2]]=bar(dates[2],price=3.6,preclose=4.)
        first[dates[3]]=bar(dates[3],price=3.7,preclose=3.6)
        other=prices(dates)
        r=simulate(dates,[target(dates[0]),target(dates[2],'600002.SH')],{'600001.SH':first,'600002.SH':other},[],{},Config(top_n=1))
        exits=[t for t in r['trades'] if t['side']=='sell']
        self.assertEqual(exits[0]['date'],'2021-02-02')
        self.assertTrue(any(b['reason'].startswith('开盘跌停') for b in r['blocked']))
        self.assertGreaterEqual(min(row['cash'] for row in r['curve']),0)
        self.assertTrue(all(t['shares']%100==0 for t in r['trades']))

    def test_cash_dividend_preserves_nav_and_waits_for_payment(self):
        dates=list(map(D,['2021-01-04','2021-01-05','2021-01-06']))
        history=prices(dates);history[dates[1]]=bar(dates[1],price=3.,preclose=3.);history[dates[2]]=bar(dates[2],price=3.,preclose=3.)
        action={'symbol':'600001.SH','record':dates[0],'ex':dates[1],'pay':dates[2],'cash':1.,'bonus':0.,'listing':None}
        result=simulate(dates,[target(dates[0])],{'600001.SH':history},[action],{},Config(top_n=1,slippage_bps=0,dividend_tax=0))
        self.assertAlmostEqual(result['curve'][0]['equity'],result['curve'][1]['equity'])
        self.assertGreater(result['curve'][1]['receivables'],0)
        self.assertEqual(result['curve'][2]['receivables'],0)
        self.assertAlmostEqual(result['curve'][1]['equity'],result['curve'][2]['equity'])
        self.assertGreater(result['curve'][2]['cash'],result['curve'][1]['cash'])

    def test_ex_date_buyer_gets_no_previous_dividend(self):
        dates=list(map(D,['2021-01-04','2021-01-05','2021-01-06']))
        action={'symbol':'600001.SH','record':dates[0],'ex':dates[1],'pay':dates[2],'cash':1.,'bonus':0.,'listing':None}
        r=simulate(dates,[target(dates[1])],{'600001.SH':prices(dates)},[action],{},Config(top_n=1))
        self.assertEqual(r['stats']['gross_cash_dividends'],0)

    def test_stock_bonus_is_valued_on_ex_day_but_locked_until_listing(self):
        dates=list(map(D,['2021-01-04','2021-01-05','2021-01-06']))
        history=prices(dates);history[dates[1]]=bar(dates[1],price=2.,preclose=2.);history[dates[2]]=bar(dates[2],price=2.,preclose=2.)
        action={'symbol':'600001.SH','record':dates[0],'ex':dates[1],'pay':None,'cash':0.,'bonus':1.,'listing':dates[2]}
        r=simulate(dates,[target(dates[0])],{'600001.SH':history},[action],{},Config(top_n=1,slippage_bps=0))
        self.assertAlmostEqual(r['curve'][0]['equity'],r['curve'][1]['equity'])
        self.assertGreater(r['corporate_actions'][0]['bonus_shares'],0)

    def test_missing_held_price_is_not_flattened_to_zero_return(self):
        dates=list(map(D,['2021-01-04','2021-01-05']))
        history=prices(dates);del history[dates[1]]
        with self.assertRaisesRegex(ValueError,'缺少持仓日线'):
            simulate(dates,[target(dates[0])],{'600001.SH':history},[],{},Config(top_n=1))

class RetiredUniverseTests(unittest.TestCase):
    def test_today_delisted_stock_remains_in_earlier_candidates_but_unknown_profit_never_qualifies(self):
        import tempfile
        from pathlib import Path
        from datetime import timedelta
        from unittest.mock import patch
        from app.services.public_financials import atomic_json
        from app.research import dividend_history as hist
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calendar=[];d=D('2020-01-01')
            while d<=D('2021-01-05'):
                calendar.append({'calendar_date':str(d),'is_trading_day':'1' if d.weekday()<5 else '0'});d+=timedelta(days=1)
            atomic_json(root/'calendar/all.json',{'rows':calendar})
            atomic_json(root/'basic/all.json',{'rows':[{'code':'sh.600001','ipoDate':'2000-01-01','outDate':'2022-01-01','type':'1','status':'0'}]})
            atomic_json(root/'dividends.json',{'rows':[]})
            atomic_json(root/'delisted_dividends.json',{'rows':[{'SECUCODE':'600001.SH','EX_DIVIDEND_DATE':'2020-07-01','REPORT_DATE':None,'ASSIGN_PROGRESS':'实施分配','PRETAX_BONUS_RMB':2,'BONUS_IT_RATIO':0}]})
            row={'date':'2020-12-31','code':'sh.600001','close':'4','isST':'0','tradestatus':'1','delisted_supplement':True}
            atomic_json(root/'monthly/2020-12-31.json',{'rows':[row]})
            needed=hist.prepare_candidates(root,D('2021-01-01'),D('2021-01-05'),lambda *args:None)
            self.assertIn('600001.SH',needed)
            candidate=hist.read(root/'candidates.json')['rows'][0]['candidates'][0]
            self.assertTrue(candidate['requires_profit_check'])
            atomic_json(root/'capital/600001.SH.json',{'rows':[{'VARYDATE':'2020-01-01','DECLAREDATE':'2020-01-02','F003N':100}]})
            hist.rank_months(root,lambda *args:None)
            atomic_json(root/'states/600001.SH.json',{'rows':[row]})
            with patch.object(hist,'parallel_bao',side_effect=lambda tasks,progress:self.assertEqual(tasks,[])):
                with self.assertRaisesRegex(ValueError,'需要补充归母净利润'):
                    hist.verify_status(root,D('2021-01-01'),D('2021-01-05'),lambda *args:None)

class DividendDataTests(unittest.TestCase):
    def test_duplicate_vendor_dividends_are_paid_only_once(self):
        import tempfile
        from pathlib import Path
        from app.services.public_financials import atomic_json
        from app.research.dividend_simulator import normalize_actions
        raw={'dividOperateDate':'2021-06-02','dividRegistDate':'2021-06-01','dividPayDate':'2021-06-02','dividCashPsBeforeTax':'0.2','dividStocksPs':'0','dividReserveToStockPs':'','dividStockMarketDate':''}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            atomic_json(root/'dividends.json',{'rows':[{'SECUCODE':'600001.SH','EX_DIVIDEND_DATE':'2021-06-02','EQUITY_RECORD_DATE':'2021-06-01','PRETAX_BONUS_RMB':2,'ASSIGN_PROGRESS':'实施分配'}]})
            atomic_json(root/'actions/600001.SH_2021.json',{'rows':[{**raw,'dividCashPsBeforeTax':''},raw,raw]})
            result=normalize_actions(root,{'600001.SH'},D('2021-01-01'),D('2021-12-31'),[])
            self.assertEqual(len(result),1)
            self.assertEqual(result[0]['cash'],.2)

class AdapterTests(unittest.TestCase):
    def test_rejects_changed_strategy_and_current_stock_subset_before_loading_data(self):
        from types import SimpleNamespace
        from app.backtest.strategy import StrategyBacktestConfig
        from app.research.dividend_adapter import build_result
        strategy=SimpleNamespace(meta={'params':[{'id':'top_n','default':10},{'id':'dividend_yield_min','default':.03}]})
        for overrides in [dict(symbols=['600001.SH']),dict(mode='full'),dict(params={'top_n':5.5}),dict(params={'dividend_yield_min':.02}),dict(overrides={'stop_loss':-.08}),dict(slippage_bps=float('nan'))]:
            values={'strategy_id':'ai_mwum51zl','symbols':None,'start':D('2021-01-01'),'end':D('2021-02-01'),**overrides}
            with self.subTest(overrides=overrides),self.assertRaises(ValueError):
                build_result(StrategyBacktestConfig(**values),'/does-not-exist',strategy)

    def test_history_not_claimed_available_without_archive(self):
        import tempfile
        from app.research.dividend_adapter import history_info
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(history_info(folder)['archive_available'])
            self.assertIsNone(history_info(folder)['earliest_date'])

if __name__=='__main__':unittest.main()
