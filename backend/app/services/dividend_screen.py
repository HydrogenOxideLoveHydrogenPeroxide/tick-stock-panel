"""Current-close research screen, independent of historical enriched K-line data.

Current public revisions cannot reconstruct an old point-in-time universe. Each
snapshot keeps its real observation timestamp; historical backtests are blocked.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import time
import httpx
import polars as pl
from app.services.public_financials import EastmoneyFinancials, SHANGHAI, atomic_json, atomic_parquet, day, number

RELATIVE = Path('fundamentals/dividend_screen')
MAIN = re.compile(r'^(?:600|601|603|605)\d{3}\.SH$|^(?:000|001|002|003)\d{3}\.SZ$')


def cash_dividends(events, as_of):
    """Cash per TEN shares; exclude plans and future ex-dates, adjust bonus shares.

    Cash from each event is restated by subsequent bonus/transfer ratios. Rights
    issues and buybacks are not a historical total-return model.
    """
    try:cutoff=as_of.replace(year=as_of.year-1)
    except ValueError:cutoff=as_of.replace(year=as_of.year-1,day=28)
    groups=defaultdict(list);seen=set()
    for event in events:
        symbol=event.get('SECUCODE','');ex=day(event.get('EX_DIVIDEND_DATE'))
        cash=number(event.get('PRETAX_BONUS_RMB'));bonus=number(event.get('BONUS_IT_RATIO')) or 0.
        if not MAIN.fullmatch(symbol) or not ex or not cutoff<ex<=as_of:continue
        if event.get('ASSIGN_PROGRESS')!='实施分配':continue
        if cash is None:
            # Some explicit pure-share transfer plans encode cash as null.
            plan=re.fullmatch(r'10(?:送|转)([0-9.]+)股',str(event.get('IMPL_PLAN_PROFILE','')))
            if plan and bonus>0 and abs(float(plan[1])-bonus)<1e-5:
                cash=0.
        if cash is None or cash<0 or bonus<0:raise ValueError('分红金额或送转比例缺失/无效，不能按零处理')
        key=(symbol,ex,day(event.get('REPORT_DATE')))
        if key in seen:raise ValueError('分红记录存在重复版本，需核验后再采集')
        seen.add(key);groups[symbol].append((ex,cash/10,1+bonus/10))
    result={}
    for symbol,rows in groups.items():
        divisor=1.;cash_sum=0.
        for ex,cash,factor in sorted(rows,reverse=True):
            divisor*=factor;cash_sum+=cash/divisor
        result[symbol]=cash_sum
    return result


def parse_quotes(text, requested, as_of):
    rows={}
    for ticker,body in re.findall(r'v_((?:sh|sz)\d{6})="([^"\r\n]*)";',text):
        symbol=ticker[2:]+'.'+ticker[:2].upper()
        if symbol not in requested:raise ValueError('报价返回未请求的证券')
        fields=body.split('~')
        try:
            if fields[2]!=symbol[:6]:raise ValueError()
            stamp=datetime.strptime(fields[30],'%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
            price=number(fields[3]);volume=number(fields[6]);cap=number(fields[45])
            if stamp.date()!=as_of or stamp>datetime.now(SHANGHAI) or not price or price<=0 or not cap or cap<=0:continue
            if volume is None:continue
            rows[symbol]={'symbol':symbol,'name':fields[1],'raw_close':price,'close':price,
                          'volume':volume,'market_cap':cap*1e8,'total_shares':cap*1e8/price,
                          'quote_at':stamp.isoformat(),'is_suspended':volume<=0}
        except (ValueError,IndexError):continue
    return rows


def sync_snapshot(data_dir):
    root=Path(data_dir);now=datetime.now(SHANGHAI);as_of=now.date()
    if now.hour<15 or (now.hour==15 and now.minute<10):
        raise ValueError('当前收盘筛选需交易日15:10后采集，盘中数据不能冒充收盘价')
    instruments=pl.read_parquet(root/'instruments/instruments.parquet')
    if instruments['as_of'].min()!=as_of:raise ValueError('证券名单不是今日快照，请先同步标的信息')
    income_path=root/'financials/income/part.parquet'
    if not income_path.exists():raise ValueError('请先同步公开利润表，再同步分红筛选快照')
    income=pl.read_parquet(income_path).sort('period_end').unique('symbol',keep='last')
    profits={r['symbol']:r for r in income.to_dicts() if (number(r.get('net_income_attributable')) or 0)>0}
    listed={r['symbol']:r for r in instruments.to_dicts() if MAIN.fullmatch(r['symbol']) and r['symbol'] in profits}
    calendar_paths=list((root/'kline_index_enriched').rglob('*.parquet'))
    if not calendar_paths:raise ValueError('缺少交易日历，不能把自然日当上市交易日')
    days=sorted(pl.scan_parquet([str(p) for p in calendar_paths]).filter(pl.col('symbol')=='000001.SH').select('date').unique().collect()['date'].to_list())
    days=[d for d in days if d<=as_of]
    if len(days)<250 or days[-1]!=as_of:raise ValueError('交易日历不足250天或缺少今日，拒绝估算上市交易日')
    source=EastmoneyFinancials()
    try:
        start=as_of-timedelta(days=367)
        events=source.records('RPT_SHAREBONUS_DET',where=f"(EX_DIVIDEND_DATE>='{start}')(EX_DIVIDEND_DATE<='{as_of}')",sort='EX_DIVIDEND_DATE,SECURITY_CODE')
    finally:source.close()
    atomic_json(root/RELATIVE/(str(as_of)+".dividend-events.json"), {"observed_at":datetime.now(SHANGHAI).isoformat(),"events":events})
    dividends=cash_dividends(events,as_of)
    # A dividend-positive screen can exclude zero/no-distribution stocks before
    # requesting quotes, keeping this public source load bounded.
    symbols=sorted(set(listed)&{s for s,v in dividends.items() if v>0});quotes={};failed=[]
    with httpx.Client(timeout=15,trust_env=False,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'identity'}) as client:
        for i in range(0,len(symbols),60):
            batch=symbols[i:i+60];tickers=','.join(s[-2:].lower()+s[:6] for s in batch)
            try:
                with client.stream('GET','https://qt.gtimg.cn/q='+tickers) as response:
                    response.raise_for_status();payload=bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload)>1024*1024:raise ValueError('报价响应过大')
                quotes.update(parse_quotes(payload.decode('gb18030'),set(batch),as_of))
            except (httpx.HTTPError,ValueError) as exc:
                failed.append(type(exc).__name__)
            time.sleep(.15)
    if failed or len(quotes)!=len(symbols):
        raise ValueError(f'当前报价覆盖不完整（{len(quotes)}/{len(symbols)}），保留旧快照；不能把缺报价当零收益')
    now=datetime.now(SHANGHAI)
    if now.date()!=as_of:raise ValueError("采集跨越自然日，请重新同步当日快照")
    rows=[]
    for symbol in symbols:
        quote=quotes[symbol];inst=listed[symbol];profit=profits[symbol]
        listing=day(inst.get('listing_date'))
        if not listing:continue
        name=quote['name']
        rows.append({**quote,'date':as_of,'dividend_per_share_12m':dividends[symbol],
            'net_profit':profit['net_income_attributable'],'is_st':'ST' in name.upper(),
            'is_delisting':'退' in name,'listed_trading_days':sum(d>=listing for d in days),
            'fundamentals_available_date':as_of,'observed_at':now.isoformat(),
            'financial_report_date':profit['period_end'],'financial_announce_date':profit['announce_date'],
            'data_basis':'current_public_revision'})
    if not rows:raise ValueError('未取得当前筛选数据，保留旧快照')
    frame=pl.DataFrame(rows,infer_schema_length=None)
    folder=root/RELATIVE
    meta={'as_of':str(as_of),'observed_at':now.isoformat(),'rows':len(rows),'universe':'今日沪深主板盈利且过去一年已实施现金分红股票',
          'earnings_symbols':len(listed),'dividend_events':len(events),'quote_symbols':len(quotes),
          'point_in_time':False,'historical_backtest_supported':False,
          'sources':['Eastmoney/AKShare-adapted','Tencent current quotes','TickFlow instruments/index trading dates'],
          'limitations':['当前修订财报与今日证券名单；不得用于历史回测。','仅使用除权除息日已到的实施方案；每10股派现金转换为每股并调整送转。',
                         '总市值来自腾讯字段45（亿元），股本由同一时点市值/价格推导，非历史股本。',
                         '上市交易日数在本地日历窗口内截断；旧股的数值为下界，但250日门槛可验证。',
                         '当前ST/退市整理按当日证券名称识别；无成交股票保守排除，非历史证券状态库。',
                         '未模拟月度组合交易、分红再投资、配股或回购对总收益的影响。']}
    # Single date-specific parquet is the commit point. Readers never combine a
    # new manifest with an old differently dated latest.parquet.
    atomic_parquet(folder/f'{as_of}.parquet',frame)
    atomic_json(folder/f'{as_of}.source.json',meta)
    atomic_json(folder/'latest.json',meta)
    return meta


def status(data_dir):
    path=Path(data_dir)/RELATIVE/'latest.json'
    return json.loads(path.read_text()) if path.exists() else None


def load_history(start,end,pool=None,*,data_dir=None):
    if data_dir is None:
        from app.config import settings
        data_dir=settings.data_dir
    meta=status(data_dir)
    if not meta:raise ValueError('请先在财务分析页同步公开财报和分红筛选快照')
    as_of=date.fromisoformat(meta['as_of'])
    if end!=as_of or datetime.now(SHANGHAI).date()!=as_of:
        raise ValueError(f'分红筛选仅支持采集当日 {as_of} 的当前快照；不允许历史回测或使用过期快照，请重新同步')
    frame=pl.read_parquet(Path(data_dir)/RELATIVE/f'{as_of}.parquet')
    return frame.filter(pl.col('symbol').is_in(pool)) if pool else frame
