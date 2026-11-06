from datetime import datetime,timedelta
from pathlib import Path
import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch,AsyncMock
from app.services import fundamental_sync as s
from app.services.buffett_factors import FactorDataset,load_dataset,MarketValue,SHANGHAI
from test_buffett_factors import fixture,D


def bundle():
    data=fixture(1);tables={k:dict(source_url='https://example.com/report',items=[]) for k in ('balance_sheet','income_statement','cash_flow')}
    for r in data.statements:
        base=dict(code='600001',report_date=str(r.report_date),disclosed_date=str(r.disclosed_at.date()),updated_date=str(r.disclosed_at.date()),currency='CNY',amount_unit='元')
        tables['balance_sheet']['items'].append(dict(base,basis='period_end',total_assets=r.total_assets,total_liabilities=r.total_liabilities,total_equity=r.total_equity))
        tables['income_statement']['items'].append(dict(base,basis='year_to_date',revenue=r.revenue,net_profit=r.net_profit))
        tables['cash_flow']['items'].append(dict(base,basis='year_to_date',operating_cash_flow=r.operating_cash_flow,source_fields={'CONSTRUCT_LONG_ASSET':r.capex_cash_paid}))
    return dict(data=tables,provenance={k:dict(fetched_at=D.isoformat()) for k in tables},status='success',errors=[])

INFO=dict(industry='白酒Ⅱ',name='合成公司',source='https://example.com/info',observed_at=D)

class FundamentalSyncTests(unittest.TestCase):
    def test_code_normalization_and_batch_limits(self):
        self.assertEqual(['600519.SH','000858.SZ'],s.SyncRequest.normalized(['600519','600519.SH','000858']))
        for codes in [[],['600001.SH']*21]:
            with self.assertRaises(ValueError):s.SyncRequest(symbols=codes)
        for code in ['513100.SH','600519.SZ','$(touch x)','../600519']:
            with self.assertRaises(ValueError):s.SyncRequest.normalized([code])

    def test_normalization_keeps_observation_and_industry_time_not_old_disclosure(self):
        later=dict(INFO,observed_at=D+timedelta(days=1))
        rows=s.normalize_statements('600001.SH',bundle(),later)
        self.assertEqual(6,len(rows))
        self.assertTrue(all(row.available_at>=later['observed_at'] for row in rows))
        self.assertTrue(all(row.capex_cash_paid==40 for row in rows))
        self.assertEqual('白酒Ⅱ',rows[-1].industry)

    def test_missing_capex_or_wrong_currency_and_identity_rejected(self):
        for field,value in [('currency','USD'),('code','000001')]:
            d=bundle();d['data']['cash_flow']['items'][0][field]=value
            with self.assertRaises(ValueError):s.normalize_statements('600001.SH',d,INFO)
        d=bundle();d['data']['cash_flow']['items'][0]['source_fields']={}
        with self.assertRaisesRegex(ValueError,'资本开支'):s.normalize_statements('600001.SH',d,INFO)
        with self.assertRaisesRegex(ValueError,'金融企业'):s.normalize_statements('600001.SH',bundle(),dict(INFO,industry='银行'))

    def test_year_gaps_and_accounting_identity_fail_closed(self):
        d=bundle();d['data']['income_statement']['items'].pop(2)
        with self.assertRaisesRegex(ValueError,'不足6'):s.normalize_statements('600001.SH',d,INFO)
        d=bundle();d['data']['balance_sheet']['items'][0]['total_assets']=1000000
        with self.assertRaisesRegex(ValueError,'恒等式'):s.normalize_statements('600001.SH',d,INFO)

    def test_atomic_merge_preserves_first_observation_and_adds_new_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rows=s.normalize_statements('600001.SH',bundle(),INFO);market=fixture(1).market_values[0]
            s.merge_dataset(root,rows,[market]);before=load_dataset(root)
            newer=[row.model_copy(update={'observed_at':D+timedelta(days=1)}) for row in rows]
            s.merge_dataset(root,newer,[market]);after=load_dataset(root)
            self.assertEqual(before.model_dump(),after.model_dump())
            revision=rows[-1].model_copy(update={'net_profit':110.,'observed_at':D+timedelta(days=1)})
            s.merge_dataset(root,[revision],[])
            self.assertEqual(7,len(load_dataset(root).statements))
            self.assertEqual(0o600,(root/'fundamentals/buffett_v1.json').stat().st_mode&0o777)

    def test_corrupt_dataset_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'fundamentals/buffett_v1.json';path.parent.mkdir();path.write_text('broken')
            with self.assertRaises(ValueError):s.merge_dataset(root,[],[])
            self.assertEqual('broken',path.read_text())

    def test_successful_and_failed_stock_results_persist_without_erasing_good_data(self):
        with tempfile.TemporaryDirectory() as directory:
            async def info(symbol):
                if symbol=='600002.SH':raise ValueError('mock source unavailable')
                return INFO
            with patch.object(s,'stock_info',side_effect=info),patch.object(s,'market_value',new=AsyncMock(return_value=fixture(1).market_values[0])),patch.object(s,'financial_bundle',return_value=(bundle(),Path('raw.json'))):
                result=s.collect(Path(directory),['600001.SH','600002.SH'])
            self.assertEqual('partial',result['status'])
            self.assertEqual(['collected','failed'],[r['status'] for r in result['results']])
            self.assertEqual(6,len(load_dataset(Path(directory)).statements))
            self.assertEqual(2,s.sync_status(Path(directory))['completed'])

    def test_restarted_and_live_process_job_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'fundamentals/sync_status.json'
            s._atomic(p,dict(status='running',owner_pid=os.getpid()))
            self.assertEqual('running',s.sync_status(root)['status'])
            with patch.object(s,'_pid_alive',return_value=False):self.assertEqual('interrupted',s.sync_status(root)['status'])

class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tencent_total_field_45_and_yi_conversion_are_not_float_cap(self):
        f=['']*53;f[2]='600001';f[30]=(D-timedelta(days=1)).strftime('%Y%m%d%H%M%S');f[44]='50';f[45]='100'
        raw=('v_sh600001="'+'~'.join(f)+'";').encode('gbk')
        with patch.object(s,'_fixed_get',new=AsyncMock(return_value=raw)):
            value=await s.market_value('600001.SH',now=D)
        self.assertEqual(10_000_000_000.,value.market_cap_cny)
        self.assertEqual(D-timedelta(days=1),value.as_of)

    async def test_stale_or_wrong_symbol_quote_rejected(self):
        for code,stamp in [('000001',D),('600001',D-timedelta(days=8))]:
            f=['']*53;f[2]=code;f[30]=stamp.strftime('%Y%m%d%H%M%S');f[44]='50';f[45]='100'
            raw=('v_sh600001="'+'~'.join(f)+'";').encode('gbk')
            with patch.object(s,'_fixed_get',new=AsyncMock(return_value=raw)):
                with self.assertRaises(ValueError):await s.market_value('600001.SH',now=D)

    async def test_unknown_host_rejected_before_any_fetch(self):
        for url in ['https://evil.example/', 'http://qt.gtimg.cn/q=sh600001','https://qt.gtimg.cn/q=sh600001#x']:
            with patch.object(s,'validate_public_https_url') as dns:
                with self.assertRaises(ValueError):await s._fixed_get(url)
                dns.assert_not_called()
