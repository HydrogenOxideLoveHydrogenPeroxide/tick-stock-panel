import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { editQuantIdentity, exactQuantInstrument, type HoldingDraft, type QuantInstrument } from '@/lib/quant-assistant'

type Identity = Pick<HoldingDraft, 'symbol' | 'name'>
type Field = 'symbol' | 'name'
interface Props {
  holding: HoldingDraft
  index: number
  disabled: boolean
  inputClass: string
  onEdit: (field: Field, value: string) => void
  onSelect: (item: QuantInstrument, expected: Identity) => string | null
}
type Lookup = { field: Field; value: string; expected: Identity; automatic: boolean }

export function QuantInstrumentFields({ holding, index, disabled, inputClass, onEdit, onSelect }: Props) {
  const [lookup, setLookup] = useState<Lookup | null>(null)
  const [items, setItems] = useState<QuantInstrument[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeIndex, setActiveIndex] = useState(-1)
  const selectRef = useRef(onSelect)
  selectRef.current = onSelect

  useEffect(() => {
    let active = true
    setItems([]); setError(''); setLoading(false); setActiveIndex(-1)
    if (!lookup || disabled || !lookup.value.trim() || lookup.expected.symbol !== holding.symbol || lookup.expected.name !== holding.name) return
    setLoading(true)
    const timer = window.setTimeout(() => {
      api.instrumentSearch(lookup.value.trim(), 8).then(response => {
        if (!active) return
        setItems(response.results)
        const exact = lookup.automatic ? exactQuantInstrument(lookup.value, response.results) : null
        if (exact) {
          const failure = selectRef.current(exact, lookup.expected)
          if (failure) setError(failure)
          else { setOpen(false); setLookup(null) }
        }
      }).catch(err => { if (active) setError(err instanceof Error ? err.message : '搜索失败，请稍后重试') })
        .finally(() => { if (active) setLoading(false) })
    }, 250)
    return () => { active = false; window.clearTimeout(timer) }
  }, [lookup, disabled, holding.symbol, holding.name])

  function search(field: Field, value: string, changed: boolean) {
    const expected = changed ? editQuantIdentity(holding, field, value) : holding
    if (changed) onEdit(field, value)
    setLookup({ field, value, expected, automatic: changed || (field === 'symbol' ? !holding.name : !holding.symbol) })
    setOpen(true)
  }
  function choose(item: QuantInstrument) {
    if (!lookup || disabled) return
    const failure = selectRef.current(item, lookup.expected)
    if (failure) setError(failure)
    else { setOpen(false); setLookup(null) }
  }
  return <>{(['symbol', 'name'] as const).map(field => {
    const expanded = open && !disabled && lookup?.field === field && !!lookup.value.trim()
    const listId = 'quant-instrument-' + index + '-' + field
    return <td key={field} className="px-2 py-3 align-top">
      <div className="relative">
        <input className={inputClass + (field === 'symbol' ? ' min-w-[165px] pr-8 font-mono' : ' min-w-[180px] pr-8')}
          aria-label={'第 ' + (index + 1) + ' 行' + (field === 'symbol' ? '代码' : '名称')}
          role="combobox" aria-autocomplete="list" aria-expanded={expanded} aria-controls={expanded ? listId : undefined}
          aria-activedescendant={expanded && activeIndex >= 0 && items[activeIndex] ? listId + '-' + activeIndex : undefined}
          autoComplete="off" maxLength={field === 'symbol' ? 12 : 100} value={holding[field]} disabled={disabled}
          placeholder={field === 'symbol' ? '输入代码，如 513100' : '输入名称，如 纳指ETF'}
          onChange={e => search(field, e.target.value, true)} onFocus={() => search(field, holding[field], false)}
          onBlur={() => setOpen(false)} onKeyDown={event => {
            if (event.nativeEvent?.isComposing) return
            if (event.key === 'Escape') { setOpen(false); setLookup(null); return }
            if (!expanded || !items.length) return
            if (event.key === 'ArrowDown') { event.preventDefault(); setActiveIndex(i => Math.min(i + 1, items.length - 1)) }
            if (event.key === 'ArrowUp') { event.preventDefault(); setActiveIndex(i => Math.max(i - 1, 0)) }
            if (event.key === 'Enter') { event.preventDefault(); choose(items[Math.max(0, activeIndex)]) }
          }} />
        {loading && lookup?.field === field && <Loader2 aria-label="正在匹配证券" className="absolute right-2 top-3 h-3.5 w-3.5 animate-spin text-muted" />}
      </div>
      {expanded && <div className="mt-1 min-w-[220px] rounded-btn border border-border bg-base shadow-sm">
        {error && <p role="alert" className="px-3 py-2 text-xs text-amber-500">{error}</p>}
        {!loading && !error && items.length === 0 && <p className="px-3 py-2 text-xs text-muted">未找到匹配，请检查代码或名称</p>}
        <ul id={listId} role="listbox" aria-label="匹配证券" className="max-h-52 overflow-y-auto">{items.map((item, i) => <li key={item.symbol} role="option" id={listId + '-' + i} aria-selected={activeIndex === i}>
          <button type="button" tabIndex={-1} onMouseDown={event => event.preventDefault()} onClick={() => choose(item)}
            className={'flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs hover:bg-elevated ' + (activeIndex === i ? 'bg-elevated' : '')}>
            <span>{item.name}</span><span className="font-mono text-muted">{item.symbol}</span>
          </button>
        </li>)}</ul>
      </div>}
    </td>
  })}</>
}
