"""Synthetic accounting fixtures only; no actual company valuation or performance claim."""
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import polars as pl
from pydantic import ValidationError
from app.services import buffett_factors as b
from app.backtest.factor import FactorBacktestService, FactorConfig, FACTOR_COLUMNS

D = datetime(2025, 6, 2, 15, tzinfo=b.SHANGHAI)


def fixture(n=5):
    reports=[];markets=[]
    for i in range(n):
        symbol=f'{600001+i}.SH'
        for year in range(2019,2025):
            known=datetime(year+1,4,30,10,tzinfo=b.SHANGHAI)
            reports.append(dict(symbol=symbol,report_date=date(year,12,31),disclosed_at=known,observed_at=known,
                source='https://example.com/synthetic-report',company_type='non_financial',industry='manufacturing',
                revenue=1000.,net_profit=100.,operating_cash_flow=140.,capex_cash_paid=40.,
                total_assets=1000.,total_liabilities=400.,total_equity=600.))
        markets.append(dict(symbol=symbol,as_of=D,observed_at=D,source='https://example.com/synthetic-cap',market_cap_cny=1000.*(i+1)))
    return b.FactorDataset(statements=reports,market_values=markets)


class BuffettFactorTests(unittest.TestCase):
    def test_formula_and_peer_rank_are_reproducible(self):
        out=b.evaluate_dataset(fixture(),D)
        a,z=out['rows'][0],out['rows'][-1]
        self.assertAlmostEqual(100/600,a['factors']['bv_roe'])
        self.assertAlmostEqual(1.4,a['factors']['bv_cash_conversion'])
        self.assertAlmostEqual(.1,a['factors']['bv_fcf_yield'])
        self.assertAlmostEqual(.6,a['factors']['bv_balance_safety'])
        self.assertEqual(0,a['factors']['bv_margin_stability'])
        self.assertAlmostEqual(65,a['score']);self.assertAlmostEqual(35,z['score'])
        self.assertFalse(out['validated_for_live'])

    def test_money_scale_invariance(self):
        data=fixture();before=b.evaluate_dataset(data,D)
        for r in data.statements:
            for key in ('revenue','net_profit','operating_cash_flow','capex_cash_paid','total_assets','total_equity','total_liabilities'):
                setattr(r,key,getattr(r,key)*10000)
        for r in data.market_values:r.market_cap_cny*=10000
        after=b.evaluate_dataset(data,D)
        self.assertEqual(before['rows'][0]['factors'],after['rows'][0]['factors'])

    def test_today_observation_never_backfills_old_disclosure(self):
        data=fixture()
        for r in data.statements:r.observed_at=D+timedelta(days=1)
        self.assertTrue(all(r['factors'] is None for r in b.evaluate_dataset(data,D)['rows']))

    def test_later_revision_does_not_overwrite_known_original(self):
        data=fixture(); original=b.evaluate_dataset(data,D)['rows'][0]['factors']
        late=data.statements[5].model_copy(deep=True)
        late.revised_at=D+timedelta(days=2);late.observed_at=late.revised_at;late.net_profit=10000.
        data.statements.append(late)
        self.assertEqual(original,b.evaluate_dataset(data,D)['rows'][0]['factors'])
        self.assertNotEqual(original,b.evaluate_dataset(data,D+timedelta(days=3))['rows'][0]['factors'])

    def test_market_must_be_known_fresh_total_cap(self):
        data=fixture()
        data.market_values[0].observed_at=D+timedelta(seconds=1)
        self.assertIsNone(b.evaluate_dataset(data,D)['rows'][0]['factors'])
        self.assertTrue(all(r['factors'] is None for r in b.evaluate_dataset(fixture(),D+timedelta(days=8))['rows']))
        with self.assertRaises(ValidationError):
            b.MarketValue(**{**fixture().market_values[0].model_dump(),'cap_basis':'float_cap'})

    def test_missing_capex_loss_negative_equity_and_units_fail_closed(self):
        for key,value in [('capex_cash_paid',None),('net_profit',0.),('total_equity',-1.),('total_assets',10000000.)]:
            data=fixture();setattr(data.statements[5],key,value)
            row=b.evaluate_dataset(data,D)['rows'][0]
            self.assertIsNone(row['factors'],(key,row))
            self.assertTrue(row['reason'])
        r=fixture().statements[0].model_dump()
        for change in [dict(amount_unit='wan_yuan'),dict(currency='USD'),dict(capex_cash_paid=-1),dict(net_profit=float('nan')),dict(report_date=date(2019,9,30)),dict(observed_at=D.replace(tzinfo=None)),dict(symbol='513100.SH')]:
            with self.assertRaises(ValidationError):b.AnnualStatement(**{**r,**change})

    def test_banks_missing_years_and_stale_statements_are_blocked(self):
        data=fixture();data.statements[5].company_type='bank'
        self.assertIsNone(b.evaluate_dataset(data,D)['rows'][0]['factors'])
        data=fixture();data.statements.pop(2)
        self.assertIsNone(b.evaluate_dataset(data,D)['rows'][0]['factors'])
        later=datetime(2026,7,5,15,tzinfo=b.SHANGHAI)
        data=fixture();data.market_values[0].as_of=later;data.market_values[0].observed_at=later
        self.assertIn('过旧',b.evaluate_dataset(data,later)['rows'][0]['reason'])

    def test_industry_minimum_and_ties_do_not_create_fake_strength(self):
        data=fixture(4)
        rows=b.evaluate_dataset(data,D)['rows']
        self.assertTrue(all(r['factors'] is not None and r['score'] is None for r in rows))
        data=fixture(5)
        for r in data.market_values:r.market_cap_cny=1000.
        self.assertEqual([50.]*5,[r['score'] for r in b.evaluate_dataset(data,D)['rows']])
        data.statements[5].industry='different'
        self.assertTrue(all(r['score'] is None for r in b.evaluate_dataset(data,D)['rows']))

    def test_duplicate_versions_and_naive_decision_rejected(self):
        data=fixture();data.statements.append(data.statements[0])
        with self.assertRaises(ValidationError):b.FactorDataset.model_validate(data.model_dump())
        with self.assertRaises(ValueError):b.evaluate_dataset(fixture(),D.replace(tzinfo=None))

    def test_panel_only_carries_information_forward_after_known_time(self):
        data=fixture();days=[D.date()-timedelta(days=1),D.date(),D.date()+timedelta(days=1)]
        panel=b.build_factor_panel(data,days)
        self.assertEqual(10,panel.height)
        self.assertNotIn(days[0],panel['date'].to_list())
        self.assertEqual(5,len(panel.filter(pl.col('date')==days[1])))

    def test_backtest_missing_data_blocks_before_price_load(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=Path(directory))),load_panel=Mock())
            result=FactorBacktestService(engine).run(FactorConfig(factor_name=b.COMPOSITE,symbols=None,start=D.date(),end=D.date()+timedelta(days=4)))
            self.assertIn('不会用今日数据倒填历史',result.error)
            engine.load_panel.assert_not_called()
            self.assertEqual('blocked_missing_data',b.dataset_status(Path(directory))['status'])

    def test_factor_gaps_do_not_turn_multi_day_moves_into_one_day_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/b.DATASET_RELATIVE;path.parent.mkdir()
            data=fixture(10)
            for market in list(data.market_values):
                fresh=market.model_copy(deep=True);fresh.as_of=D+timedelta(days=9);fresh.observed_at=fresh.as_of
                data.market_values.append(fresh)
            path.write_text(data.model_dump_json())
            prices=pl.DataFrame([dict(symbol=f'{600001+i}.SH',date=D.date()+timedelta(days=j),close=10.+j*(i+1)*.01,open=10.,high=11.,low=9.,volume=1000.,turnover_rate=1.) for i in range(10) for j in range(11)])
            engine=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=root)),load_panel=lambda *a,**kw:prices)
            with patch.object(FactorBacktestService,'_calc_ic', wraps=FactorBacktestService._calc_ic) as ic:
                result=FactorBacktestService(engine).run(FactorConfig(factor_name=b.COMPOSITE,symbols=None,start=D.date(),end=D.date()+timedelta(days=10),rebalance='daily'))
            self.assertIsNone(result.error)
            panel=ic.call_args.args[0]
            row=panel.filter((pl.col('symbol')=='600001.SH') & (pl.col('date')==D.date()+timedelta(days=7))).to_dicts()[0]
            self.assertAlmostEqual(10.08/10.07-1,row['_next_return'])
            self.assertNotIn(D.date()+timedelta(days=8),panel['date'].to_list())

    def test_tied_factors_keep_one_group_without_crashing_or_fake_long_short(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/b.DATASET_RELATIVE;path.parent.mkdir()
            path.write_text(fixture(10).model_dump_json())
            prices=pl.DataFrame([dict(symbol=f'{600001+i}.SH',date=D.date()+timedelta(days=j),close=10.+j*.01,open=10.,high=11.,low=9.,volume=1000.,turnover_rate=1.) for i in range(10) for j in range(5)])
            engine=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=root)),load_panel=lambda *a,**kw:prices)
            result=FactorBacktestService(engine).run(FactorConfig(factor_name='bv_margin_stability',symbols=None,start=D.date(),end=D.date()+timedelta(days=4),rebalance='daily'))
            self.assertIsNone(result.error)
            self.assertEqual({},result.long_short_stats)
            self.assertTrue(all(set(row)=={'date','Q3'} for row in result.group_nav))

    def test_snapshot_endpoint_is_read_only_and_missing_dataset_is_explicit(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.quant_assistant import router
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=FastAPI();app.include_router(router)
            app.state.repo=SimpleNamespace(store=SimpleNamespace(data_dir=root))
            client=TestClient(app)
            missing=client.post('/api/quant-assistant/value-factors/evaluate',json={})
            self.assertEqual(422,missing.status_code)
            path=root/b.DATASET_RELATIVE;path.parent.mkdir();path.write_text(fixture().model_dump_json())
            before=path.read_bytes()
            response=client.post('/api/quant-assistant/value-factors/evaluate',json={'decision_at':D.isoformat()})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(5,len(response.json()['rows']))
            self.assertEqual(before,path.read_bytes())
            self.assertFalse((root/'user_data/quant_assistant.json').exists())
            naive=client.post('/api/quant-assistant/value-factors/evaluate',json={'decision_at':'2025-06-02T15:00:00'})
            self.assertEqual(422,naive.status_code)

    def test_real_factor_service_consumes_versioned_factors(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/b.DATASET_RELATIVE;path.parent.mkdir()
            data=fixture(10);path.write_text(data.model_dump_json())
            prices=pl.DataFrame([dict(symbol=f'{600001+i}.SH',date=D.date()+timedelta(days=j),close=10.+j*(i+1)*.01,open=10.,high=11.,low=9.,volume=1000.,turnover_rate=1.) for i in range(10) for j in range(5)])
            def load(*args,**kwargs):
                self.assertNotIn(b.COMPOSITE,kwargs['columns'])
                return prices
            engine=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=root)),load_panel=load)
            result=FactorBacktestService(engine).run(FactorConfig(factor_name=b.COMPOSITE,symbols=None,start=D.date(),end=D.date()+timedelta(days=4),rebalance='daily'))
            self.assertIsNone(result.error,result.error)
            self.assertEqual(10,result.n_symbols)
            self.assertTrue(result.ic_series)
            self.assertTrue(b.FACTOR_IDS <= {r['id'] for r in FACTOR_COLUMNS})
