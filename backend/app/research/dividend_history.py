"""Auditable public historical data collection for the dividend/small-cap study.

BaoStock supplies historical raw prices, isST/tradestatus and IPO/delist dates;
CNInfo supplies dated capital changes; Eastmoney supplies published financials
and dividend implementation events. Current source revisions are archived and
flagged, never described as original PIT filing versions.
"""
from __future__ import annotations
import base64
import bisect
from concurrent.futures import ProcessPoolExecutor, as_completed, ThreadPoolExecutor
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import httpx
from app.services.public_financials import EastmoneyFinancials, SHANGHAI, atomic_json, day, number
from app.services.dividend_screen import MAIN, cash_dividends

BAO_FIELDS='date,code,open,high,low,close,preclose,volume,amount,tradestatus,isST'
_BAO_CONNECTED=False


def archive(path, rows, source, **meta):
    doc={'source':source,'observed_at':datetime.now(SHANGHAI).isoformat(),'rows':rows,**meta}
    atomic_json(path,doc)
    return doc


def read(path):return json.loads(Path(path).read_text())


def symbol(code):
    return code[3:]+'.'+code[:2].upper() if code.startswith(('sh.','sz.')) else ''


def bao_read(rs):
    if rs.error_code!='0':raise ValueError(f'BaoStock {rs.error_code}: {rs.error_msg}')
    rows=[]
    while rs.next():
        rows.append(dict(zip(rs.fields,rs.get_row_data())))
        if len(rows)>30000:raise ValueError('BaoStock 单次查询超过3万行')
    if rs.error_code!='0':raise ValueError(f'BaoStock {rs.error_code}: {rs.error_msg}')
    return rows


def bao_task(args):
    global _BAO_CONNECTED
    kind,key,folder,start,end=args
    path=Path(folder)/kind/(key+'.json')
    if path.exists():return key,len(read(path)['rows'])
    import baostock as bs
    socket.setdefaulttimeout(20)
    last=None
    for attempt in range(3):
        try:
            if not _BAO_CONNECTED:
                result=bs.login()
                if result.error_code!='0':raise ValueError(result.error_msg)
                _BAO_CONNECTED=True
            if kind=='monthly':rs=bs.query_daily_history_k_AStock(date=key)
            elif kind=='states':rs=bs.query_history_k_data_plus(key[-2:].lower()+'.'+key[:6],'date,code,close,tradestatus,isST',start_date=start,end_date=end,adjustflag='3')
            elif kind=='monthprices':rs=bs.query_history_k_data_plus(key[-2:].lower()+'.'+key[:6],'date,code,close',start_date=start,end_date=end,frequency='m',adjustflag='3')
            elif kind=='prices':rs=bs.query_history_k_data_plus(key[-2:].lower()+'.'+key[:6],BAO_FIELDS,start_date=start,end_date=end,adjustflag='3')
            elif kind=='reported_capital':
                code,year,quarter=key.split('_');rs=bs.query_profit_data(code=code[-2:].lower()+'.'+code[:6],year=int(year),quarter=int(quarter))
            elif kind=='actions':
                code,year=key.split('_');rs=bs.query_dividend_data(code=code[-2:].lower()+'.'+code[:6],year=year,yearType='operate')
            elif kind=='basic':rs=bs.query_stock_basic()
            elif kind=='calendar':rs=bs.query_trade_dates(start_date='2000-01-01',end_date=end)
            elif kind=='benchmark':rs=bs.query_history_k_data_plus(key[-2:].lower()+'.'+key[:6],'date,code,open,high,low,close,preclose,volume,amount',start_date=start,end_date=end,adjustflag='3')
            else:raise ValueError(kind)
            rows=bao_read(rs)
            if kind=='monthly' and len(rows)<3000:raise ValueError('历史全市场日线覆盖不足3000，拒绝不完整截面')
            if not rows and kind not in ('actions','reported_capital'):raise ValueError(f'BaoStock {kind}/{key} 没有记录')
            archive(path,rows,'baostock-0.9.4',request={'kind':kind,'key':key,'start':start,'end':end},adjustflag='3' if kind in ('monthly','prices','benchmark') else None)
            return key,len(rows)
        except Exception as exc:
            last=exc;_BAO_CONNECTED=False
            try:bs.logout()
            except Exception:pass
            time.sleep(1+attempt)
    raise ValueError(f'{kind}/{key}: {last}')


def cninfo_capital(code, folder, end):
    path=Path(folder)/'capital'/(code+'.json')
    if path.exists():return code,len(read(path)['rows'])
    # AKShare cninfo.js getResCode1: public timestamp AES-CBC protocol. Rewritten
    # with OpenSSL instead of evaluating downloaded JavaScript.
    key=b'1234567887654321'
    for attempt in range(3):
        try:
            token=base64.b64encode(subprocess.run(['openssl','enc','-aes-128-cbc','-K',key.hex(),'-iv',key.hex()],input=str(int(time.time())).encode(),capture_output=True,check=True).stdout).decode()
            headers={'User-Agent':'Mozilla/5.0','Accept-Enckey':token,'Referer':'https://webapi.cninfo.com.cn/','Origin':'https://webapi.cninfo.com.cn','Accept-Encoding':'identity'}
            with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
                with client.stream('POST','https://webapi.cninfo.com.cn/api/stock/p_stock2215',params={'scode':code[:6],'sdate':'1990-01-01','edate':end},headers=headers) as response:
                    response.raise_for_status();payload=bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload)>4*1024*1024:raise ValueError('股本响应超过4MiB')
            data=json.loads(payload);rows=data.get('records')
            if not isinstance(rows,list) or not rows or len(rows)!=int(data.get('count',-1)):
                raise ValueError('股本记录为空或分页不完整')
            if any(r.get('SECCODE')!=code[:6] for r in rows):raise ValueError('股本来源返回其他证券')
            archive(path,rows,'cninfo/p_stock2215',amount_unit='10000 shares',request={'symbol':code,'end':end})
            return code,len(rows)
        except Exception:
            if attempt==2:raise
            time.sleep(1+attempt)
    raise RuntimeError('unreachable')


def collect_http(folder,start,end,progress):
    root=Path(folder);source=EastmoneyFinancials()
    try:
        for year in range(start.year-1,end.year+1):
            path=root/'income'/(str(year)+'.json')
            if not path.exists():
                where=f"(REPORTDATE>='{year}-01-01')(REPORTDATE<='{year}-12-31')"
                rows=source.records('RPT_LICO_FN_CPD',where=where,sort='REPORTDATE,SECURITY_CODE')
                if not rows:raise ValueError(f'{year} 财报缺失')
                archive(path,rows,'eastmoney/RPT_LICO_FN_CPD',point_in_time=False)
            progress('income',year,len(read(path)['rows']))
        path=root/'dividends.json'
        if not path.exists():
            lower=start.replace(year=start.year-1)-timedelta(days=2)
            rows=source.records('RPT_SHAREBONUS_DET',where=f"(EX_DIVIDEND_DATE>='{lower}')(EX_DIVIDEND_DATE<='{end}')",sort='EX_DIVIDEND_DATE,SECURITY_CODE')
            archive(path,rows,'eastmoney/RPT_SHAREBONUS_DET')
        progress('dividends','all',len(read(path)['rows']))
    finally:source.close()


def dividend_events(folder):
    root=Path(folder);base=read(root/'dividends.json')['rows']
    supplement=root/'delisted_dividends.json'
    known={(r['SECUCODE'],day(r.get('EX_DIVIDEND_DATE'))) for r in base}
    return base+([r for r in read(supplement)['rows'] if (r['SECUCODE'],day(r.get('EX_DIVIDEND_DATE'))) not in known] if supplement.exists() else [])


def monthly_dates(calendar,start,end):
    # A month is traded on its first trading day; the signal uses the prior
    # trading day's close. Preserve the preceding December signal for January.
    days=sorted(day(r['calendar_date']) for r in calendar if r['is_trading_day']=='1')
    result=[]
    for i,d in enumerate(days):
        if i and start<=d<=end and d.month!=days[i-1].month:
            result.append({'trade_date':str(d),'signal_date':str(days[i-1])})
    return days,result


def capital_at(rows,signal):
    known=[]
    for r in rows:
        effective=day(r.get('VARYDATE'));announced=day(r.get('DECLAREDATE'));shares=number(r.get('F003N'))
        if not effective or not announced or not shares or shares<=0:continue
        available=max(effective,announced)
        if available<=signal:known.append((available,effective,shares*10000))
    if not known:return None
    # A late quarterly filing must not overwrite a newer capital change with
    # its older period-end share count. Select effective date first, then the
    # latest available revision for that same effective date.
    latest_effective=max(x[1] for x in known)
    candidates=[x for x in known if x[1]==latest_effective]
    chosen=max(candidates,key=lambda r:r[0])
    if len({x[2] for x in candidates if x[0]==chosen[0]})>1:raise ValueError('同日股本存在冲突版本')
    return {'total_shares':chosen[2],'capital_available_date':str(chosen[0]),'capital_effective_date':str(chosen[1])}


def prepare_candidates(folder,start,end,progress):
    root=Path(folder)
    basic={symbol(r['code']):r for r in read(root/'basic/all.json')['rows'] if r.get('type')=='1' and MAIN.fullmatch(symbol(r['code']))}
    calendar,months=monthly_dates(read(root/'calendar/all.json')['rows'],start,end)
    events=dividend_events(root)
    financials={}
    for path in sorted((root/'income').glob('*.json')):
        for r in read(path)['rows']:
            code=r.get('SECUCODE','');period=day(r.get('REPORTDATE'));pub=day(r.get('NOTICE_DATE'))
            if MAIN.fullmatch(code) and period and pub:
                financials.setdefault(code,[]).append((period,pub,day(r.get('UPDATE_DATE')),number(r.get('PARENT_NETPROFIT'))))
    output=[];needed=set()
    for month in months:
        signal=day(month['signal_date']);divs=cash_dividends(events,signal);rows=[];counts={'raw_main':0,'price_dividend':0,'missing_financial':0,'revised_after_signal':0}
        for r in read(root/'monthly'/(str(signal)+'.json'))['rows']:
            code=symbol(r['code']);inst=basic.get(code);price=number(r['close'])
            if not inst:continue
            counts['raw_main']+=1
            ipo=day(inst.get('ipoDate'));out=day(inst.get('outDate'))
            if not ipo or (out and out<=signal) or bisect.bisect_right(calendar,signal)-bisect.bisect_left(calendar,ipo)<250:continue
            if r.get('isST')=='1' or r.get('tradestatus')=='0' or not price or not 2<=price<=9:continue
            yield_=divs.get(code,0)/price
            if yield_<.03:continue
            counts['price_dividend']+=1
            known=[f for f in financials.get(code,[]) if f[0]<=signal and f[1]<=signal]
            if not known and r.get('delisted_supplement'):
                rows.append({'symbol':code,'raw_close':price,'dividend_yield':yield_,'net_profit':None,'report_date':None,'announce_date':None,'source_update_date':None,'revision_unverified':True,'requires_profit_check':True})
                needed.add(code);continue
            if not known:counts['missing_financial']+=1;continue
            f=max(known,key=lambda f:(f[0],f[1]))
            if f[3] is None:counts['missing_financial']+=1;continue
            if f[3]<=0:continue
            revised=f[2] is not None and f[2]>signal
            counts['revised_after_signal']+=int(revised)
            rows.append({'symbol':code,'raw_close':price,'dividend_yield':yield_,'net_profit':f[3],
                'report_date':str(f[0]),'announce_date':str(f[1]),'source_update_date':str(f[2]) if f[2] else None,'revision_unverified':revised})
            needed.add(code)
        output.append({**month,'candidates':rows,'coverage':counts})
        progress('candidates',str(signal),len(rows))
    archive(root/'candidates.json',output,'historical-public-reconstruction',point_in_time=False)
    return needed


def rank_months(folder,progress):
    root=Path(folder);months=read(root/'candidates.json')['rows'];needed=set()
    for month in months:
        ranked=[];missing=[]
        for row in month['candidates']:
            path=root/'capital'/(row['symbol']+'.json')
            capital=capital_at(read(path)['rows'],day(month['signal_date'])) if path.exists() else None
            if capital is None:missing.append(row['symbol']);continue
            ranked.append({**row,**capital,'market_cap':row['raw_close']*capital['total_shares']})
        ranked.sort(key=lambda r:(r['market_cap'],r['symbol']))
        month['ranked']=ranked;month['missing_capital']=missing
        month['selected']=ranked[:10]
        needed.update(r['symbol'] for r in ranked[:20])
        progress('ranked',month['signal_date'],len(ranked))
    archive(root/'targets.json',months,'monthly-smallest-total-capitalization',point_in_time=False)
    return needed


def parallel_bao(tasks,progress):
    with ProcessPoolExecutor(max_workers=4) as pool:
        pending={pool.submit(bao_task,t):t for t in tasks}
        for future in as_completed(pending):
            key,count=future.result();progress(pending[future][0],key,count)


def verify_status(folder,start,end,progress):
    """Lazy exact ranking: inspect daily state in ascending capital order.

    Lower-ranked stocks cannot affect the first twenty eligible names. Fetch
    more rows whenever suspension/ST exclusions leave fewer than twenty.
    """
    root=Path(folder);months=read(root/'targets.json')['rows']
    inspected={m['signal_date']:min(40,len(m['ranked'])) for m in months}
    for attempt in range(100):
        needed={r['symbol'] for m in months for r in m['ranked'][:inspected[m['signal_date']]]}
        tasks=[('states',code,str(root),str(start-timedelta(days=40)),str(end)) for code in sorted(needed) if not (root/'prices'/(code+'.json')).exists() and not (root/'states'/(code+'.json')).exists()]
        parallel_bao(tasks,progress)
        price_index={s:{r['date']:r for r in read((root/'prices'/(s+'.json')) if (root/'prices'/(s+'.json')).exists() else root/'states'/(s+'.json'))['rows']} for s in needed}
        expand=[]
        for month in months:
            eligible=[];excluded_st=excluded_suspended=0
            for row in month['ranked'][:inspected[month['signal_date']]]:
                bar=price_index[row['symbol']].get(month['signal_date'])
                if not bar:raise ValueError(f"{row['symbol']}/{month['signal_date']} 缺少逐日状态，不能推断为正常交易")
                if bar.get('isST') not in ('0','1') or bar.get('tradestatus') not in ('0','1'):raise ValueError('历史ST/交易状态字段缺失')
                if bar['isST']=='1':excluded_st+=1;continue
                if bar['tradestatus']=='0':excluded_suspended+=1;continue
                actual=number(bar['close'])
                if actual is None or abs(actual-row['raw_close'])>.011:raise ValueError(f"{row['symbol']}/{month['signal_date']} 月末与逐日不复权价格不一致")
                if row.get('requires_profit_check'):raise ValueError(f"{row['symbol']}/{month['signal_date']} 退市候选进入有效排名，需要补充归母净利润后才能回测")
                eligible.append({**row,'historical_daily_status_verified':True})
                if len(eligible)>=20:break
            month['_verified']=eligible
            month['coverage'].update(excluded_st_in_capital_order=excluded_st,excluded_suspended_in_capital_order=excluded_suspended)
            if len(eligible)<20 and inspected[month['signal_date']]<len(month['ranked']):expand.append(month)
        if not expand:break
        for month in expand:inspected[month['signal_date']]=min(len(month['ranked']),inspected[month['signal_date']]+40)
    else:raise ValueError('逐日状态核验超过迭代边界')
    selected=set()
    for month in months:
        month['preliminary_ranked']=month['ranked'];month['ranked']=month.pop('_verified');month['selected']=month['ranked'][:10]
        selected.update(r['symbol'] for r in month['ranked'])
        progress('verified_month',month['signal_date'],len(month['ranked']))
    archive(root/'targets.json',months,'monthly-capital-ranking/daily-status-verified',point_in_time=False)
    return selected


def freeze_spec(folder,start,end):
    path=Path(folder)/'collection_spec.json'
    expected={'start':str(start),'end':str(end)}
    if path.exists() and read(path)!=expected:
        raise ValueError('此研究目录已冻结为另一日期范围，请用 --folder 指定新目录，不能复用不覆盖区间的缓存')
    if not path.exists():atomic_json(path,expected)


def collect(folder,start,end,progress=print):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    freeze_spec(folder,start,end)
    def record(stage,key,count):
        progress(stage,key,count)
        atomic_json(folder/'progress.json',{'stage':stage,'last':str(key),'count':count,'at':datetime.now(SHANGHAI).isoformat()})
    # HTTP collection and bounded two-process market snapshots use independent
    # providers. Individual BaoStock connections are process-local.
    parallel_bao([(k,'all',str(folder),str(start-timedelta(days=370)),str(end)) for k in ('calendar','basic')],record)
    calendar,months=monthly_dates(read(folder/'calendar/all.json')['rows'],start,end)
    # Stock histories are much faster than the provider's full-market-by-date
    # endpoint. Collect every historically dividend-paying eligible stock, then
    # derive each month-end cross-section without using today's survivor pool.
    collect_http(folder,start,end,record)
    events=read(folder/'dividends.json')['rows']
    ever_dividends={r.get('SECUCODE') for r in events if (number(r.get('PRETAX_BONUS_RMB')) or 0)>0 and MAIN.fullmatch(r.get('SECUCODE',''))}
    basic={symbol(r['code']):r for r in read(folder/'basic/all.json')['rows'] if r.get('type')=='1'}
    pool=[]
    for code in sorted(ever_dividends):
        inst=basic.get(code)
        if inst and (not day(inst.get('outDate')) or day(inst['outDate'])>=start) and day(inst.get('ipoDate')) and day(inst['ipoDate'])<=end:
            pool.append(code)
    if len(pool)<2000:raise ValueError('历史分红股票池覆盖异常，不能用当前少量候选替代全市场')
    record('historical_dividend_universe','all',len(pool))
    parallel_bao([('monthprices',code,str(folder),str(start-timedelta(days=40)),str(end)) for code in pool],record)
    cross_sections={m['signal_date']:[] for m in months}
    month_to_signal={k[:7]:k for k in cross_sections}
    for code in pool:
        for row in read(folder/'monthprices'/(code+'.json'))['rows']:
            signal=month_to_signal.get(row['date'][:7])
            if signal:cross_sections[signal].append({**row,'monthly_source_date':row['date'],'date':signal,'requires_daily_status_check':True})
    for signal,rows in cross_sections.items():
        if len(rows)<1800:raise ValueError(f'{signal} 历史分红股票月线覆盖不足')
        archive(folder/'monthly'/(signal+'.json'),rows,'baostock-monthly-close/preliminary-only',universe_symbols=len(pool))
    return finalize(folder,start,end,progress)


def finalize(folder,start,end,progress=print):
    folder=Path(folder)
    freeze_spec(folder,start,end)
    def record(stage,key,count):
        progress(stage,key,count)
        atomic_json(folder/'progress.json',{'stage':stage,'last':str(key),'count':count,'at':datetime.now(SHANGHAI).isoformat()})
    from app.research.delisted_supplement import supplement
    supplement(folder,start,end,record)
    symbols=prepare_candidates(folder,start,end,record)
    basic_map={symbol(r['code']):r for r in read(folder/'basic/all.json')['rows']}
    retired_missing=[code for code in symbols if not (folder/'capital'/(code+'.json')).exists() and day(basic_map.get(code,{}).get('outDate'))]
    if retired_missing:
        from app.research.delisted_supplement import supplement_capital
        supplement_capital(folder,retired_missing,start,end,record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(cninfo_capital,s,str(folder),str(end)) for s in sorted(symbols)]
        for i,future in enumerate(as_completed(futures)):
            key,n=future.result();record('capital',key,n)
    rank_months(folder,record)
    symbols=verify_status(folder,start,end,record)
    tasks=[('prices',s,str(folder),str(start-timedelta(days=14)),str(end)) for s in sorted(symbols)]
    tasks += [('benchmark',s,str(folder),str(start-timedelta(days=14)),str(end)) for s in ['000300.SH','000852.SH','000001.SH']]
    parallel_bao(tasks,record)
    action_tasks=[('actions',s+'_'+str(y),str(folder),str(start),str(end)) for s in sorted(symbols) for y in range(start.year,end.year+1)]
    parallel_bao(action_tasks,record)
    records=[]
    for path in sorted(folder.rglob('*.json')):
        if path.name in ('manifest.json','progress.json') or path.relative_to(folder).parts[0]=='report':continue
        records.append({'file':str(path.relative_to(folder)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest={'complete':True,'schema_revision':2,'delisted_supplement_checked':True,'start':str(start),'end':str(end),'point_in_time':False,'files':records,
              'limitations':['公开财报当前修订版按披露日期重建，未核验原始公告版本。','股本仅使用公告日与变动日均不晚于信号日的记录；缺公告日不回填。']}
    atomic_json(folder/'manifest.json',manifest);record('complete','all',len(records))
    return manifest
