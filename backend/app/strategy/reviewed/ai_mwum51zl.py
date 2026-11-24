"""已审查的菜场大妈候选股筛选器；不实现月度组合交易。"""
import polars as pl
from app.services.dividend_screen import load_history

META = {
    "id": "ai_mwum51zl",
    "current_snapshot_only": True,
    "version": "1.1.0",
    "name": "菜场大妈：分红低价小市值",
    "description": "已审查候选股版：股息率≥3%、不复权股价2～9元，按总市值选前10只。使用公开财报、已实施分红与当前报价；仅当前收盘筛选，不支持历史回测及月度组合交易。",
    "tags": ["分红", "低价", "小市值", "已审查", "候选股版", "当前公开数据"],
    "basic_filter": {"enabled": False},
    "params": [
        {"id": "dividend_yield_min", "label": "最低股息率（小数）", "type": "float", "default": 0.03, "min": 0.01, "max": 0.10, "step": 0.005},
        {"id": "top_n", "label": "候选股数量", "type": "int", "default": 10, "min": 5, "max": 20, "step": 1},
    ],
    "scoring": {}, "order_by": "market_cap", "descending": False, "limit": 20,
}
LOOKBACK_DAYS = 1
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = None
ALERTS = []
RULES = """沪深主板普通A股，排除ST、退市整理期和停牌，上市满250个交易日。
以每日收盘时点筛选：不复权股价2～9元；过去12个月已实施分红/不复权收盘价≥3%；已披露归母净利润>0。
总市值=不复权收盘价×当时总股本；按市值、代码升序选前10只。
输入分红必须已实施且按当时股本口径换算；财报、股本和证券状态必须为该日已知版本，不得用当前值倒填。
本文件只生成每日候选，月初下一交易日成交、等权调仓及费用、T+1等组合撮合尚未实现，不得据此宣称完整策略回测收益。
缺少依赖时明确报错，不以空候选或零收益掩盖缺数据。"""

REQUIRED = {"symbol", "date", "raw_close", "dividend_per_share_12m", "net_profit", "total_shares",
            "is_st", "is_delisting", "is_suspended", "listed_trading_days", "fundamentals_available_date"}


def prepare_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    missing = sorted(REQUIRED - set(df.columns))
    if missing:
        raise ValueError("菜场大妈缺少历史时点数据：" + "、".join(missing) + "；已保存，暂不能选股/回测，禁止用当前数据倒填")
    return df


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    df = prepare_history(df, params)
    minimum = float(params.get("dividend_yield_min", .03))
    n = int(params.get("top_n", 10))
    if not .01 <= minimum <= .10 or not 5 <= n <= 20:
        raise ValueError("股息率阈值需为0.01～0.10，候选数量需为5～20")
    if df.is_empty():
        return df
    # Input dates are daily PIT snapshots, never today's fundamentals carried backwards.
    future = df.filter(pl.col("fundamentals_available_date").cast(pl.Date) > pl.col("date").cast(pl.Date))
    if not future.is_empty():
        raise ValueError("基本面数据晚于选股日期，拒绝使用未来数据")
    valid = df.filter(
        pl.col("symbol").str.contains(r"^(?:600|601|603|605)\d{3}\.SH$|^(?:000|001|002|003)\d{3}\.SZ$")
        & (pl.col("is_st") == False) & (pl.col("is_delisting") == False) & (pl.col("is_suspended") == False)
        & (pl.col("listed_trading_days") >= 250)
        & pl.col("fundamentals_available_date").is_not_null()
        & pl.col("raw_close").is_finite() & pl.col("raw_close").is_between(2, 9)
        & pl.col("dividend_per_share_12m").is_finite() & (pl.col("dividend_per_share_12m") >= 0)
        & pl.col("total_shares").is_finite() & (pl.col("total_shares") > 0)
        & pl.col("net_profit").is_finite() & (pl.col("net_profit") > 0)
    ).with_columns(
        (pl.col("dividend_per_share_12m") / pl.col("raw_close")).alias("dividend_yield"),
        (pl.col("raw_close") * pl.col("total_shares")).alias("market_cap"),
    ).filter(pl.col("dividend_yield") >= minimum)
    if valid.is_empty():
        return valid
    # Avoid stale upstream score columns reordering the smallest-cap selection.
    return (valid.drop("score", strict=False).sort(["date", "market_cap", "symbol"])
            .group_by("date", maintain_order=True).head(n))
