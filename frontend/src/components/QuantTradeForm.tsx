import { useRef, useState } from 'react'
import type { QuantPortfolio, QuantSuggestion, QuantTradeRequest } from '@/lib/api'
import { quantReferencePrice } from '@/lib/quant-assistant'

export function QuantTradeForm({ item, suggestionKey, portfolio, onSubmit, onClose }: {
  item: QuantSuggestion; suggestionKey: string; portfolio: QuantPortfolio
  onSubmit: (payload: QuantTradeRequest) => Promise<void>; onClose: () => void
}) {
  const [quantity, setQuantity] = useState(String(item.quantity))
  const [price, setPrice] = useState('')
  const [fees, setFees] = useState('')
  const [time, setTime] = useState(() => {
    const now = new Date()
    return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
  })
  const [clientId] = useState(() => crypto.randomUUID().replace(/-/g, ''))
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [error, setError] = useState('')
  const style = 'mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm disabled:opacity-50'
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busyRef.current) return
    setError('')
    if (!/^\d+$/.test(quantity) || !Number.isSafeInteger(Number(quantity)) || Number(quantity) <= 0 || Number(quantity) > 1e9) { setError('实际成交数量须为正整数，单位为股'); return }
    if (!/^(?:\d+(?:\.\d{1,6})?|\.\d{1,6})$/.test(price) || Number(price) <= 0 || Number(price) > 1e6) { setError('请填写实际成交价，最多六位小数'); return }
    if (!/^\d+(?:\.\d{1,2})?$/.test(fees) || !Number.isFinite(Number(fees)) || Number(fees) > 1e12) { setError('请填写费用，最多两位小数；无费用请填0'); return }
    const stamp = new Date(time)
    if (!Number.isFinite(stamp.getTime()) || stamp.getTime() > Date.now() + 60000) { setError('请填写已经发生的成交时间'); return }
    busyRef.current = true; setBusy(true)
    try {
      await onSubmit({ client_id: clientId, suggestion_key: suggestionKey, expected_portfolio: portfolio,
        symbol: item.symbol, side: item.action === 'buy' ? 'buy' : 'sell', quantity: Number(quantity),
        price: Number(price), fees: Number(fees), executed_at: stamp.toISOString() })
    } catch (err) { setError(err instanceof Error ? err.message : '记录失败，请重试') }
    finally { busyRef.current = false; setBusy(false) }
  }
  return <form onSubmit={submit} aria-label="记录实际成交" className="m-4 rounded-card border-2 border-accent/40 bg-accent/5 p-4">
    <h3 className="font-semibold">记录已完成的{item.action === 'buy' ? '买入' : '卖出'}：{item.name} {item.symbol}</h3>
    <p className="mt-2 text-xs text-secondary">参考数量 {item.quantity} 股 · 参考价 {quantReferencePrice(item.price)}。请按券商实际成交填写，确认后更新本地持仓与现金，不会发送交易订单。</p>
    <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <label className="text-xs">实际成交数量（股）<input className={style} aria-label="实际成交数量" value={quantity} inputMode="numeric" disabled={busy} onChange={e => setQuantity(e.target.value)} /></label>
      <label className="text-xs">实际成交均价（元）<input className={style} aria-label="实际成交均价" value={price} placeholder="填写实际成交价" inputMode="decimal" disabled={busy} onChange={e => setPrice(e.target.value)} /></label>
      <label className="text-xs">全部费用（元）<input className={style} aria-label="全部费用" value={fees} placeholder="含佣金税费，无费用填0" inputMode="decimal" disabled={busy} onChange={e => setFees(e.target.value)} /></label>
      <label className="text-xs">实际成交时间<input type="datetime-local" className={style} aria-label="实际成交时间" value={time} disabled={busy} onChange={e => setTime(e.target.value)} /></label>
    </div>
    <p className="mt-3 text-xs text-muted">买入成本按含买入费用的加权平均更新；卖出保留剩余持仓均价，清仓后成本归零。新买入默认不增加可卖数量，可卖数量须按券商账户核对更新。</p>
    {error && <p role="alert" className="mt-3 text-sm text-red-500">{error}</p>}
    <div className="mt-4 flex flex-wrap gap-3"><button type="submit" disabled={busy} className="rounded-btn bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{busy ? '正在记账…' : '确认成交并更新持仓'}</button><button type="button" disabled={busy} onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm">取消</button></div>
  </form>
}
