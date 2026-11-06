"""Versioned, opt-in Buffett-style *proxies*, not Buffett's proprietary formula.

Annual consolidated CNY statements only. No GDP, DCF target price, news, LLM,
network request, order or portfolio mutation. Missing/revised data never backfills
an earlier decision. See BUFFETT_FACTORS.md for economic and PIT limitations.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
import math
import statistics
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHANGHAI = ZoneInfo('Asia/Shanghai')
NUMBER = Annotated[float, Field(allow_inf_nan=False, ge=-1e16, le=1e16)]
FACTOR_META = [
    dict(id='bv_roe', label='价值质量·五年总权益ROE', direction=1, weight=.30, formula='median(年度合并净利润 / 期初期末平均总权益)'),
    dict(id='bv_cash_conversion', label='价值质量·五年现金兑现', direction=1, weight=.20, formula='sum(五年经营现金流) / sum(五年合并净利润)'),
    dict(id='bv_fcf_yield', label='价值估值·保守自由现金流收益率', direction=1, weight=.30, formula='median(五年经营现金流－全部资本开支) / 当时可知总市值'),
    dict(id='bv_margin_stability', label='价值质量·利润率稳定性', direction=1, weight=.10, formula='－stdev(五年合并净利率)，越接近0越稳定'),
    dict(id='bv_balance_safety', label='价值质量·资产负债安全度', direction=1, weight=.10, formula='1－最近年度总负债 / 总资产'),
]
COMPOSITE = 'bv_quality_value'
FACTOR_IDS = {m['id'] for m in FACTOR_META} | {COMPOSITE}
DATASET_RELATIVE = Path('fundamentals/buffett_v1.json')
LIMITATIONS = [
    '本项目自定义研究代理因子，不是巴菲特公布的固定打分公式。',
    'CFO减全部资本开支仅为保守现金流代理，不等同于Owner Earnings、FCFE或可分配股利；不生成内在价值或目标价。',
    '总权益ROE使用含少数股东损益的合并净利润与平均总权益；不是归母ROE。',
    '行业内百分位只在有效样本至少5家公司时产生综合分；缺值不填0，不跨行业凑样本。',
    '仅支持非金融A股；ETF、银行、保险、券商等不适用。当前股票池仍可能有幸存者偏差。',
    '读取时间和披露/修订时间共同决定可用时间；今天下载的修订版不能用于过去的回测。',
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class TimedSource(StrictModel):
    symbol: str = Field(pattern=r'^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$')
    observed_at: datetime
    source: str = Field(min_length=8, max_length=1000, pattern=r'^https://')
    currency: Literal['CNY'] = 'CNY'
    amount_unit: Literal['yuan'] = 'yuan'

    @field_validator('observed_at', check_fields=False)
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('必须提供带时区的真实时间')
        return value.astimezone(SHANGHAI)


class AnnualStatement(TimedSource):
    report_date: date
    disclosed_at: datetime
    revised_at: datetime | None = None
    statement_scope: Literal['consolidated'] = 'consolidated'
    basis: Literal['annual'] = 'annual'
    company_type: Literal['non_financial', 'bank', 'insurance', 'securities', 'other_financial']
    industry: str = Field(min_length=1, max_length=100)
    revenue: NUMBER | None = None
    net_profit: NUMBER | None = None
    operating_cash_flow: NUMBER | None = None
    capex_cash_paid: Annotated[float, Field(ge=0, le=1e16, allow_inf_nan=False)] | None = None
    total_assets: NUMBER | None = None
    total_liabilities: NUMBER | None = None
    total_equity: NUMBER | None = None

    @field_validator('disclosed_at', 'revised_at')
    @classmethod
    def aware_dates(cls, value):
        return TimedSource.aware(value) if value is not None else None

    @model_validator(mode='after')
    def annual_only(self):
        if (self.report_date.month, self.report_date.day) != (12, 31):
            raise ValueError('只接受完整年度数据，不能把累计季度数据直接当年度或TTM')
        if self.disclosed_at.date() <= self.report_date:
            raise ValueError('披露时间必须晚于报告期末')
        if self.revised_at and self.revised_at < self.disclosed_at:
            raise ValueError('修订时间不能早于披露时间')
        return self

    @property
    def available_at(self):
        return max(self.disclosed_at, self.observed_at, self.revised_at or self.disclosed_at)


class MarketValue(TimedSource):
    as_of: datetime
    market_cap_cny: Annotated[float, Field(gt=0, le=1e16, allow_inf_nan=False)]
    cap_basis: Literal['total_equity_market_cap'] = 'total_equity_market_cap'

    @field_validator('as_of')
    @classmethod
    def aware_as_of(cls, value):
        return TimedSource.aware(value)

    @property
    def available_at(self):
        return max(self.as_of, self.observed_at)


class FactorDataset(StrictModel):
    version: Literal[1] = 1
    statements: list[AnnualStatement] = Field(max_length=100000)
    market_values: list[MarketValue] = Field(max_length=100000)

    @model_validator(mode='after')
    def unambiguous(self):
        keys = [(r.symbol, r.report_date, r.available_at) for r in self.statements]
        markets = [(r.symbol, r.as_of, r.available_at) for r in self.market_values]
        if len(set(keys)) != len(keys) or len(set(markets)) != len(markets):
            raise ValueError('同一证券/报告期/可用时间存在重复版本，必须先核验去重')
        return self


def load_dataset(data_dir: Path) -> FactorDataset:
    path = data_dir / DATASET_RELATIVE
    if not path.exists():
        raise ValueError('尚无个股价值因子数据：需要6个连续年度财报版本、资本开支、行业和带可用时间的历史总市值。请先准备 fundamentals/buffett_v1.json；不会用今日数据倒填历史。')
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('价值因子数据文件异常或超过32MiB')
    try:
        return FactorDataset.model_validate_json(path.read_bytes())
    except Exception as exc:
        raise ValueError('价值因子数据不满足版本、单位或时间约束，请核验数据文件') from exc


def dataset_status(data_dir: Path):
    base = dict(name='巴菲特式个股质量—价值（研究因子）', factors=FACTOR_META + [
        dict(id=COMPOSITE, label='价值质量·行业内综合分', direction=1, weight=1., formula='行业内百分位：30%ROE＋20%现金兑现＋30%FCF收益率＋10%稳定性＋10%负债安全度')], limitations=LIMITATIONS)
    try:
        data = load_dataset(data_dir)
        if not data.statements or not data.market_values:
            raise ValueError('财报版本或市值快照为空；暂不能计算价值因子')
        return dict(base, status='data_loaded_unvalidated', statement_versions=len(data.statements), market_versions=len(data.market_values), note='数据已加载；各决策日仍需时间、覆盖和行业样本校验，未验证预测能力。')
    except ValueError as exc:
        return dict(base, status='blocked_missing_data', statement_versions=0, market_versions=0, note=str(exc))


def known_input_symbols(data, decision):
    decision=TimedSource.aware(decision)
    reported={r.symbol for r in data.statements if r.available_at<=decision}
    # Dependency preflight checks whether any usable history can exist. Quote
    # freshness is evaluated at each signal date, not only the backtest end.
    valued={m.symbol for m in data.market_values if m.available_at<=decision}
    return reported & valued


def _one(symbol, statements, markets, decision):
    row = dict(symbol=symbol, status='blocked', reason='', factors=None, score=None)
    def blocked(reason):
        return dict(row, reason=reason)
    available = [r for r in statements if r.available_at <= decision]
    by_year = {}
    for r in available:
        if r.report_date.year not in by_year or by_year[r.report_date.year].available_at < r.available_at:
            by_year[r.report_date.year] = r
    if not by_year:
        return blocked('决策时点无可用财报版本')
    last = max(by_year)
    years = [by_year.get(y) for y in range(last - 5, last + 1)]
    if any(r is None for r in years):
        return blocked('缺少6个连续年度报告（5年指标及首年期初总权益）')
    current = years[-1]
    if current.company_type != 'non_financial':
        return blocked('金融企业不适用本因子')
    if any(r.company_type != 'non_financial' for r in years):
        return blocked('历史公司类别不一致')
    if (decision.date() - current.report_date).days > 550:
        return blocked('最近年度财报过旧（超过550天）')
    for r in years:
        fields = ('revenue', 'net_profit', 'operating_cash_flow', 'capex_cash_paid', 'total_assets', 'total_liabilities', 'total_equity')
        if any(getattr(r, k) is None for k in fields):
            return blocked('财报科目缺失，尤其资本开支不能填0')
        if r.revenue <= 0 or r.total_assets <= 0 or r.total_equity <= 0 or r.total_liabilities < 0:
            return blocked('营收/资产/总权益或负债不满足计算条件')
        if abs(r.total_assets - r.total_equity - r.total_liabilities) > max(1., r.total_assets * .001):
            return blocked('资产负债恒等式不一致，疑似单位或合并范围混用')
    usable = [m for m in markets if m.available_at <= decision and timedelta(0) <= decision - m.as_of <= timedelta(days=7)]
    if not usable:
        return blocked('缺少决策时点可知且7天内的总市值')
    market = max(usable, key=lambda m: (m.as_of, m.available_at))
    window = years[1:]
    if any(r.net_profit <= 0 for r in window):
        return blocked('近5年存在亏损或零利润，不满足本版质量约束')
    roe = [r.net_profit / ((prior.total_equity + r.total_equity) / 2) for prior, r in zip(years, window)]
    margins = [r.net_profit / r.revenue for r in window]
    values = dict(bv_roe=statistics.median(roe),
        bv_cash_conversion=sum(r.operating_cash_flow for r in window) / sum(r.net_profit for r in window),
        bv_fcf_yield=statistics.median(r.operating_cash_flow - r.capex_cash_paid for r in window) / market.market_cap_cny,
        bv_margin_stability=-statistics.pstdev(margins), bv_balance_safety=1-current.total_liabilities/current.total_assets)
    if not all(math.isfinite(v) for v in values.values()):
        return blocked('计算结果非有限值')
    return dict(row, status='valid_raw_factors', reason='等待行业内有效样本排名', factors=values,
        industry=current.industry, report_date=str(current.report_date), market_as_of=market.as_of.isoformat(),
        available_at=max([r.available_at for r in years]+[market.available_at]).isoformat(),
        sources=sorted({r.source for r in years} | {market.source}), percentiles=None)


def evaluate_dataset(data: FactorDataset, decision: datetime, symbols=None):
    decision = TimedSource.aware(decision)
    reports, markets = defaultdict(list), defaultdict(list)
    for r in data.statements:
        reports[r.symbol].append(r)
    for m in data.market_values:
        markets[m.symbol].append(m)
    universe = sorted(set(symbols) if symbols is not None else set(reports) | set(markets))
    rows = [_one(s, reports[s], markets[s], decision) for s in universe]
    groups = defaultdict(list)
    for row in rows:
        if row['factors'] is not None:
            groups[row['industry']].append(row)
    for peers in groups.values():
        if len(peers) < 5:
            for row in peers:
                row['reason'] = '行业内有效样本少于5家，仅输出原始因子，不生成综合分'
            continue
        # Sort each cross section once; pairwise comparisons become quadratic
        # for large industries and make a multi-year factor run impractical.
        lookups = {}
        for meta in FACTOR_META:
            key = meta['id']; values = sorted(row['factors'][key] for row in peers)
            lookup = {}; begin = 0
            while begin < len(values):
                end = begin + 1
                while end < len(values) and values[end] == values[begin]:
                    end += 1
                lookup[values[begin]] = (begin + (end - begin - 1) / 2) / (len(values) - 1)
                begin = end
            lookups[key] = lookup
        for row in peers:
            ranks = {meta['id']:lookups[meta['id']][row['factors'][meta['id']]] for meta in FACTOR_META}
            row.update(status='scored', reason='研究分数，不代表买入信号或内在价值', percentiles=ranks,
                       score=100 * sum(meta['weight'] * ranks[meta['id']] for meta in FACTOR_META))
    return dict(name='巴菲特式个股质量—价值（研究因子）', decision_at=decision.isoformat(),
                rows=rows, limitations=LIMITATIONS, validated_for_live=False)


def build_factor_panel(data, dates, symbols=None):
    # Market-close decision; newer disclosures/imports first become usable at a
    # later close. Factor research returns are not execution prices or orders.
    rows = []
    for day in sorted(set(dates)):
        result = evaluate_dataset(data, datetime.combine(day, time(15), SHANGHAI), symbols)
        for r in result['rows']:
            if r['factors'] is not None:
                rows.append(dict(symbol=r['symbol'], date=day, **r['factors'], **{COMPOSITE:r['score']}))
    schema = dict(symbol=pl.String, date=pl.Date, **{k:pl.Float64 for k in sorted(FACTOR_IDS)})
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
