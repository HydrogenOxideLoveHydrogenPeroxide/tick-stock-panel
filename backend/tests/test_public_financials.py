import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, Mock
import httpx
import polars as pl
from app.services import public_financials as pf, dividend_screen as ds
from app.strategy.engine import StrategyEngine

class FinancialSourceTests(unittest.TestCase):
    def client(self,handler):return pf.EastmoneyFinancials(httpx.Client(transport=httpx.MockTransport(handler)))

    def test_income_mapping_keeps_missing_and_units(self):
        row={'SECUCODE':'600829.SH','REPORT_DATE':'2026-09-30','NOTICE_DATE':'2026-10-20',
             'PARENT_NETPROFIT':123000000,'TOTAL_OPERATE_INCOME':5000000000}
        source=self.client(lambda req:httpx.Response(200,json={'success':True,'result':{'data':[row],'pages':1,'count':1}}))
        frame,meta=source.fetch_table('income')
        self.assertEqual(frame['net_income_attributable'][0],123000000)
        self.assertIsNone(frame['operating_cost'][0])
        self.assertFalse(meta['point_in_time'])
        self.assertTrue(frame['observed_at'][0])

    def test_repeated_page_rejected(self):
        source=self.client(lambda req:httpx.Response(200,json={'success':True,'result':{'data':[{'id':1}],'pages':2,'count':2}}))
        with self.assertRaisesRegex(ValueError,'分页不完整'):source.records('RPT_LICO_FN_CPD')

    def test_non_json_and_error_state_not_empty_success(self):
        for response in [httpx.Response(200,text='alert(1)'),httpx.Response(200,json={'success':False}),httpx.Response(302)]:
            source=self.client(lambda req:response)
            with patch.object(pf.time,'sleep'),self.assertRaises((ValueError,httpx.HTTPError)):
                source.page('RPT_LICO_FN_CPD')

    def test_multi_column_sort_has_matching_directions(self):
        def handler(request):
            self.assertEqual(request.url.params['sortTypes'],'-1,-1')
            return httpx.Response(200,json={'success':True,'result':{'data':[],'pages':0,'count':0}})
        self.client(handler).records('RPT_SHAREBONUS_DET',sort='EX_DIVIDEND_DATE,SECURITY_CODE')

    def test_unknown_report_rejected_before_network(self):
        with self.assertRaises(ValueError):self.client(lambda req: self.fail('network called')).page('bad')

    def test_sync_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'financials/income/part.parquet';pf.atomic_parquet(path,pl.DataFrame({'symbol':['600829.SH']}))
            before=path.read_bytes()
            with patch.object(pf.EastmoneyFinancials,'fetch_table',side_effect=ValueError('upstream failed')):
                with self.assertRaises(ValueError):pf.sync_table(Path(tmp),'income')
            self.assertEqual(path.read_bytes(),before)

    def test_sync_merges_periods_and_archives_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for period,value in [('2026-06-30',1.),('2026-09-30',2.),('2026-09-30',3.)]:
                frame=pl.DataFrame({'symbol':['600829.SH'],'period_end':[period],'revenue':[value]})
                with patch.object(pf.EastmoneyFinancials,'fetch_table',return_value=(frame,{'point_in_time':False})):
                    pf.sync_table(root,'income')
            out=pl.read_parquet(root/'financials/income/part.parquet')
            self.assertEqual(out.height,2)
            self.assertEqual(out.filter(pl.col('period_end')=='2026-09-30')['revenue'][0],3.)
            self.assertEqual(len(list((root/'financials/archives/income').glob('*.parquet'))),3)

class FreeTierIntegrationTests(unittest.TestCase):
    def test_public_financial_api_and_scheduler_work_without_paid_capability(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.financials import router
        from app.tickflow.capabilities import CapabilitySet
        from app.services.financial_sync import FinancialScheduler
        with tempfile.TemporaryDirectory() as tmp, patch.object(pf,'enabled',return_value=True):
            root=Path(tmp)
            pf.atomic_parquet(root/'financials/income/part.parquet',pl.DataFrame({'symbol':['600829.SH'],'period_end':['2026-09-30'],'net_income_attributable':[123.]}))
            app=FastAPI();app.include_router(router)
            app.state.capabilities=CapabilitySet()
            app.state.repo=SimpleNamespace(store=SimpleNamespace(data_dir=root))
            with TestClient(app) as client:
                status=client.get('/api/financials/status')
                self.assertEqual(status.status_code,200)
                self.assertTrue(status.json()['available'])
                self.assertEqual(status.json()['provider'],'eastmoney')
                income=client.get('/api/financials/income?symbol=600829.SH')
                self.assertEqual(income.status_code,200)
                self.assertEqual(income.json()['data'][0]['net_income_attributable'],123.)
            scheduler=FinancialScheduler();scheduler._data_dir=root;scheduler._capset=CapabilitySet()
            with patch.object(pf,'sync_table',return_value=10),patch.object(scheduler,'_record_sync'):
                self.assertEqual(scheduler.run_now('income'),{'income':10})

class DividendSnapshotTests(unittest.TestCase):
    def event(self,**kwargs):return {'SECUCODE':'600829.SH','EX_DIVIDEND_DATE':'2026-05-01','REPORT_DATE':'2025-12-31',
        'ASSIGN_PROGRESS':'实施分配','PRETAX_BONUS_RMB':2,'BONUS_IT_RATIO':0,**kwargs}

    def test_per_ten_units_and_only_implemented_elapsed_dates(self):
        events=[self.event(),self.event(EX_DIVIDEND_DATE='2026-12-01'),self.event(ASSIGN_PROGRESS='预案',EX_DIVIDEND_DATE='2026-06-01'),
                self.event(EX_DIVIDEND_DATE='2025-11-24'),self.event(SECUCODE='518800.SH')]
        self.assertEqual(ds.cash_dividends(events,date(2026,11,24)),{'600829.SH':.2})

    def test_bonus_adjustment_and_duplicate_rejected(self):
        event=self.event(BONUS_IT_RATIO=10)
        self.assertEqual(ds.cash_dividends([event],date(2026,11,24)),{'600829.SH':.1})
        with self.assertRaisesRegex(ValueError,'重复'):ds.cash_dividends([event,event],date(2026,11,24))

    def test_explicit_pure_share_transfer_has_no_cash_but_adjusts_older_cash(self):
        events=[self.event(),self.event(EX_DIVIDEND_DATE='2026-06-01',REPORT_DATE='2026-03-31',PRETAX_BONUS_RMB=None,BONUS_IT_RATIO=4,IMPL_PLAN_PROFILE='10转4股')]
        self.assertAlmostEqual(ds.cash_dividends(events,date(2026,11,24))['600829.SH'],.2/1.4)

    def test_missing_amount_not_fabricated_as_zero(self):
        with self.assertRaises(ValueError):ds.cash_dividends([self.event(PRETAX_BONUS_RMB=None)],date(2026,11,24))

    def test_old_snapshot_cannot_be_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/ds.RELATIVE;pf.atomic_json(folder/'latest.json',{'as_of':'2020-01-02'})
            with self.assertRaisesRegex(ValueError,'不允许历史回测'):
                ds.load_history(date(2020,1,1),date(2020,1,2),data_dir=tmp)

    def test_current_only_backtest_rejected_before_price_loading(self):
        from app.backtest.strategy import StrategyBacktestService
        from app.backtest.strategy import StrategyBacktestConfig
        root=Path(__file__).resolve().parents[1]
        engine=StrategyEngine(lambda d: self.fail('prices loaded'),strategy_files=[root/'app/strategy/reviewed/ai_mwum51zl.py'])
        # Method only needs these attributes before returning the early error.
        service=StrategyBacktestService.__new__(StrategyBacktestService)
        service.strategy_engine=engine
        result=service.run(StrategyBacktestConfig(strategy_id='ai_mwum51zl',symbols=['600829.SH'],start=date(2020,1,1),end=date(2020,2,1)))
        self.assertIn('不支持历史回测',result.error)

if __name__=='__main__':unittest.main()
