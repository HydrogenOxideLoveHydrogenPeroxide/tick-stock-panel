"""Expose a fixed research artifact, not a live auction or valuation feed."""
import json
import hashlib
from pathlib import Path


def context_strategy_status():
    from app.config import settings
    from app.services.buffett_factors import dataset_status
    value_factors = dataset_status(settings.data_dir)
    from app.services.quality_value_strategy import VARIANTS, validation_code_paths
    value_factors['strategy_variants'] = [dict(strategy_id=sid,name=name) for sid,name in VARIANTS.values()]
    validation = None
    report_path = Path(__file__).resolve().parents[3]/'research/value-integration-20261106-sources-final/report.json'
    if report_path.exists() and report_path.stat().st_size <= 1024*1024:
        checked = json.loads(report_path.read_text())
        # Check only known local implementation paths; never follow arbitrary
        # paths embedded in a report in order to populate this UI.
        implementation = validation_code_paths(Path(__file__).resolve().parents[3])
        current = all(checked.get('code_sha256',{}).get(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in implementation)
        validation = dict(status=checked['validation_status'],code_current=current,total_tests=checked['total_tests'],
                          prediction_validated=False,real_historical_windows_blocked=len(checked['real_historical_preflights']),
                          generated_at=checked['generated_at'])
    file=Path(__file__).resolve().parents[3].parent/'quant-rsi/quant/output/factors/context-fusion-20261106-v2/report.json'
    status=dict(name='环境情绪多头（研究子模型）',strategy_id='context_sentiment_bull_core',
        full_status='blocked_missing_data',components=[
            dict(name='市场环境',status='日线指数可检验'),dict(name='市场情绪',status='涨跌广度可检验，不是新闻情绪'),
            dict(name='技术多头',status='均线与量价可检验'),dict(name='竞价资金强度',status='缺真实09:25竞价历史'),
            dict(name='题材热度',status='缺PIT历史成分与热度'),dict(name='市场整体估值（巴菲特指标）',status='市场层面；仅年度参考，缺实时市值与PIT历史分位')],
        promotion='research_only',report_available=False,value_factors=value_factors,value_integration=validation)
    if not file.exists():
        return status
    if file.stat().st_size>1024*1024:
        raise ValueError('研究报告文件异常')
    report=json.loads(file.read_text())
    selected=[r for r in report['results'] if r['variant']=='sentiment' and r['slippage']==10]
    status.update(report_available=True,generated_at=report['generated_at'],core_gate_passed=report['core_gate_passed'],
        buffett=report['buffett_reference'],scenarios=len(report['results']),
        annual_results=[dict(year=r['year'],total_return=r['metrics']['total_return'],
            max_drawdown=r['metrics']['max_drawdown'],trades=r['metrics']['trades']) for r in selected])
    return status
