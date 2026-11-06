"""Opt-in public-source collection for current quality/value research snapshots.

Reference: invest-skills-top10/09-a-stock-data/SKILL.md §§1.1,6.3,6.4-6.7.
Reuse quant's financial bundle/cache; do not run third-party skill code. Preserve
actual observation times: this collector never manufactures historical PIT data.
"""
from __future__ import annotations
import asyncio
import base64
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import Field
from app.services.buffett_factors import AnnualStatement, MarketValue, FactorDataset, StrictModel, SHANGHAI, DATASET_RELATIVE, load_dataset, evaluate_dataset
from app.services.safe_http import validate_public_https_url

_LOCK=threading.RLock()
_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='value-data')
_ACTIVE=None
_RAW_ARCHIVE=ContextVar("fundamental_raw_archive",default=None)
_INFO_ENDPOINT='https://push2.eastmoney.com/api/qt/stock/get'
SOURCE_NOTES=[
    '三表复用quant公开财报接口，保留实际披露、修订、采集时间；当前修订版不保证原始历史PIT。',
    '腾讯字段45为总市值（亿元），44为流通市值；统一转人民币元。',
    '行业来自东方财富当前快照，不能倒填历史行业。A/H等跨市场多股类市值未单独重建。',
    '历史PE/PB/PS/PCF不是历史总市值；不从这些比率或今日股本伪造历史总市值。',
    '同一批采集股票仅为研究子样本，行业内少于5家不生成综合分，不等同全市场选股。',
]

class SyncRequest(StrictModel):
    symbols:list[str]=Field(min_length=1,max_length=20)

    @classmethod
    def normalized(cls,symbols):
        output=[]
        for value in symbols:
            value=str(value).strip().upper()
            if re.fullmatch(r'[036]\d{5}',value):value += '.SH' if value.startswith('6') else '.SZ'
            if not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)',value):
                raise ValueError('仅支持沪深A股代码；不能混入ETF或指数')
            if value not in output:output.append(value)
        return output


def _atomic(path,value):
    if path.is_symlink() or path.parent.is_symlink():raise ValueError('数据文件或目录不得为符号链接')
    path.parent.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2).encode()
    if len(encoded)>32*1024*1024:raise ValueError('采集档案超过32MiB，需分区归档')
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.value-',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


@contextmanager
def _storage(data_dir):
    folder=Path(data_dir)/'fundamentals'
    if folder.is_symlink():raise ValueError('财务目录不得为符号链接')
    folder.mkdir(parents=True,exist_ok=True)
    fd=os.open(folder/'.sync.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX);yield folder
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def _fingerprint(row):
    # Identical provider content retains its earliest real observation time.
    return hashlib.sha256(json.dumps(row.model_dump(mode='json',exclude={'observed_at'}),sort_keys=True).encode()).hexdigest()


def merge_dataset(data_dir,statements,markets):
    with _LOCK,_storage(data_dir) as folder:
        path=folder/'buffett_v1.json'
        old=load_dataset(Path(data_dir)) if path.exists() else FactorDataset(statements=[],market_values=[])
        def merge(existing,incoming):
            unique={_fingerprint(r):r for r in existing}
            for row in incoming:
                key=_fingerprint(row)
                if key not in unique or row.observed_at<unique[key].observed_at:unique[key]=row
            return list(unique.values())
        merged=FactorDataset(statements=merge(old.statements,statements),market_values=merge(old.market_values,markets))
        _atomic(path,merged.model_dump(mode='json'))
        return dict(statement_versions=len(merged.statements),market_versions=len(merged.market_values))


async def _fixed_get(url):
    parts=urlsplit(url)
    allowed=(url.startswith(_INFO_ENDPOINT+'?') and parts.netloc=='push2.eastmoney.com') or (
        parts.netloc=='qt.gtimg.cn' and not parts.query and re.fullmatch(r'/q=(sh|sz)[0-9]{6}',parts.path))
    if not allowed or parts.scheme!='https' or parts.fragment or parts.username or parts.password:
        raise ValueError('仅允许固定腾讯市值和东方财富行业HTTPS接口')
    async def fetch():
        await asyncio.to_thread(validate_public_https_url,url)
        async with httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as client:
            async with client.stream('GET',url,headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'identity'}) as response:
                if 300<=response.status_code<400:raise ValueError('公开数据接口禁止重定向')
                response.raise_for_status()
                if response.headers.get('content-encoding','identity').lower() not in ('identity',''):
                    raise ValueError('公开数据接口不接受压缩响应')
                body=bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body)>1024*1024:raise ValueError('公开数据响应超过1MiB')
        payload=bytes(body)
        archive=_RAW_ARCHIVE.get()
        if archive is not None:
            _atomic(Path(archive)/(uuid.uuid4().hex+'.json'),dict(source=url,fetched_at=datetime.now(SHANGHAI).isoformat(),
                sha256=hashlib.sha256(payload).hexdigest(),body_base64=base64.b64encode(payload).decode('ascii')))
        return payload
    return await asyncio.wait_for(fetch(),12)


async def stock_info(symbol):
    params=dict(fltt='2',invt='2',fields='f57,f58,f84,f85,f127,f116,f117,f189,f43',secid=('1.' if symbol.endswith('.SH') else '0.')+symbol[:6])
    url=_INFO_ENDPOINT+'?'+urlencode(params)
    d=json.loads(await _fixed_get(url)).get('data') or {}
    if d.get('f57')!=symbol[:6] or not isinstance(d.get('f127'),str) or not d['f127'].strip():
        raise ValueError('行业来源身份不一致或行业缺失，不按名称猜行业')
    return dict(industry=d['f127'].strip(),name=d.get('f58',''),source=url,observed_at=datetime.now(SHANGHAI))


async def market_value(symbol,now=None):
    now=now or datetime.now(SHANGHAI);ticker=symbol[-2:].lower()+symbol[:6]
    source='https://qt.gtimg.cn/q='+ticker
    raw=(await _fixed_get(source)).decode('gbk','strict')
    match=re.fullmatch(r'\s*v_'+ticker+r'="([^"\r\n]*)";\s*',raw)
    if not match:raise ValueError('市值报价包装或证券身份不匹配')
    f=match[1].split('~')
    try:
        if f[2]!=symbol[:6]:raise ValueError()
        stamp=datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
        total=Decimal(f[45])*100_000_000;floating=Decimal(f[44])*100_000_000
        if not total.is_finite() or not floating.is_finite() or total<=0 or floating<0 or total<floating*Decimal(".999"):
            raise ValueError()
        if stamp>now or now-stamp>timedelta(days=7):raise ValueError()
    except (ValueError,IndexError,ArithmeticError) as exc:raise ValueError('总市值字段、单位或来源时间无效') from exc
    return MarketValue(symbol=symbol,as_of=stamp,observed_at=datetime.now(SHANGHAI),source=source,market_cap_cny=float(total))


def financial_bundle(symbol,archive_dir):
    project=Path(__file__).resolve().parents[4]/'quant-rsi'
    python=project/'quant/.venv/bin/python'
    if not python.is_file():raise ValueError('quant数据环境不存在，不能启动公开财报采集')
    archive_dir.mkdir(parents=True,exist_ok=True)
    path=archive_dir/(symbol.replace('.','_')+'-'+uuid.uuid4().hex+'.json')
    year=datetime.now(SHANGHAI).year
    command=[str(python),'-m','quant.main','financial','all',symbol[:6],'--start',f'{year-7}-01-01',
        '--end',f'{year-1}-12-31','--limit','100','--json','--out',str(path)]
    try:
        result=subprocess.run(command,cwd=project,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=65)
    except subprocess.TimeoutExpired as exc:raise ValueError('财报采集超过65秒预算，未提交部分股票数据') from exc
    if result.returncode or not path.exists() or path.stat().st_size>8*1024*1024:raise ValueError('财报采集失败或原始档案异常')
    payload=json.loads(path.read_text())
    if payload.get('status')!='success' or payload.get('errors'):raise ValueError('财报来源不完整，保留原始档案但不提交该股票')
    return payload,path


def normalize_statements(symbol,bundle,info):
    tables={key:bundle['data'][key] for key in ('balance_sheet','income_statement','cash_flow')}
    maps={key:{r['report_date']:r for r in table['items'] if r['report_date'].endswith('12-31')} for key,table in tables.items()}
    periods=sorted(set.intersection(*(set(m) for m in maps.values())))
    if len(periods)<6:raise ValueError('来源不足6个连续年度，不能计算五年质量价值')
    industry=info['industry']
    if any(term in industry for term in ('银行','保险','证券','金融','信托')):raise ValueError('金融企业不适用本版质量价值因子')
    output=[]
    for period in periods:
        balance,income,cash=(maps[key][period] for key in tables)
        records=(balance,income,cash)
        if any(r.get('code')!=symbol[:6] or r.get('currency')!='CNY' or r.get('amount_unit')!='元' for r in records):
            raise ValueError('财报代码/币种/金额单位不一致')
        if balance.get('basis')!='period_end' or any(r.get('basis')!='year_to_date' for r in (income,cash)):
            raise ValueError('报表口径不一致，不能将单季或TTM冒充年报')
        if not all(r.get('disclosed_date') for r in records):raise ValueError('财报缺少真实披露日期')
        capex=cash.get('source_fields',{}).get('CONSTRUCT_LONG_ASSET')
        if capex is None:raise ValueError('资本开支科目缺失，不能用净投资现金流替代或填0')
        disclosed=max(date.fromisoformat(r['disclosed_date']) for r in records)
        updates=[date.fromisoformat(r['updated_date']) for r in records if r.get('updated_date')]
        observed=max([datetime.fromisoformat(bundle['provenance'][key]['fetched_at']) for key in tables]+[info['observed_at']])
        row=AnnualStatement(symbol=symbol,report_date=date.fromisoformat(period),disclosed_at=datetime.combine(disclosed,time.max,SHANGHAI),
            revised_at=datetime.combine(max(updates),time.max,SHANGHAI) if updates else None,observed_at=observed,
            source=tables['balance_sheet']['source_url'],company_type='non_financial',industry=industry,
            revenue=income.get('revenue'),net_profit=income.get('net_profit'),operating_cash_flow=cash.get('operating_cash_flow'),capex_cash_paid=capex,
            total_assets=balance.get('total_assets'),total_liabilities=balance.get('total_liabilities'),total_equity=balance.get('total_equity'))
        if any(v is None for v in (row.revenue,row.net_profit,row.operating_cash_flow,row.total_assets,row.total_liabilities,row.total_equity)):
            raise ValueError('必要财报科目缺失')
        if row.total_assets<=0 or abs(row.total_assets-row.total_liabilities-row.total_equity)>max(1.,row.total_assets*.001):
            raise ValueError('资产负债恒等式不符，拒绝单位或合并范围混用')
        output.append(row)
    latest=output[-1].report_date.year
    if not set(range(latest-5,latest+1)) <= {r.report_date.year for r in output}:raise ValueError('年度报告不连续')
    return output


def _job_path(data_dir):return Path(data_dir)/'fundamentals/sync_status.json'


def sync_status(data_dir):
    path=_job_path(data_dir)
    if not path.exists():return dict(status='idle',notes=SOURCE_NOTES)
    if path.is_symlink() or path.stat().st_size>1024*1024:raise ValueError('采集状态档案异常')
    data=json.loads(path.read_text())
    # A restarted process must not leave a permanent phantom running state.
    if data.get('status')=='running' and not _pid_alive(data.get('owner_pid')):
        data.update(status='interrupted',message='服务已重启，已成功提交的数据保留，请重新抓取剩余标的')
    return dict(data,notes=SOURCE_NOTES)


def _collect_inner(data_dir,symbols,job_id=None):
    global _ACTIVE
    symbols=SyncRequest.normalized(SyncRequest(symbols=symbols).symbols)
    state=dict(job_id=job_id or uuid.uuid4().hex,status='running',owner_pid=os.getpid(),total=len(symbols),completed=0,
        started_at=datetime.now(SHANGHAI).isoformat(),results=[],message='采集当前公开版本，不会倒填历史')
    _atomic(_job_path(data_dir),state)
    for symbol in symbols:
        try:
            info=asyncio.run(stock_info(symbol));market=asyncio.run(market_value(symbol))
            bundle,raw_file=financial_bundle(symbol,Path(data_dir)/'fundamentals/raw')
            annual=normalize_statements(symbol,bundle,info)
            counts=merge_dataset(data_dir,annual,[market])
            row=dict(symbol=symbol,name=info['name'],status='collected',annual_periods=len(annual),industry=info['industry'],
                market_as_of=market.as_of.isoformat(),observed_at=max(r.observed_at for r in annual).isoformat(),raw_file=raw_file.name,**counts)
        except Exception as exc:
            row=dict(symbol=symbol,status='failed',error=str(exc)[:350])
        state['results'].append(row);state['completed']+=1
        _atomic(_job_path(data_dir),state)
    good=sum(r['status']=='collected' for r in state['results'])
    state.update(status='completed' if good==len(symbols) else 'partial' if good else 'failed',
                 finished_at=datetime.now(SHANGHAI).isoformat(),message=f'成功 {good}/{len(symbols)}；当前快照不代表历史PIT档案')
    if good:
        evaluated=evaluate_dataset(load_dataset(Path(data_dir)),datetime.now(SHANGHAI))
        state['current_scored_symbols']=sum(r['score'] is not None for r in evaluated['rows'])
    _atomic(_job_path(data_dir),state)
    return dict(state,notes=SOURCE_NOTES)


def _pid_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


def collect(data_dir,symbols,job_id=None):
    folder=Path(data_dir)/'fundamentals'
    if folder.is_symlink():raise ValueError('财务目录不得为符号链接')
    folder.mkdir(parents=True,exist_ok=True)
    fd=os.open(folder/'.collector.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('已有财报采集任务在运行，请等待后重试') from exc
        token=_RAW_ARCHIVE.set(folder/'raw/http')
        try:return _collect_inner(data_dir,symbols,job_id)
        finally:_RAW_ARCHIVE.reset(token)
    finally:
        os.close(fd)


def _run_job(data_dir,symbols,job_id):
    try:return collect(data_dir,symbols,job_id)
    except Exception as exc:
        state=sync_status(data_dir)
        if state.get('job_id')==job_id:
            state.update(status='failed',message=str(exc)[:350],finished_at=datetime.now(SHANGHAI).isoformat())
            _atomic(_job_path(data_dir),state)
        return state


def start_collect(data_dir,symbols):
    global _ACTIVE
    symbols=SyncRequest.normalized(SyncRequest(symbols=symbols).symbols)
    with _LOCK:
        if _ACTIVE is not None and not _ACTIVE.done():return dict(sync_status(data_dir),reused=True)
        existing=sync_status(data_dir)
        if existing.get('status')=='running':return dict(existing,reused=True)
        job_id=uuid.uuid4().hex
        # Persist before returning so the first status poll never sees an old job.
        _atomic(_job_path(data_dir),dict(job_id=job_id,status='running',owner_pid=os.getpid(),total=len(symbols),completed=0,results=[]))
        _ACTIVE=_EXECUTOR.submit(_run_job,Path(data_dir),symbols,job_id)
        return dict(job_id=job_id,status='running',reused=False)
