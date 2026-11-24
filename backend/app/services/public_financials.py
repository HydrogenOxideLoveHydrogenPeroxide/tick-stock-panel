"""Public Eastmoney financial statements adapted from AKShare (MIT).

Protocol reference pinned in docs/public-financials.md. No remote Python/JS is
executed. These are observed current revisions, NOT archived historical PIT.
Amounts are CNY; margin/growth/ROE fields retain percentage-point units.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx
import polars as pl

URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
SHANGHAI = ZoneInfo('Asia/Shanghai')
REPORTS = {'metrics': ('RPT_LICO_FN_CPD','REPORTDATE'),
           'income': ('RPT_DMSK_FN_INCOME','REPORT_DATE'),
           'balance_sheet': ('RPT_DMSK_FN_BALANCE','REPORT_DATE'),
           'cash_flow': ('RPT_DMSK_FN_CASHFLOW','REPORT_DATE')}
FIELDS = {
 'metrics': {'eps_basic':'BASIC_EPS','bps':'BPS','ocfps':'MGJYXJJE','roe':'WEIGHTAVG_ROE',
             'gross_margin':'XSMLL','revenue_yoy':'YSTZ','net_income_yoy':'SJLTZ'},
 'income': {'revenue':'TOTAL_OPERATE_INCOME','operating_cost':'OPERATE_COST','operating_profit':'OPERATE_PROFIT',
            'total_profit':'TOTAL_PROFIT','net_income_attributable':'PARENT_NETPROFIT',
            'net_income_deducted':'DEDUCT_PARENT_NETPROFIT','sales_expense':'SALE_EXPENSE',
            'admin_expense':'MANAGE_EXPENSE','financial_expense':'FINANCE_EXPENSE','income_tax':'INCOME_TAX'},
 'balance_sheet': {'total_assets':'TOTAL_ASSETS','fixed_assets':'FIXED_ASSET','cash_and_equivalents':'MONETARYFUNDS',
            'accounts_receivable':'ACCOUNTS_RECE','inventory':'INVENTORY','total_liabilities':'TOTAL_LIABILITIES',
            'accounts_payable':'ACCOUNTS_PAYABLE','total_equity':'TOTAL_EQUITY','debt_to_asset_ratio':'DEBT_ASSET_RATIO'},
 'cash_flow': {'net_operating_cash_flow':'NETCASH_OPERATE','net_investing_cash_flow':'NETCASH_INVEST',
              'net_financing_cash_flow':'NETCASH_FINANCE','capex':'CONSTRUCT_LONG_ASSET','net_cash_change':'CCE_ADD'},
}
LIMITATIONS = ['公开来源当前修订版，采集时间不等于原始披露时间；不保证历史PIT。',
               '利润和现金流为年初累计，资产负债为期末余额；未提供的科目保留缺失。']


def enabled():
    from app.config import settings
    return settings.financial_data_provider == 'eastmoney'


def available(capset):
    from app.tickflow.capabilities import Cap
    return enabled() or (capset is not None and capset.has(Cap.FINANCIAL))


def number(value):
    if value is None or isinstance(value,bool): return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError): return None


def day(value):
    try: return date.fromisoformat(str(value)[:10])
    except (ValueError,TypeError): return None


def atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.public-finance-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,ensure_ascii=False,allow_nan=False,default=str,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def atomic_parquet(path, frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.public-finance-');os.close(fd)
    try:
        frame.write_parquet(tmp);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


class EastmoneyFinancials:
    def __init__(self, client=None):
        self.client=client or httpx.Client(timeout=15,trust_env=False,follow_redirects=False,
            headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/','Accept-Encoding':'identity'})
        self.owns_client=client is None

    def close(self):
        if self.owns_client:self.client.close()

    def page(self, report, *, where='', sort='SECURITY_CODE', page=1, size=500):
        if report not in {v[0] for v in REPORTS.values()} | {'RPT_SHAREBONUS_DET'}:
            raise ValueError('未允许的公开财务报表')
        params={'reportName':report,'columns':'ALL','pageNumber':page,'pageSize':size,
                'sortColumns':sort,'sortTypes':','.join('-1' for _ in sort.split(',')),'source':'WEB','client':'WEB'}
        if where:params['filter']=where
        for attempt in range(3):
            try:
                with self.client.stream('GET',URL,params=params) as response:
                    response.raise_for_status()
                    if response.status_code != 200:raise ValueError('财务源返回非200状态')
                    payload=bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload)>16*1024*1024:raise ValueError('财务响应超过16MiB')
                data=json.loads(payload)
                if not data.get('success'):
                    raise ValueError('公开财务源拒绝查询或报表格式变化')
                result=data.get('result')
                if result is None:return {'data':[],'pages':0,'count':0}
                if not isinstance(result,dict) or not isinstance(result.get('data'),list):
                    raise ValueError('公开财务源缺少列表')
                return result
            except (httpx.HTTPError,ValueError):
                if attempt==2:raise
                time.sleep(.5*(attempt+1))
        raise RuntimeError('unreachable')

    def records(self,report,*,where='',sort='SECURITY_CODE'):
        rows=[];seen=set();expected=None;pages=None
        for page in range(1,201):
            data=self.page(report,where=where,sort=sort,page=page)
            if expected is None:expected=int(data.get('count',0));pages=int(data.get('pages',0))
            if expected>100000 or pages>200:raise ValueError('财务查询超过10万条边界')
            before=len(rows)
            for row in data['data']:
                key=json.dumps(row,sort_keys=True,ensure_ascii=False)
                if key not in seen:seen.add(key);rows.append(row)
            if len(rows)>=expected:return rows
            if len(rows)==before or page>=pages:raise ValueError('财务分页不完整，保留旧数据')
            time.sleep(.15)
        raise ValueError('财务分页超过边界')

    def fetch_table(self,table):
        report,date_key=REPORTS[table]
        latest=self.page(report,sort=date_key,size=1)['data']
        if not latest:raise ValueError('公开财务源暂无报表，保留旧数据')
        period=day(latest[0].get(date_key))
        if period is None or period>datetime.now(SHANGHAI).date():raise ValueError('公开财务报告期无效')
        rows=self.records(report,where=f'({date_key}=\'{period}\')')
        stamp=datetime.now(SHANGHAI).isoformat()
        normalized=[]
        for row in rows:
            symbol=row.get('SECUCODE','')
            if not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)',symbol):continue
            report_date=day(row.get(date_key));notice=day(row.get('NOTICE_DATE'))
            if report_date!=period or notice is None or notice>datetime.now(SHANGHAI).date():continue
            currency=row.get('CURRENCY')
            if currency and currency!='CNY':continue
            item={'symbol':symbol,'period_end':str(period),'announce_date':str(notice),
                  'source':'eastmoney/AKShare-adapted','observed_at':stamp,'point_in_time':False,
                  'source_updated_date':str(day(row.get('UPDATE_DATE')) or notice),'amount_unit':'CNY'}
            item.update({key:number(row.get(field)) for key,field in FIELDS[table].items()})
            normalized.append(item)
        if not normalized:raise ValueError('公开财务源没有可用A股记录，保留旧数据')
        return pl.DataFrame(normalized,infer_schema_length=None),{'provider':'eastmoney','report':report,'period_end':str(period),
               'observed_at':stamp,'source_rows':len(rows),'point_in_time':False,'limitations':LIMITATIONS}


def sync_table(data_dir,table):
    source=EastmoneyFinancials()
    try:frame,meta=source.fetch_table(table)
    finally:source.close()
    path=Path(data_dir)/'financials'/table/'part.parquet'
    # Keep previously collected periods; current revisions replace matching keys,
    # while original observation records remain in immutable per-run archives.
    archive=Path(data_dir)/'financials'/'archives'/table/(datetime.now(SHANGHAI).strftime('%Y%m%dT%H%M%S%f')+'.parquet')
    atomic_parquet(archive,frame)
    if path.exists():
        old=pl.read_parquet(path)
        frame=pl.concat([old,frame],how='diagonal_relaxed').unique(['symbol','period_end'],keep='last',maintain_order=True)
    atomic_parquet(path,frame)
    atomic_json(path.with_suffix('.source.json'),{**meta,'stored_rows':frame.height,'archive':str(archive.relative_to(data_dir))})
    return frame.height
