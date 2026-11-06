"""Offline validation of integration and audited public-source sample.

Does not download, optimize, place orders, write portfolio or overwrite frozen
research. Current-revision statements are checked, never backdated into returns.
Run from backend with its .venv-local Python; see BUFFETT_FACTORS.md.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, time
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parent
sys.path.insert(0,str(BACKEND));sys.path.insert(0,str(BACKEND/'tests'))
from app.services.buffett_factors import AnnualStatement, SHANGHAI, dataset_status, load_dataset, evaluate_dataset
from app.services.quality_value_strategy import VARIANTS, validation_code_paths
from app.strategy.engine import StrategyEngine
from app.backtest.engine import BacktestEngine
from app.backtest.strategy import StrategyBacktestService, StrategyBacktestConfig


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_source(path):
    if path.stat().st_size>8*1024*1024:raise ValueError('source bundle too large')
    result=json.loads(path.read_text())
    if result['status']!='success' or result.get('errors'):raise ValueError('source bundle not successful')
    tables={k:result['data'][k] for k in ('balance_sheet','income_statement','cash_flow')}
    by_table={k:{r['report_date']:r for r in v['items'] if r['report_date'].endswith('12-31')} for k,v in tables.items()}
    periods=sorted(set.intersection(*(set(v) for v in by_table.values())))
    if len(periods)<6:raise ValueError('fewer than six complete annual periods')
    records=[];checks=[]
    for period in periods:
        balance,income,cash=(by_table[k][period] for k in ('balance_sheet','income_statement','cash_flow'))
        if any(r['currency']!='CNY' or r['amount_unit']!='元' for r in (balance,income,cash)):
            raise ValueError('unexpected currency or units')
        if balance['basis']!='period_end' or any(r['basis']!='year_to_date' for r in (income,cash)):
            raise ValueError('unexpected report basis')
        codes={r['code'] for r in (balance,income,cash)}
        if codes!={'600519'}:raise ValueError('sample identity mismatch')
        if not all(r.get('disclosed_date') for r in (balance,income,cash)):
            raise ValueError('missing actual disclosure date')
        observed=max(datetime.fromisoformat(result['provenance'][k]['fetched_at']) for k in tables)
        disclosure=max(date.fromisoformat(r['disclosed_date']) for r in (balance,income,cash))
        updates=[date.fromisoformat(r['updated_date']) for r in (balance,income,cash) if r.get('updated_date')]
        # Dates carry no intraday disclosure evidence; take end of day.
        revised=datetime.combine(max(updates),time.max,SHANGHAI) if updates else None
        record=AnnualStatement(symbol='600519.SH',report_date=date.fromisoformat(period),
            disclosed_at=datetime.combine(disclosure,time.max,SHANGHAI),revised_at=revised,observed_at=observed,
            source=tables['balance_sheet']['source_url'],company_type='non_financial',industry='audit_only_unclassified',
            revenue=income['revenue'],net_profit=income['net_profit'],operating_cash_flow=cash['operating_cash_flow'],
            capex_cash_paid=cash['source_fields'].get('CONSTRUCT_LONG_ASSET'),total_assets=balance['total_assets'],
            total_liabilities=balance['total_liabilities'],total_equity=balance['total_equity'])
        error=abs(record.total_assets-record.total_liabilities-record.total_equity)/record.total_assets
        if error>.001 or record.capex_cash_paid is None:raise ValueError('accounting identity or capex check failed')
        records.append(record)
        checks.append(dict(report_date=period,disclosed_at=record.disclosed_at.isoformat(),revised_at=record.revised_at.isoformat() if revised else None,
            available_at=record.available_at.isoformat(),balance_relative_error=error,capex_cash_paid=record.capex_cash_paid,
            cfo_less_capex=record.operating_cash_flow-record.capex_cash_paid))
    return dict(status='passed_current_statement_mapping_only',symbol='600519.SH',annual_periods=len(records),
        source_bundle_sha256=sha(path),source_bundle=str(path),point_in_time=all(v.get('point_in_time',False) for v in tables.values()),
        sources=sorted({v['source_url'] for v in tables.values()}),checks=checks,
        first_usable_at=min(r.available_at for r in records).isoformat(),
        note='真实来源字段、会计恒等式与金额口径核验；单公司样本，不是行业排名或选股收益验证。未导入生产因子档案。')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-bundle',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve()
    if out.exists():raise ValueError('use a new output directory; validation records are not overwritten')
    model=ROOT.parent/'quant-rsi/quant/index/weekly_parameters.json'
    model_before=sha(model)
    book=ROOT/'data/user_data/quant_assistant.json';book_before=sha(book) if book.exists() else None
    source=audit_source(args.source_bundle.resolve())
    suites=[]
    for pattern in ('test_fundamental_sync.py','test_buffett_factors.py','test_quality_value_strategy.py','test_quant*.py','test_context_strategy_status.py'):
        stream=io.StringIO();suite=unittest.defaultTestLoader.discover(str(BACKEND/'tests'),pattern=pattern)
        result=unittest.TextTestRunner(stream=stream).run(suite)
        suites.append(dict(pattern=pattern,tests=result.testsRun,passed=result.wasSuccessful()))
        if not result.wasSuccessful():raise ValueError(stream.getvalue()[-5000:])
    namespace=runpy.run_path(str(BACKEND/'tests/backtest/test_strategy_backtest_correctness.py'))
    checks=0
    for name,fn in namespace.items():
        if name.startswith('test_') and callable(fn):fn();checks+=1
    suites.append(dict(pattern='existing_strategy_correctness',tests=checks,passed=True))
    store=SimpleNamespace(data_dir=ROOT/'data')
    class DependencyRepo:
        def __init__(self):self.store=store;self.price_loads=0
        def get_raw_daily_range(self,*a,**kw):
            self.price_loads+=1;raise AssertionError('missing finance must stop before large price load')
    repo=DependencyRepo()
    strategies=StrategyEngine(lambda _:None,strategy_files=[BACKEND/'app/strategy/builtin'/f'{kind}_quality_value.py' for kind in VARIANTS],data_dir=store.data_dir)
    blocked=[]
    for year in range(2018,2027):
        for sid,name in VARIANTS.values():
            end=min(date(year,12,31),date(2026,11,4))
            native=StrategyBacktestService(BacktestEngine(repo),strategies).run(StrategyBacktestConfig(
                strategy_id=sid,symbols=None,start=date(year,1,1),end=end))
            if not native.error or '依赖数据未就绪' not in native.error:
                raise ValueError('expected missing-data preflight to block real performance validation')
            blocked.append(dict(year=year,strategy_id=sid,error=native.error,performance_metrics=None))
    assert repo.price_loads==0
    assert sha(model)==model_before
    assert (sha(book) if book.exists() else None)==book_before
    code=validation_code_paths(ROOT)
    current=evaluate_dataset(load_dataset(store.data_dir),datetime.now(SHANGHAI))
    summary=dataset_status(store.data_dir)
    report=dict(current_snapshot=dict(decision_at=current['decision_at'],symbols=len(current['rows']),scored=sum(r['score'] is not None for r in current['rows']),
                note='仅已采集5只同业子样本，不代表全市场、收益回测或买入建议'),generated_at=datetime.now(SHANGHAI).isoformat(),validation_status='integration_passed_performance_blocked',
        prediction_validated=False,promotion=False,baseline_parameters_unchanged=True,portfolio_unchanged=True,
        baseline_parameter_sha256=model_before,code_sha256={str(p):sha(p) for p in code},
        protocol=dict(original_price_weight=.80,quality_value_weight=.20,optimized=False,
            compare=['original_baseline','historical_rule_fix_only','historical_rule_fix_plus_value'],
            execution='T收盘可知数据，T+1开盘撮合；不使用今日财报倒填',
            required_checks=['年度10/50bp成本压力对照','行业覆盖与规模暴露','Rank IC/IR、分层单调性','逐笔成交及逐日净值一致'],
            limitation='目前只有合成会计样本验证执行链路，真实PIT档案与历史总市值缺失，不能做增量收益判断'),
        variants=[dict(strategy_id=sid,name=name) for sid,name in VARIANTS.values()],
        tests=suites,total_tests=sum(s['tests'] for s in suites),source_audit=source,
        data_status=summary,real_historical_preflights=blocked,
        note='18个年度策略依赖检查均因真实数据缺失阻断；它们不是18个收益回测，更不是空仓零收益。')
    out.mkdir(parents=True)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    lines=['# 个股价值因子接入验证','',f"生成：{report['generated_at']}",'',
        '**链路验证通过，真实收益效果尚未验证，禁止据此升级实盘。**','',
        f"- 自动检查：{report['total_tests']} 项通过。",'- 固定融合：原分数80%＋行业内个股质量价值20%，未选参。',
        '- 一周版本修正历史创业板涨跌幅规则，并保留周五信号；环境版本保留原环境/情绪/趋势条件。',
        '- 修复选股与回测未一致应用保存的策略参数；固定优先级为默认值、保存参数、显式请求。',
        '- 缺少价值分阻止新买入，不填中性分、不改变既有持仓退出行情。',
        '- 原模型参数及真实持仓/现金未修改。','',
        '## 真实来源核验','',
        f"贵州茅台完整年度三表覆盖 {source['annual_periods']} 年；CNY金额、资本开支字段和资产负债恒等式核验通过。",
        f"当前版本最早可用时间：{source['first_usable_at']}。来源声明 point_in_time={source['point_in_time']}。",
        '例如2025年度资产负债表披露于2026-04-17，但当前来源修订日期为2026-10-31；不能把这个修订版本提前放进4月交易信号。',
        f"另已完成公开源采集：{summary['statement_versions']}条年度记录、{summary['market_versions']}条市值快照版本；当前已采集子样本 {len(current['rows'])} 只，可评分 {sum(r['score'] is not None for r in current['rows'])} 只。不是历史收益或全市场选股验证。",'',
        '## 历史效果门槛','',report['note'],
        '现有基线报告保持冻结；目前无法比较加入价值因子的真实增量收益。需要原始公告版本/实际归档时间、历史行业、历史总市值及公司行动数据后，按固定协议进行对照。','']
    (out/'report.md').write_text('\n'.join(lines))
    print(json.dumps(dict(output=str(out),tests=report['total_tests'],validation_status=report['validation_status'],annual_source_periods=source['annual_periods'],blocked_real_windows=len(blocked)),ensure_ascii=False))

if __name__=='__main__':main()
