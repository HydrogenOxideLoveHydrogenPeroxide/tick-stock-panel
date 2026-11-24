"""Recover cash distributions omitted by the listed-company Eastmoney report."""
from datetime import date,timedelta
from collections import defaultdict
from pathlib import Path
import re
from app.research.dividend_history import read,symbol,parallel_bao,archive
from app.services.dividend_screen import MAIN
from app.services.public_financials import day,number


def supplement(folder,start,end,progress=print):
    root=Path(folder)
    basic=read(root/'basic/all.json')['rows']
    dead=[r for r in basic if r.get('type')=='1' and MAIN.fullmatch(symbol(r['code']))
          and day(r.get('outDate')) and day(r['outDate'])>=start and day(r.get('ipoDate')) and day(r['ipoDate'])<=end]
    tasks=[('actions',symbol(r['code'])+'_'+str(y),str(root),str(start),str(end)) for r in dead for y in range(start.year-1,min(end.year,day(r['outDate']).year)+1)]
    parallel_bao(tasks,progress)
    events=[];unresolved=[]
    for inst in dead:
        code=symbol(inst['code']);grouped=defaultdict(list)
        for y in range(start.year-1,min(end.year,day(inst['outDate']).year)+1):
            for row in read(root/'actions'/(code+'_'+str(y)+'.json'))['rows']:
                ex=day(row.get('dividOperateDate'))
                if ex and start-timedelta(days=367)<=ex<=end:grouped[ex].append(row)
        for ex,rows in grouped.items():
            def merged(field):
                values={number(r.get(field)) for r in rows};values.discard(None)
                if len(values)>1:raise ValueError(f'{code}/{ex} 退市分红字段冲突')
                return next(iter(values)) if values else None
            cash=merged('dividCashPsBeforeTax');bonus=(merged('dividStocksPs') or 0)+(merged('dividReserveToStockPs') or 0)
            if cash is None:
                parsed=[]
                for r in rows:
                    match=re.search(r'^10.*?派([0-9.]+)元',r.get('dividCashStock',''))
                    if match:parsed.append(float(match[1])/10)
                if parsed and max(parsed)-min(parsed)<1e-6:cash=parsed[0]
                elif bonus>0 and all('派' not in r.get('dividCashStock','') for r in rows):cash=0.
                else:unresolved.append({'symbol':code,'ex_date':str(ex)});continue
            row=next((r for r in rows if number(r.get('dividCashPsBeforeTax')) is not None),rows[0])
            events.append({'SECUCODE':code,'SECURITY_CODE':code[:6],'EX_DIVIDEND_DATE':str(ex),
                'EQUITY_RECORD_DATE':row.get('dividRegistDate'),'PRETAX_BONUS_RMB':cash*10,'BONUS_IT_RATIO':bonus*10,
                'REPORT_DATE':None,'ASSIGN_PROGRESS':'实施分配','PLAN_NOTICE_DATE':row.get('dividPlanDate'),
                'IMPL_PLAN_PROFILE':row.get('dividCashStock'),'source':'baostock/delisted-dividend'})
    archive(root/'delisted_dividends.json',events,'baostock-delisted-supplement',delisted_stocks_checked=len(dead),unresolved=unresolved)
    if unresolved:raise ValueError('退市股票分红金额无法核验：'+str(unresolved[:8]))
    positive={r['SECUCODE'] for r in events if r['PRETAX_BONUS_RMB']>0}
    progress('delisted_dividend_symbols','all',len(positive))
    tasks=[(kind,code,str(root),str(start-timedelta(days=40)),str(end)) for code in sorted(positive) for kind in ('monthprices','states')]
    parallel_bao(tasks,progress)
    # Enrich preliminary month-end files; only observable historical daily
    # status is used. Today's delisted flag never removes an earlier stock.
    from app.research.dividend_history import monthly_dates
    _,months=monthly_dates(read(root/'calendar/all.json')['rows'],start,end)
    indices={s:{r['date']:r for r in read(root/'states'/(s+'.json'))['rows']} for s in positive}
    for m in months:
        path=root/'monthly'/(m['signal_date']+'.json');document=read(path)
        existing={symbol(r['code']) for r in document['rows']}
        for code in sorted(positive):
            if code in existing:continue
            bar=indices[code].get(m['signal_date'])
            if bar:document['rows'].append({**bar,'delisted_supplement':True})
        archive(path,document['rows'],'baostock-monthly-close/including-delisted')
    return {'checked':len(dead),'positive_cash_symbols':len(positive),'events':len(events)}



def supplement_capital(folder,codes,start,end,progress=print):
    root=Path(folder)
    basic={symbol(r['code']):r for r in read(root/'basic/all.json')['rows']}
    tasks=[('reported_capital',code+'_'+str(year)+'_'+str(q),str(root),str(start),str(end)) for code in codes
           for year in range(start.year-1,min(end.year,day(basic[code]['outDate']).year)+1) for q in range(1,5)]
    parallel_bao(tasks,progress)
    for code in codes:
        normalized=[]
        for year in range(start.year-1,min(end.year,day(basic[code]['outDate']).year)+1):
            for q in range(1,5):
                for row in read(root/'reported_capital'/(code+'_'+str(year)+'_'+str(q)+'.json'))['rows']:
                    if symbol(row.get('code',''))!=code:raise ValueError('季报股本返回其他证券')
                    shares=number(row.get('totalShare'));pub=day(row.get('pubDate'));period=day(row.get('statDate'))
                    if shares and shares>0 and pub and period:
                        normalized.append({'VARYDATE':str(period),'DECLAREDATE':str(pub),'F003N':shares/10000,
                                           'source':'baostock totalShare, original unit=shares','source_report':row})
        if not normalized:raise ValueError(f'{code} 缺少退市历史股本，不能忽略此候选')
        archive(root/'capital'/(code+'.json'),normalized,'baostock/published-quarterly-totalShare',amount_unit='10000 shares',
                limitations=['仅使用当时已披露的期末股本，未重建期内所有股本变动；此来源差异在报告披露。'])
        progress('retired_capital',code,len(normalized))
