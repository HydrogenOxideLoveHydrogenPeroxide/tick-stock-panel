"""Monthly equal-weight A-share simulation with explicit public-data limits."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import statistics
from functools import lru_cache
from app.research.dividend_history import read, monthly_dates, dividend_events
from app.services.public_financials import day, number, atomic_json

@dataclass
class Config:
    initial_cash:float=100000.
    top_n:int=10
    commission:float=.0003
    minimum_commission:float=5.
    slippage_bps:float=5.
    dividend_tax:float=.20  # conservative flat provision, not lot-specific tax
    participation:float=.05
    max_exposure:float=1.0


def rounded(value):return float(Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


def fees(value,d,side,config):
    commission=max(config.minimum_commission,value*config.commission) if value else 0.
    stamp=value*(.001 if d<date(2023,8,28) else .0005) if side=='sell' else 0.
    transfer=value*(.00002 if d<date(2022,4,29) else .00001)
    return {'commission':commission,'stamp':stamp,'transfer':transfer,'total':commission+stamp+transfer}


def executable(bar,side):
    if bar is None or bar.get('tradestatus')!='1' or (number(bar.get('volume')) or 0)<=0:return False,'停牌/无成交'
    price=number(bar.get('open'));previous=number(bar.get('preclose'))
    if not price or not previous or previous<=0:return False,'缺少开盘价/涨跌停参考价'
    band=.05 if bar.get('isST')=='1' else .10
    if side=='buy' and price>=rounded(previous*(1+band))-.00001:return False,'开盘涨停，保守不买入'
    if side=='sell' and price<=rounded(previous*(1-band))+.00001:return False,'开盘跌停，延后卖出'
    return True,''


def stats(curve,initial_cash):
    values=[initial_cash]+[r['equity'] for r in curve]
    returns=[b/a-1 for a,b in zip(values,values[1:])]
    peak=initial_cash;peak_date=None;drawdown=0.;drawdown_period={}
    for row in curve:
        value=row['equity']
        if value>peak:peak=value;peak_date=row['date']
        current=value/peak-1
        if current<drawdown:
            drawdown=current;drawdown_period={'peak_date':peak_date,'trough_date':row['date'],'peak_equity':peak,'recovery_date':None}
    if drawdown_period:
        for row in curve:
            if row['date']>drawdown_period['trough_date'] and row['equity']>=drawdown_period['peak_equity']:
                drawdown_period['recovery_date']=row['date'];break
    years=max(1,(day(curve[-1]['date'])-day(curve[0]['date'])).days+1)/365.25
    annual=(values[-1]/initial_cash)**(1/years)-1 if values[-1]>0 else -1.
    vol=statistics.stdev(returns)*math.sqrt(252) if len(returns)>1 else 0.
    sharpe=statistics.mean(returns)*252/vol if vol else None
    by_year={};previous=initial_cash
    for row in curve:
        y=row['date'][:4]
        if y not in by_year:by_year[y]={'start_equity':previous,'end_equity':row['equity']}
        by_year[y]['end_equity']=row['equity'];previous=row['equity']
    return {'total_return':values[-1]/initial_cash-1,'annual_return':annual,'max_drawdown':drawdown,
        'annual_volatility':vol,'sharpe_rf0':sharpe,'calmar':annual/abs(drawdown) if drawdown else None,
        'ending_equity':values[-1],'max_drawdown_period':drawdown_period,'yearly_returns':{k:v['end_equity']/v['start_equity']-1 for k,v in by_year.items()}}


def normalize_actions(folder,symbols,start,end,calendar):
    root=Path(folder);events=dividend_events(root)
    fallback={(r['SECUCODE'],day(r.get('EX_DIVIDEND_DATE'))):r for r in events}
    grouped=defaultdict(list)
    for code in sorted(symbols):
        for year in range(start.year,end.year+1):
            path=root/'actions'/(code+'_'+str(year)+'.json')
            if not path.exists():raise ValueError(f'缺少 {code}/{year} 的现金派发日/送股上市日记录')
            for row in read(path)['rows']:
                ex=day(row.get('dividOperateDate'))
                if ex and start<=ex<=end:grouped[(code,ex)].append(row)
    unique={}
    for (code,ex),rows in grouped.items():
        related=fallback.get((code,ex),{})
        def merge(field,parser,default=None):
            values={parser(r.get(field)) for r in rows};values.discard(None)
            if len(values)>1:raise ValueError(f'{code}/{ex} 分红{field}存在冲突版本')
            return next(iter(values)) if values else default
        record=merge('dividRegistDate',day) or day(related.get('EQUITY_RECORD_DATE'))
        cash=merge('dividCashPsBeforeTax',number)
        bonus=merge('dividStocksPs',number,0.)+merge('dividReserveToStockPs',number,0.)
        pay=merge('dividPayDate',day);listing=merge('dividStockMarketDate',day)
        em_cash=number(related.get('PRETAX_BONUS_RMB'))
        if cash is None:
            # Pure transfers explicitly have no cash; missing cash in an actual
            # cash plan must not silently become zero.
            if em_cash is None and bonus>0 and all('派' not in str(r.get('dividCashStock','')) for r in rows):cash=0.
            else:raise ValueError(f'{code}/{ex} 缺少税前每股现金分红')
        if not record or (cash>0 and not pay) or (bonus>0 and not listing):
            raise ValueError(f'{code}/{ex} 缺少股权登记/现金派发/送股上市日期')
        if em_cash is not None and abs(em_cash/10-cash)>1e-5:raise ValueError(f'{code}/{ex} 两源每股分红金额不一致')
        unique[(code,ex)]={'symbol':code,'record':record,'ex':ex,'cash':cash,'bonus':bonus,'pay':pay,'listing':listing}
    expected={(r.get('SECUCODE'),day(r.get('EX_DIVIDEND_DATE'))) for r in events
              if r.get('SECUCODE') in symbols and start<= (day(r.get('EX_DIVIDEND_DATE')) or date.min) <=end
              and r.get('ASSIGN_PROGRESS')=='实施分配'
              and ((number(r.get('PRETAX_BONUS_RMB')) or 0)>0 or (number(r.get('BONUS_IT_RATIO')) or 0)>0)}
    missing=expected-set(unique)
    if missing:raise ValueError('两源除权事件未对齐：'+str(sorted((s,str(d)) for s,d in missing)[:12]))
    return list(unique.values())


def simulate(calendar,months,bars,actions,basic,config=None,*,cancel_event=None,progress_cb=None):
    config=config or Config()
    if not calendar:raise ValueError('没有交易日')
    cash=config.initial_cash;positions={};last_close={};locks=defaultdict(list);cash_due=[];pending_ex=[]
    pending_sell=set();current_targets=set();trades=[];blocked=[];curve=[];corporate=[];holdings=[]
    volumes=defaultdict(list);total_fees=defaultdict(float);cash_dividends=0.;tax_provision=0.;delisted=[]
    trade_months={day(m['trade_date']):m for m in months};record_actions=defaultdict(list)
    for action in actions:record_actions[action['record']].append(action)
    previous_equity=config.initial_cash
    # Prime prices/volume using archived pre-period bars without opening positions.
    for code,history in bars.items():
        prior=[(d,r) for d,r in history.items() if d<calendar[0]]
        for d,r in sorted(prior):
            close=number(r.get('close'));volume=number(r.get('volume'))
            if close and close>0:last_close[code]=close
            if volume and volume>0:volumes[code].append(volume)
        volumes[code]=volumes[code][-20:]

    def mark(d):
        value=cash+sum(a['amount'] for a in cash_due)
        for code,qty in positions.items():
            bar=bars.get(code,{}).get(d)
            if bar is None:raise ValueError(f'{code}/{d} 缺少持仓日线，不能静默填平净值')
            close=number(bar.get('close'))
            if close and close>0:last_close[code]=close
            elif code not in last_close:raise ValueError(f'{code}/{d} 缺少持仓估值')
            value+=qty*last_close[code]
        return value

    def order(code,d,side,qty):
        nonlocal cash
        bar=bars.get(code,{}).get(d);ok,reason=executable(bar,side)
        if not ok:
            blocked.append({'date':str(d),'symbol':code,'side':side,'reason':reason});return 0
        recent=volumes.get(code,[])
        if len(recent)<5:raise ValueError(f'{code}/{d} 缺少成交量预热数据')
        capacity=int(statistics.mean(recent[-20:])*config.participation/100)*100
        qty=min(qty,capacity)
        if side=='sell':
            locked=sum(q for unlock,q in locks[code] if unlock>d)
            qty=min(qty,positions.get(code,0)-locked)
            # Full exits may dispose odd-lot remainders created by stock dividends.
            if qty!=positions.get(code,0):qty=int(qty/100)*100
        else:qty=int(qty/100)*100
        if qty<=0:return 0
        reference=float(bar['open']);price=reference*(1+config.slippage_bps/10000*(1 if side=='buy' else -1))
        if side=='buy':
            while qty>0:
                charge=fees(qty*price,d,side,config)
                if qty*price+charge['total']<=cash+1e-8:break
                qty-=100
            if qty<=0:return 0
        charge=fees(qty*price,d,side,config)
        if side=='buy':cash-=qty*price+charge['total'];positions[code]=positions.get(code,0)+qty
        else:
            cash+=qty*price-charge['total'];positions[code]-=qty
            if positions[code]==0:del positions[code]
        if cash < -.001:raise ValueError('模拟出现负现金')
        for key,value in charge.items():total_fees[key]+=value
        total_fees['slippage']+=qty*abs(price-reference)
        trades.append({'date':str(d),'symbol':code,'side':side,'shares':qty,'reference_open':reference,'fill_price':price,
                       'notional':qty*price,'fees':charge,'cash_after':cash})
        return qty

    for day_index,d in enumerate(calendar):
        if cancel_event is not None and cancel_event.is_set():raise ValueError("回测已取消")
        if progress_cb is not None and day_index % 20 == 0:
            progress_cb({"day":day_index,"total":len(calendar),"date":str(d),"equity":previous_equity})
        # Actual ex/payment/listing dates. Receivables count in NAV but cannot
        # fund purchases until the cash payment date.
        for entitlement in list(pending_ex):
            if entitlement['ex']<=d:
                gross=entitlement['qty']*entitlement['cash'];tax=gross*config.dividend_tax
                if gross:
                    cash_due.append({'pay':entitlement['pay'],'amount':gross-tax})
                    cash_dividends+=gross;tax_provision+=tax
                bonus=math.floor(entitlement['qty']*entitlement['bonus']+1e-7)
                if bonus:
                    code=entitlement['symbol'];positions[code]=positions.get(code,0)+bonus
                    locks[code].append((entitlement['listing'],bonus))
                    if code not in current_targets:pending_sell.add(code)
                corporate.append({**{k:str(v) if isinstance(v,date) else v for k,v in entitlement.items()},'gross_cash':gross,'tax_provision':tax,'bonus_shares':bonus})
                pending_ex.remove(entitlement)
        for amount in list(cash_due):
            if amount['pay']<=d:cash+=amount['amount'];cash_due.remove(amount)
        for code in list(positions):
            out=day(basic.get(code,{}).get('outDate'))
            if out and d>=out:
                delisted.append({'symbol':code,'date':str(d),'shares':positions[code],'policy':'无法卖出退市持仓按零估值，未假定场外回收'})
                positions.pop(code);pending_sell.discard(code)
        month=trade_months.get(d)
        if month:
            eligible=month['ranked']
            chosen=eligible[:config.top_n];current_targets={r['symbol'] for r in chosen}
            pending_sell.update(set(positions)-current_targets);pending_sell-=current_targets
        # Retry obsolete holdings on subsequent trading days, never force a
        # fill through suspension or an opening limit-down.
        for code in sorted(pending_sell):
            if code in positions:order(code,d,'sell',positions[code])
        pending_sell={s for s in pending_sell if s in positions}
        if month:
            # Budgets are based on prior close NAV, not today's future close.
            budget=previous_equity*config.max_exposure/config.top_n
            for row in chosen:
                code=row['symbol'];bar=bars.get(code,{}).get(d)
                if not bar or not number(bar.get('open')):continue
                wanted=int(budget/(float(bar['open'])*(1+config.slippage_bps/10000))/100)*100
                excess=positions.get(code,0)-wanted
                if excess>=100:order(code,d,'sell',int(excess/100)*100)
            for row in chosen:
                code=row['symbol'];bar=bars.get(code,{}).get(d)
                if not bar or not number(bar.get('open')):
                    blocked.append({'date':str(d),'symbol':code,'side':'buy','reason':'无开盘行情，不补造成交'});continue
                wanted=int(budget/(float(bar['open'])*(1+config.slippage_bps/10000))/100)*100
                need=wanted-positions.get(code,0)
                if need>=100:order(code,d,'buy',int(need/100)*100)
        # Record-day buyers qualify; stocks bought on ex-date do not.
        for action in record_actions.get(d,[]):
            qty=positions.get(action['symbol'],0)
            if qty:pending_ex.append({**action,'qty':qty})
        equity=mark(d)
        if not math.isfinite(equity) or equity<=0:raise ValueError('净值无效')
        curve.append({'date':str(d),'equity':equity,'cash':cash,'receivables':sum(a['amount'] for a in cash_due),'positions':len(positions)})
        if month:holdings.append({'date':str(d),'positions':dict(positions),'cash':cash})
        previous_equity=equity
        for code,history in bars.items():
            bar=history.get(d)
            if bar and (number(bar.get('volume')) or 0)>0:volumes[code]=(volumes[code]+[float(bar['volume'])])[-20:]
    return {'config':asdict(config),'stats':{**stats(curve,config.initial_cash),'trades':len(trades),
             'fees':dict(total_fees),'gross_cash_dividends':cash_dividends,'dividend_tax_provision':tax_provision,
             'blocked_orders':len(blocked),'delisted_holdings':delisted},
            'curve':curve,'trades':trades,'blocked':blocked,'corporate_actions':corporate,'monthly_holdings':holdings}


@lru_cache(maxsize=16000)
def _valid_digest(path,modified,size,expected):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected


def run(folder,config=None,start=None,end=None,*,cancel_event=None,progress_cb=None):
    root=Path(folder);manifest=read(root/'manifest.json')
    if not manifest.get('complete'):raise ValueError('历史采集未完成')
    if not manifest.get('delisted_supplement_checked'):raise ValueError('退市公司分红尚未核验，不能以当前存续股票池回测')
    for record in manifest['files']:
        path=root/record['file']
        if not path.resolve().is_relative_to(root.resolve()):raise ValueError('历史档案路径越界')
        stat=path.stat()
        if not _valid_digest(str(path),stat.st_mtime_ns,stat.st_size,record['sha256']):raise ValueError('历史档案指纹不匹配')
    start=start or day(manifest['start']);end=end or day(manifest['end'])
    calendar,months=monthly_dates(read(root/'calendar/all.json')['rows'],start,end)
    targets=[m for m in read(root/'targets.json')['rows'] if start<=day(m['trade_date'])<=end]
    cfg=config or Config()
    symbols=set()
    for month in targets:
        eligible=month['ranked']
        symbols.update(r['symbol'] for r in eligible[:cfg.top_n])
    bars={s:{day(r['date']):r for r in read(root/'prices'/(s+'.json'))['rows']} for s in sorted(symbols)}
    basic={r['code'][3:]+'.'+r['code'][:2].upper():r for r in read(root/'basic/all.json')['rows']}
    actions=normalize_actions(root,symbols,start,end,calendar)
    result=simulate([d for d in calendar if start<=d<=end],targets,bars,actions,basic,config,cancel_event=cancel_event,progress_cb=progress_cb)
    result['data_quality']={'point_in_time':False,'kind':'公开历史重建研究回测，非原始公告PIT认证',
        'months':len(targets),'missing_capital_candidates':sum(len(m['missing_capital']) for m in targets),
        'later_revised_selected':sum(r.get('revision_unverified',False) for m in targets for r in m['selected']),
        'selected_slots':sum(len(m['selected']) for m in targets),
        'missing_financial_candidates':sum(m['coverage']['missing_financial'] for m in targets),
        'deferred_profit_candidates_below_verified_top20':sum(r.get('requires_profit_check',False) for m in targets for r in m.get('preliminary_ranked',[]))}
    from app.services.dividend_screen import MAIN
    div_codes={r['SECUCODE'] for r in dividend_events(root) if MAIN.fullmatch(r.get('SECUCODE','')) and (number(r.get('PRETAX_BONUS_RMB')) or 0)>0}
    historical_pool={s for s in div_codes if s in basic and day(basic[s].get('ipoDate')) and day(basic[s]['ipoDate'])<=end and (not day(basic[s].get('outDate')) or day(basic[s]['outDate'])>=start)}
    result['data_quality'].update(historical_dividend_universe_symbols=len(historical_pool),
        delisted_symbols_in_universe=sum(bool(day(basic[s].get('outDate'))) for s in historical_pool),
        missing_basic_symbols=sorted(div_codes-set(basic)),
        simulation_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    result['benchmarks']={}
    dates={r['date'] for r in result['curve']}
    for code in ['000300.SH','000852.SH','000001.SH']:
        rows=[r for r in read(root/'benchmark'/(code+'.json'))['rows'] if r['date'] in dates]
        if len(rows)!=len(dates):raise ValueError(f'{code} 基准行情覆盖不完整')
        opening=float(rows[0]['open'])
        curve=[{'date':r['date'],'equity':(config or Config()).initial_cash*float(r['close'])/opening} for r in rows]
        result['benchmarks'][code]={'description':'价格指数，不含现金分红，不扣指数投资成本','stats':stats(curve,(config or Config()).initial_cash),'curve':curve}
    return result
