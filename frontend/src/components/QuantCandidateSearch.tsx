import { useEffect, useState } from 'react'
import { Loader2, Search } from 'lucide-react'
import { api } from '@/lib/api'

type Instrument = { symbol: string; name: string }

export function QuantCandidateSearch({ disabled, onSelect }: { disabled: boolean; onSelect: (item: Instrument) => boolean }) {
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<Instrument[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [searched, setSearched] = useState(false)
  useEffect(() => {
    let active = true
    setItems([])
    setError('')
    setSearched(false)
    setLoading(false)
    if (disabled || !query.trim()) return
    setLoading(true)
    const timer = window.setTimeout(() => {
      api.instrumentSearch(query.trim(), 8).then(response => {
        if (active) { setItems(response.results); setSearched(true) }
      }).catch(error => {
        if (active) setError(error instanceof Error ? error.message : '搜索失败，请重试')
      }).finally(() => { if (active) setLoading(false) })
    }, 250)
    return () => { active = false; window.clearTimeout(timer) }
  }, [query, disabled])

  return <div className="mt-4 max-w-xl">
    <label className="block text-xs text-secondary" htmlFor="quant-candidate-search">搜索股票 / ETF 添加候选</label>
    <div className="relative mt-1"><Search className="absolute left-3 top-3 h-4 w-4 text-muted" />
      <input id="quant-candidate-search" type="search" maxLength={50} autoComplete="off" disabled={disabled}
        value={query} onChange={event => setQuery(event.target.value)} placeholder="代码或名称，如 513100 / 纳指ETF"
        className="w-full rounded-btn border border-border bg-base py-2 pl-9 pr-9 text-sm outline-none focus:border-accent disabled:opacity-50" />
      {loading && <Loader2 aria-label="搜索中" className="absolute right-3 top-3 h-4 w-4 animate-spin text-muted" />}
    </div>
    {!disabled && query.trim() && <div aria-live="polite" className="mt-2 space-y-1">
      {error && <p role="alert" className="text-xs text-amber-500">{error}，可修改关键词重新搜索。</p>}
      {searched && items.length === 0 && <p className="text-xs text-muted">未找到匹配标的，可手动添加代码，或在数据页同步证券清单。</p>}
      {items.map(item => <button key={item.symbol} type="button" onClick={() => { if (onSelect(item)) setQuery('') }}
        className="flex w-full items-center justify-between gap-3 rounded-btn border border-border px-3 py-2 text-left text-sm hover:bg-elevated focus-visible:outline-accent">
        <span>{item.name} <span className="font-mono text-xs text-muted">{item.symbol}</span></span><span className="shrink-0 text-xs text-accent">添加候选</span>
      </button>)}
    </div>}
    <p className="mt-1 text-[11px] text-muted">搜索本地证券清单；添加后数量为 0，请按账户填写持仓和成本。</p>
  </div>
}
