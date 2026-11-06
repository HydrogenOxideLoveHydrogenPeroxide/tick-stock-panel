"""Shared preparation and adapters for price strategies + one value factor.

The input is ranked over the PIT financial universe BEFORE technical selection.
Missing fundamentals never become a neutral score and never delete price rows.
"""
from __future__ import annotations
from pathlib import Path
import math
import sys
import polars as pl
from app.services.buffett_factors import COMPOSITE, FACTOR_IDS, build_factor_panel, load_dataset

VARIANTS = {
    'weekly': ('quant_weekly_quality_value', 'Quant 一周多因子＋个股价值（研究）'),
    'context': ('context_sentiment_quality_value', '环境情绪多头＋个股价值（研究）'),
}


def validation_code_paths(root):
    """Fixed audited paths; validation UI never follows paths supplied in JSON."""
    paths = ['app/services/buffett_factors.py','app/services/quality_value_strategy.py',
             'app/services/quant_assistant.py','app/services/context_strategy_status.py','app/services/fundamental_sync.py','app/api/quant_assistant.py',
             'app/strategy/engine.py','app/backtest/strategy.py','app/backtest/factor.py','app/main.py',
             'app/strategy/builtin/weekly_quality_value.py','app/strategy/builtin/context_quality_value.py',
             'scripts/validate_quality_value.py','scripts/sync_value_fundamentals.py','tests/test_fundamental_sync.py','tests/test_buffett_factors.py','tests/test_quality_value_strategy.py']
    return [Path(root)/'backend'/path for path in paths] + [Path(root).parent/'quant-rsi/quant/index'/name
        for name in ('panel.py','weekly.py','weekly_v2.py','context_core_panel.py','context_fusion.py','weekly_parameters.json')]


def attach_value_factor(panel, data_dir):
    data = load_dataset(Path(data_dir))
    financial = build_factor_panel(data, panel['date'].unique().to_list())
    if financial.is_empty() or financial[COMPOSITE].drop_nulls().is_empty():
        raise ValueError('所选历史窗口缺少当时可知的行业内价值综合分；禁止用今日修订财报倒填或以中性分回退原策略')
    clean = panel.drop([name for name in FACTOR_IDS if name in panel.columns])
    joined = clean.join(financial.select('symbol', 'date', COMPOSITE), on=['symbol','date'], how='left')
    if not joined[COMPOSITE].is_not_null().any():
        raise ValueError('价值数据与当前行情股票池/日期没有有效交集，暂停新买入')
    return joined


def _modules():
    root = Path(__file__).resolve().parents[4] / 'quant-rsi'
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from quant.index import panel, weekly, weekly_v2, context_core_panel
    return panel, weekly, weekly_v2, context_core_panel


def make_adapter(kind):
    base, weekly, weekly_v2, context = _modules()
    weekly_kind = kind == 'weekly'
    sid, name = VARIANTS[kind]
    original = base if weekly_kind else context
    meta = dict(original.META)
    meta.update(id=sid, name=name, scoring={}, order_by='qv_score', descending=True,
        fundamental_factor=COMPOSITE,
        description=('周五收盘信号；历史创业板涨跌幅规则修正，排除当前撮合不支持的科创/北交所。' if weekly_kind else '保留指数环境、广度情绪及技术多头条件。') +
        '原策略80%＋行业内个股质量价值20%；缺PIT财报/市值阻止新买入。未验证收益改善，不包含真实竞价或题材。',
        tags=list(meta.get('tags', []))+['个股价值', '未验证'],
        params=list(meta.get('params', []))+[dict(id='quality_value_weight', label='个股价值权重（研究）', type='float', default=.20, min=.05, max=.40, step=.05)],
        backtest_defaults={**meta.get('backtest_defaults', {}), 'start':'2018-01-02','end':'2026-11-04'})

    def prepare_history(df, params, *, benchmark=None):
        if COMPOSITE not in df.columns:
            raise ValueError('价值策略必须通过共享策略引擎接入时点财报因子')
        weight = float(params.get('quality_value_weight', .20))
        if not math.isfinite(weight) or not .05 <= weight <= .40:
            raise ValueError('个股价值权重必须为5%～40%，不能设为0绕过数据校验')
        if weekly_kind:
            # Correct historical 10%/20% bands, retaining the baseline six-factor
            # weights. This is a separate research variant, not a silent rewrite.
            df = weekly.score(weekly_v2.historical_features(df), {**base.DEFAULTS, **params}).with_columns(
                (pl.col('date').dt.weekday()==5).alias('signal_quant_weekly_entry'))
            base_score = 'quant_week_score'
        else:
            df = context.prepare_history(df, params, benchmark=benchmark)
            base_score = 'cx_core_score'
        valid = (pl.col(COMPOSITE).is_finite() & pl.col(COMPOSITE).is_between(0,100)
                 & pl.col(base_score).is_finite()).fill_null(False)
        df = df.with_columns(valid.alias('qv_available'), pl.col(base_score).alias('qv_base_score'), pl.lit(weight).alias('qv_weight'))
        df = df.with_columns(pl.when(pl.col('qv_available')).then(
            (1-weight)*pl.col('qv_base_score')+weight*pl.col(COMPOSITE)).otherwise(None).alias('qv_score'))
        return df.with_columns(pl.col('qv_score').alias('score'), pl.col('qv_score').alias('quant_week_score'))

    def filter_history(df, params):
        if 'qv_score' not in df.columns:
            raise ValueError('缺少价值融合历史因子，禁止退回原模型')
        if weekly_kind:
            p={**base.DEFAULTS, **params}
            mask=(pl.col('q_eligible') & (pl.col('q_volatility')<=float(p['vol_max']))
                  & (pl.col('q_momentum_20')>=float(p['momentum_min'])) & (pl.col('date').dt.weekday()==5))
            top=int(p['top_n'])
        else:
            mask=pl.col('q_eligible') & pl.col('cx_bull') & pl.col('cx_market_allowed') & pl.col('cx_sentiment_allowed')
            top=int(params.get('top_n',10))
        if not 1 <= top <= 20:
            raise ValueError('候选数量需在1～20之间')
        # Apply Top N only AFTER blending. Reusing weekly.candidates() here would
        # recompute the old score and silently erase the newly introduced factor.
        return (df.filter(mask & pl.col('qv_available')).sort(['date','qv_score','symbol'], descending=[False,True,False])
                .group_by('date', maintain_order=True).head(top))

    return dict(META=meta, BASIC_FILTER={'enabled':False}, ENTRY_SIGNALS=original.ENTRY_SIGNALS,
                EXIT_SIGNALS=original.EXIT_SIGNALS, STOP_LOSS=None, MAX_HOLD_DAYS=original.MAX_HOLD_DAYS,
                LOOKBACK_DAYS=max(120,original.LOOKBACK_DAYS), prepare_history=prepare_history, filter_history=filter_history)
