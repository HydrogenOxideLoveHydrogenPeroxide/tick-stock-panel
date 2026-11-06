"""Pure closing-price metrics, adapted from quant.metric.ma/rsi/macd.
Finite source window, no network/storage, bars not trading days for minute data.
"""
import math

def _ema(values, n):
    if not values:
        return []
    k = 2 / (n + 1)
    previous = values[0]
    out = []
    for value in values:
        previous = value * k + previous * (1-k)
        out.append(previous)
    return out

def price_metrics(rows: list[dict], period: str) -> dict:
    closes = [float(row['close']) for row in rows]
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError('指标价格必须为正有限数')
    count = len(closes)
    ma = {str(n): sum(closes[-n:])/n if count >= n else None for n in (5,10,20,60)}
    rsi = None
    if count > 14:
        differences = [closes[i]-closes[i-1] for i in range(1,count)]
        gain = sum(max(d,0) for d in differences[:14])/14
        loss = sum(max(-d,0) for d in differences[:14])/14
        for change in differences[14:]:
            gain = (gain*13+max(change,0))/14
            loss = (loss*13+max(-change,0))/14
        rsi = 50. if gain == loss == 0 else 100. if loss == 0 else 100-100/(1+gain/loss)
    macd = None
    if count >= 35:
        dif = [a-b for a,b in zip(_ema(closes,12),_ema(closes,26))]
        dea = _ema(dif,9)
        macd = {'dif':dif[-1],'dea':dea[-1],'hist':2*(dif[-1]-dea[-1])}
    return dict(period=period,as_of=rows[-1]['date'] if rows else None,
                available_bars=count,ma=ma,rsi14=rsi,macd=macd,
                method='SMA; Wilder RSI14 (flat=50); EMA seeded first close; MACD12/26/9 hist=2*(DIF-DEA)',
                limitation='指标按可用来源前序K线预热，缺失时段不补值；分钟指标不是日线指标，EMA受有限窗口初值影响。')
