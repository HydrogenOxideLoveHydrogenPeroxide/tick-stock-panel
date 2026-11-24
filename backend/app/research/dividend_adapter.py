"""Adapter for the panel's existing portfolio backtest API.

This deliberately exposes an explicitly labelled public-history research result,
not an unrestricted bypass of the current-snapshot strategy guard.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import date, timedelta
import math
from pathlib import Path
import time
import uuid
from app.research.dividend_history import read
from app.research.dividend_simulator import Config,run

RELATIVE=Path('research/dividend_backtest_2021')


def build_result(cfg,data_dir,strategy,*,progress_cb=None,cancel_event=None):
    from app.backtest.strategy import StrategyBacktestResult
    start_clock=time.monotonic()
    params={**{p['id']:p['default'] for p in strategy.meta['params']},**((cfg.overrides or {}).get('params') or {}),**(cfg.params or {})}
    if abs(float(params.get('dividend_yield_min',.03))-.03)>1e-9:
        raise ValueError('本历史研究档案固定股息率3%；不能用未覆盖的新阈值冒充同一回测')
    raw_n=params.get('top_n',10)
    try:n=int(raw_n)
    except (ValueError,TypeError) as exc:raise ValueError('选股数量需为整数') from exc
    if isinstance(raw_n,bool) or n!=float(raw_n):raise ValueError('选股数量需为整数')
    if not 5<=n<=20:raise ValueError('选股数量需为5～20')
    if cfg.symbols:raise ValueError('此回测按历史全市场排名；请清空自选/指定股票池，避免只回测当前股票')
    if cfg.mode!='position' or cfg.entry_fill!='open_t+1' or cfg.exit_fill!='open_t+1':
        raise ValueError('本策略按月度组合、次交易日开盘执行；请使用持仓回测及次日开盘撮合')
    if cfg.position_sizing!='equal' or cfg.max_positions<n:
        raise ValueError('本策略需等权持仓，持仓上限不得低于选股数量')
    if not 0<cfg.max_exposure_pct<=1:raise ValueError('总仓位需大于0且不超过100%')
    overrides=cfg.overrides or {}
    if any(overrides.get(k) for k in ('stop_loss','take_profit','trailing_stop','trailing_take_profit_activate','trailing_take_profit_drawdown','max_hold_days','entry_signals','exit_signals','scoring')):
        raise ValueError('此历史回测不包含额外技术买卖点、止损或最长持有天数，请恢复原策略设置')
    if (overrides.get('basic_filter') or {}).get('enabled'):
        raise ValueError('原策略的价格、ST及上市条件由时点数据执行，请关闭额外基础过滤')
    if cfg.stamp_tax_pct is not None:raise ValueError('本研究按历史印花税政策执行，请使用默认印花税')
    if not math.isfinite(cfg.initial_capital) or cfg.initial_capital<=0 or not math.isfinite(cfg.slippage_bps) or not 0<=cfg.slippage_bps<=1000:raise ValueError('资金/滑点参数无效')
    commission=cfg.commission_pct if cfg.commission_pct is not None else cfg.fees_pct
    if not math.isfinite(commission) or not 0<=commission<=.01:raise ValueError('佣金参数无效')
    folder=Path(data_dir)/RELATIVE;path=folder/'manifest.json'
    if not path.exists():raise ValueError('历史研究数据仍在准备；当前快照不能代替历史数据，请待历史归档完成后重试')
    manifest=read(path)
    first=date.fromisoformat(manifest['start']);last=date.fromisoformat(manifest['end'])
    start=first if cfg.start==date(1900,1,1) else cfg.start
    if start<first or cfg.end>last or start>cfg.end:
        raise ValueError(f'历史研究档案覆盖 {first} 至 {last}，请选择此区间；不能扩展到缺数据日期')
    result=run(folder,Config(initial_cash=cfg.initial_capital,top_n=n,commission=commission,
                  slippage_bps=cfg.slippage_bps,max_exposure=cfg.max_exposure_pct),start=start,end=cfg.end,
               progress_cb=progress_cb,cancel_event=cancel_event)
    values=result['curve'];peak=cfg.initial_capital;drawdowns=[]
    for row in values:peak=max(peak,row['equity']);drawdowns.append({'date':row['date'],'value':row['equity']/peak-1})
    info={'id':strategy.meta['id'],'name':strategy.meta['name']+'（历史重建研究）','description':
          '历史公告日、股本生效日与逐日ST/停牌对齐；公开财报修订版本未逐份核验，非严格PIT认证。',
          'entry_signals':[],'exit_signals':[],'stop_loss':None,'take_profit':None,'trailing_stop':None,
          'trailing_take_profit_activate':None,'trailing_take_profit_drawdown':None,'max_hold_days':None,'source':'custom'}
    quality=result['data_quality'];stats=result['stats']
    stats.update(mode='position',n_trades=len(result['trades']),sharpe=stats['sharpe_rf0'],
        benchmark_return=result['benchmarks']['000001.SH']['stats']['total_return'],
        research_warning=f"公开历史重建研究，非原始公告PIT认证。所选{quality['selected_slots']}个股票×月份中，{quality['later_revised_selected']}个财报记录后来更新；缺股本候选{quality['missing_capital_candidates']}个。分红统一按20%计提税款；交易次数按成交委托统计，未计算平仓胜率。",
        research_orders=result['trades'],data_quality=quality,
        data_coverage={'trading_days':len(values),'actual_start':values[0]['date'],'actual_end':values[-1]['date']},
        execution={'blocked_orders':len(result['blocked'])})
    benchmark=result['benchmarks']['000001.SH']['curve']
    # The chart normalizes its first benchmark point. Include the uninvested
    # pre-start baseline so the first opening-to-close return is retained.
    prior=date.fromisoformat(values[0]['date'])-timedelta(days=1)
    benchmark_curve=[{'date':str(prior),'value':cfg.initial_capital,'close':cfg.initial_capital,'name':'上证价格指数'}]
    benchmark_curve += [{'date':r['date'],'value':r['equity'],'close':r['equity'],'name':'上证价格指数'} for r in benchmark]
    return StrategyBacktestResult(run_id=uuid.uuid4().hex[:10],config={**asdict(cfg),'start':str(start),'end':str(cfg.end)},
        stats=stats,equity_curve=[{'date':r['date'],'value':r['equity'],'cash':r['cash'],'positions':r['positions'],
            'exposure':1-(r['cash']+r['receivables'])/r['equity']} for r in values],drawdown_curve=drawdowns,
        benchmark_curve=benchmark_curve,trades=[],strategy_info=info,elapsed_ms=(time.monotonic()-start_clock)*1000)


def history_info(data_dir):
    path=Path(data_dir)/RELATIVE/'manifest.json'
    manifest=read(path) if path.exists() else {}
    ready=bool(manifest.get('complete') and manifest.get('delisted_supplement_checked'))
    return {'earliest_date':manifest.get('start') if ready else None,'latest_date':manifest.get('end') if ready else None,
            'archive_available':ready,'source':'公开历史重建研究档案',
            'note':'月度组合；原始公告版本未核验，非严格PIT认证。' if ready else '历史研究档案尚未准备完成。'}
