import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Download, Loader2, RefreshCw, Plus, Save, ShieldCheck, Sparkles, Trash2, Wallet } from 'lucide-react'
import { ContextResearchCard } from '@/components/ContextResearchCard'
import { QuantTradeForm } from '@/components/QuantTradeForm'
import { QuantInstrumentFields } from '@/components/QuantInstrumentFields'
import { QuantCandidateSearch } from '@/components/QuantCandidateSearch'
import { retainQuantDraft, recoverQuantDraft, clearQuantDraft } from '@/lib/quant-draft-cache'
import { PageHeader } from '@/components/PageHeader'
import { api, type QuantCandidates, type QuantMode, type QuantPortfolio, type QuantSuggestions, type QuantSuggestion, type QuantStrategyCatalog, type QuantTradeRequest, type QuantTradeRecord } from '@/lib/api'
import {
  DEFAULT_QUANT_PORTFOLIO, newHoldingDraft, portfolioToDraft, validateQuantDraft,
  completeQuantHolding, editQuantIdentity, type QuantInstrument, addQuantCandidate, quantSuggestionTotals, quantReportJson, quantDraftDirty, quantContextValue, quantReferencePrice, type HoldingDraft, type PortfolioDraft, type StrategyDraft,
} from '@/lib/quant-assistant'

const inputClass = 'w-full rounded-btn border border-border bg-base px-3 py-2 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50'
const buttonClass = 'inline-flex items-center justify-center gap-2 rounded-btn border border-border bg-surface px-3 py-2 text-sm font-medium text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 disabled:cursor-not-allowed'
const modes: { value: QuantMode; label: string; hint: string }[] = [
  { value: 'morning', label: '早盘', hint: '早盘参考 · 检查开盘与趋势条件' },
  { value: 'noon', label: '午盘', hint: '午盘参考 · 检查盘中变化与风险' },
  { value: 'tail', label: '尾盘', hint: '尾盘参考 · 检查收盘前仓位与风险' },
]
const strategyFields: { key: keyof Omit<StrategyDraft, 'kind'>; label: string; min: number; max: number; hint: string }[] = [
  { key: 'stop_loss_pct', label: '止损比例', min: 1, max: 50, hint: '相对成本的亏损阈值' },
  { key: 'take_profit_pct', label: '止盈比例', min: 1, max: 200, hint: '相对成本的盈利阈值' },
  { key: 'max_position_pct', label: '单标的仓位上限', min: 1, max: 100, hint: '限制单一标的集中度' },
  { key: 'target_position_pct', label: '单标的目标仓位', min: 1, max: 100, hint: '不得超过仓位上限' },
  { key: 'cash_reserve_pct', label: '现金保留比例', min: 0, max: 100, hint: '保留现金，不计入买入预算' },
]
const actions: Record<QuantSuggestion['action'], { label: string; color: string }> = {
  buy: { label: '买入参考', color: 'text-red-500 bg-red-500/10' },
  sell: { label: '卖出参考', color: 'text-emerald-500 bg-emerald-500/10' },
  reduce: { label: '减仓参考', color: 'text-amber-500 bg-amber-500/10' },
  hold: { label: '持有 / 观察', color: 'text-secondary bg-elevated' },
  blocked: { label: '暂不可操作', color: 'text-amber-500 bg-amber-500/10' },
}
function money(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : '¥' + value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function percent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(2) + '%'
}
function message(error: unknown): string { return error instanceof Error ? error.message : '请求失败，请稍后重试' }
function Warnings({ warnings }: { warnings: string[] }) {
  return warnings.length ? <ul className="space-y-1 text-xs text-amber-500">{warnings.map((warning, i) => <li key={i} className="flex gap-2"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /><span>{warning}</span></li>)}</ul> : null
}
function SuggestionRow({ item, completed, disabled, onRecord }: { item: QuantSuggestion; completed: boolean; disabled: boolean; onRecord: () => void }) {
  const action = actions[item.action]
  const screener = item.data_context?.screener
  const screenerContext = screener && typeof screener === 'object' && !Array.isArray(screener) ? screener as Record<string, unknown> : null
  return <tr className="border-t border-border align-top">
    <td className="p-3"><div className="font-medium text-foreground">{item.name || '未命名'}</div><div className="mt-1 font-mono text-xs text-muted">{item.symbol}</div></td>
    <td className="p-3"><span className={'inline-block whitespace-nowrap rounded-btn px-2 py-1 text-xs ' + action.color}>{action.label}</span></td>
    <td className="p-3 text-right font-mono whitespace-nowrap">{item.quantity.toLocaleString('zh-CN')} 股</td>
    <td className="p-3 text-right font-mono whitespace-nowrap">{quantReferencePrice(item.price)}</td>
    <td className="p-3 min-w-[140px]">{completed ? <span className="inline-flex items-center gap-1 text-xs text-emerald-600"><CheckCircle2 className="h-4 w-4" />成交已记录</span> : ['buy', 'sell', 'reduce'].includes(item.action) && item.quantity > 0 ? <button className={buttonClass + ' border-accent/30 text-accent'} disabled={disabled} onClick={onRecord}>记录成交</button> : <span className="text-xs text-muted">无需操作</span>}</td>
    <td className="p-3 text-right font-mono whitespace-nowrap">{money(item.market_value)}</td>
    <td className="p-3 text-right font-mono whitespace-nowrap">{percent(item.position_pct)}</td>
    <td className={'p-3 text-right font-mono whitespace-nowrap ' + (item.pnl_pct == null ? '' : item.pnl_pct >= 0 ? 'text-red-500' : 'text-emerald-500')}>{percent(item.pnl_pct)}</td>
    <td className="min-w-[260px] p-3"><ul className="space-y-1 text-xs leading-relaxed">{item.reason.map((reason, i) => <li key={i}>{reason}</li>)}</ul><div className="mt-2"><Warnings warnings={item.warnings} /></div></td>
    <td className="min-w-[240px] p-3 text-xs text-secondary"><dl className="space-y-1"><div><dt className="inline text-muted">来源：</dt><dd className="inline break-words">{quantContextValue(item.data_context, 'source')}</dd></div><div><dt className="inline text-muted">数据时间：</dt><dd className="inline break-words">{quantContextValue(item.data_context, 'as_of')}</dd></div><div><dt className="inline text-muted">周期：</dt><dd className="inline break-words">{quantContextValue(item.data_context, 'period')}</dd></div></dl>
      {screenerContext && <div className="mt-2 border-t border-border pt-2 text-xs"><p>选股策略：{quantContextValue(screenerContext, 'name') === '未提供' ? quantContextValue(screenerContext, 'strategy_id') : quantContextValue(screenerContext, 'name')}</p><p>日线日期：{quantContextValue(screenerContext, 'as_of')}</p><p>{screenerContext.error ? '策略计算异常，见风险提示' : screenerContext.matched ? '已进入该策略筛选结果' : '未进入该策略筛选结果'}</p></div>}
      {item.data_context && <details className="mt-2"><summary className="cursor-pointer text-accent focus-visible:outline-accent">展开完整数据上下文</summary><pre className="mt-2 max-h-64 max-w-xs overflow-auto whitespace-pre-wrap break-all rounded-btn bg-base p-2">{JSON.stringify(item.data_context, null, 2)}</pre></details>}
    </td>
  </tr>
}

export function QuantAssistant() {
  const [draft, setDraft] = useState<PortfolioDraft>(() => portfolioToDraft(DEFAULT_QUANT_PORTFOLIO))
  const draftRef = useRef(draft)
  const [saved, setSaved] = useState<QuantPortfolio | null>(null)
  const [mode, setMode] = useState<QuantMode>('morning')
  const [result, setResult] = useState<QuantSuggestions | null>(null)
  const [candidates, setCandidates] = useState<QuantCandidates | null>(null)
  const [explanation, setExplanation] = useState<{ text: string; model: string; result: QuantSuggestions } | null>(null)
  const [resultPortfolio, setResultPortfolio] = useState<QuantPortfolio | null>(null)
  const [busy, setBusy] = useState<'load' | 'save' | 'suggest' | 'trade' | 'screen' | 'explain' | null>('load')
  const busyRef = useRef(true)
  const mounted = useRef(true)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [catalog, setCatalog] = useState<QuantStrategyCatalog | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [catalogBusy, setCatalogBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draftRetained, setDraftRetained] = useState(true)
  const [trades, setTrades] = useState<QuantTradeRecord[]>([])
  const [tradesError, setTradesError] = useState('')
  const [activeTrade, setActiveTrade] = useState<{ item: QuantSuggestion; key: string } | null>(null)
  const [resultHasTrades, setResultHasTrades] = useState(false)
  const [pendingDraft, setPendingDraft] = useState<PortfolioDraft | null>(null)
  const dirty = quantDraftDirty(draft, saved)
  const disabled = busy !== null || saved === null || pendingDraft !== null

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    let active = true
    busyRef.current = true
    setBusy('load')
    setLoadError('')
    // Portfolio GET only: no quote fetch, polling, AI request or auto-save.
    api.getQuantStrategyCatalog().then(cat => { if (active) { setCatalog(cat); setCatalogError('') } }).catch(err => { if (active) setCatalogError(message(err)) })
    api.getQuantTrades().then(records => { if (active) { setTrades(records); setTradesError('') } }).catch(err => { if (active) setTradesError(message(err)) })
    api.getQuantPortfolio().then(portfolio => {
      if (!active) return
      setSaved(portfolio)
      const recovery = recoverQuantDraft(draftStorage(), portfolio)
      setDraftRetained(recovery.status !== 'unavailable' && recovery.status !== 'invalid')
      setPendingDraft(recovery.status === 'conflict' ? recovery.draft! : null)
      replaceDraft(recovery.status === 'restored' ? recovery.draft! : portfolioToDraft(portfolio))
      if (recovery.status === 'restored') setNotice('已恢复本标签页未保存草稿，请核对后保存。')
    }).catch(err => { if (active) setLoadError(message(err)) })
      .finally(() => { if (active) { busyRef.current = false; setBusy(null) } })
    return () => { active = false }
  }, [loadAttempt])

  function suggestionKey(item: QuantSuggestion) {
    return [result?.generated_at, result?.mode, item.symbol, item.action].join('|')
  }
  async function recordCompletedTrade(payload: QuantTradeRequest) {
    if (busyRef.current || !saved || dirty) throw new Error('请先保存组合并重新生成建议，再记录实际成交')
    busyRef.current = true; setBusy('trade')
    try {
      const response = await api.recordQuantTrade(payload)
      if (!mounted.current) return
      clearQuantDraft(draftStorage())
      setSaved(response.portfolio)
      replaceDraft(portfolioToDraft(response.portfolio))
      setDraftRetained(true)
      setTrades(current => [response.trade, ...current.filter(t => t.client_id !== response.trade.client_id)].slice(0, 100))
      setTradesError('')
      setActiveTrade(null)
      setResultHasTrades(true)
      setError('')
      setNotice('成交已记录，持仓、成本与现金已更新。当前建议为成交前快照，请重新生成建议。')
    } finally { busyRef.current = false; if (mounted.current) setBusy(null) }
  }

  function draftStorage(): Storage | undefined {
    try { return window.sessionStorage } catch { return undefined }
  }
  function discardDraft() {
    if (!saved || busyRef.current) return
    if (!window.confirm('放弃本标签页的未保存修改，恢复服务器已保存组合？')) return
    setDraftRetained(clearQuantDraft(draftStorage()))
    setPendingDraft(null)
    replaceDraft(portfolioToDraft(saved))
    setResult(null); setResultPortfolio(null); setError(''); setNotice('已恢复服务器保存的组合。')
  }
  function replaceDraft(next: PortfolioDraft) {
    draftRef.current = next
    setDraft(next)
  }
  function completeHolding(index: number, item: QuantInstrument, expected: Pick<HoldingDraft, 'symbol' | 'name'>): string | null {
    if (busyRef.current || !saved) return '请等待当前操作完成'
    try {
      const next = completeQuantHolding(draftRef.current, index, item, expected)
      edit(() => next)
      return null
    } catch (err) {
      const detail = message(err)
      setError(detail)
      return detail
    }
  }
  function edit(update: (current: PortfolioDraft) => PortfolioDraft) {
    if (busyRef.current || !saved) return
    setActiveTrade(null)
    setResultHasTrades(false)
    const next = update(draftRef.current)
    setDraftRetained(retainQuantDraft(draftStorage(), next, saved))
    setPendingDraft(null)
    replaceDraft(next)
    setResult(null)
    setResultPortfolio(null)
    setError('')
    setNotice('')
  }
  async function syncStrategies() {
    if (catalogBusy) return
    setCatalogBusy(true)
    setCatalogError('')
    try {
      const next = await api.getQuantStrategyCatalog()
      if (mounted.current) { setCatalog(next); setNotice('已同步选股页 ' + next.strategies.length + ' 个策略；选择后请保存。') }
    } catch (err) { if (mounted.current) setCatalogError(message(err)) }
    finally { if (mounted.current) setCatalogBusy(false) }
  }

  function addCandidate(item: { symbol: string; name: string }): boolean {
    if (busyRef.current || !saved) return false
    try {
      const next = addQuantCandidate(draftRef.current, item)
      edit(() => next)
      setNotice('已添加候选：' + item.name + '。请填写实际持仓；草稿已保留，需点击保存提交。')
      return true
    } catch (err) { setError(message(err)); return false }
  }
  async function screenStrategy() {
    if (busyRef.current || !saved) return
    let portfolio: QuantPortfolio
    try { portfolio = validateQuantDraft(draftRef.current) } catch (err) { setError(message(err)); return }
    busyRef.current = true; setBusy('screen'); setError(''); setCandidates(null)
    try {
      const response = await api.getQuantCandidates(portfolio, mode)
      if (mounted.current) setCandidates(response)
    } catch (err) { if (mounted.current) setError(message(err)) }
    finally { busyRef.current = false; if (mounted.current) setBusy(null) }
  }
  async function explainStrategy() {
    if (busyRef.current || !saved) return
    let portfolio: QuantPortfolio
    try { portfolio = validateQuantDraft(draftRef.current) } catch (err) { setError(message(err)); return }
    if (!portfolio.holdings.length) { setError('请先按策略选股并加入候选，再生成解读'); return }
    busyRef.current = true; setBusy('explain'); setError(''); setExplanation(null); setActiveTrade(null)
    try {
      const response = await api.explainQuantPortfolio(portfolio, mode)
      if (!mounted.current) return
      setResult(response.result); setResultPortfolio(portfolio); setResultHasTrades(false)
      setExplanation({ text: response.analysis, model: response.model, result: response.result })
      setNotice('已重新计算参考股数并生成 LLM 解读；组合未自动保存。')
    } catch (err) { if (mounted.current) setError(message(err)) }
    finally { busyRef.current = false; if (mounted.current) setBusy(null) }
  }
  function downloadReport() {
    if (!result || !resultPortfolio) return
    const url = URL.createObjectURL(new Blob([quantReportJson(resultPortfolio, result)], { type: 'application/json;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = '量化助手-' + result.mode + '-' + result.generated_at.replace(/[^0-9T]/g, '-') + '.json'
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const totals = result ? quantSuggestionTotals(result) : null

  function editHolding(index: number, field: keyof HoldingDraft, value: string) {
    edit(current => ({ ...current, holdings: current.holdings.map((h, i) => i === index ? (field === 'symbol' || field === 'name' ? editQuantIdentity(h, field, value) : { ...h, [field]: value }) : h) }))
  }
  function deleteHolding(index: number) {
    if (disabled || !window.confirm('删除 ' + (draft.holdings[index].name || draft.holdings[index].symbol || '该标的') + '？删除仅修改草稿，需另行保存。')) return
    edit(current => ({ ...current, holdings: current.holdings.filter((_, i) => i !== index) }))
  }
  async function submit(kind: 'save' | 'suggest') {
    if (busyRef.current || !saved) return
    let portfolio: QuantPortfolio
    try { portfolio = validateQuantDraft(draft) } catch (err) { setError(message(err)); return }
    busyRef.current = true
    setBusy(kind)
    setError('')
    setNotice('')
    setActiveTrade(null)
    setResultHasTrades(false)
    if (kind === 'suggest') { setResult(null); setResultPortfolio(null) }
    try {
      if (kind === 'save') {
        const normalized = await api.saveQuantPortfolio(portfolio)
        recoverQuantDraft(draftStorage(), normalized)
        if (!mounted.current) return
        setDraftRetained(true)
        setPendingDraft(null)
        setSaved(normalized)
        replaceDraft(portfolioToDraft(normalized))
        setResult(null)
        setResultPortfolio(null)
        setNotice('持仓与策略已保存。保存不会获取行情或生成建议。')
      } else {
        const suggestions = await api.getQuantSuggestions(portfolio, mode)
        if (!mounted.current) return
        setResult(suggestions)
        setResultPortfolio(portfolio)
        setNotice('建议基于当前草稿生成，未自动保存，也不会执行交易。')
      }
    } catch (err) { if (mounted.current) setError(message(err)) }
    finally { if (mounted.current) { busyRef.current = false; setBusy(null) } }
  }

  const cash = draft.cash.trim() && Number.isFinite(Number(draft.cash)) && Number(draft.cash) >= 0 ? Number(draft.cash) : null
  const held = draft.holdings.filter(h => /^\d+$/.test(h.quantity) && Number(h.quantity) > 0).length
  const pnl = result && resultPortfolio && result.summary.market_value !== null && resultPortfolio.holdings.filter(h => h.quantity > 0).every(h => result.suggestions.some(s => s.symbol === h.symbol && s.market_value !== null))
    ? result.summary.market_value - resultPortfolio.holdings.reduce((sum, h) => sum + h.quantity * h.cost_price, 0) : null

  return <div className="flex h-full w-full min-h-0 min-w-0 max-w-full flex-col">
    <PageHeader title="量化助手" subtitle="技术规则辅助决策 · 不是自动交易" />
    <div className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto px-4 py-5 sm:px-6">
      <div className="mx-auto w-full min-w-0 max-w-7xl space-y-5">
        <section className="relative overflow-hidden rounded-card border border-accent/20 bg-accent/5 p-5 sm:p-6">
          <div className="flex items-start gap-3"><div className="rounded-card bg-accent/10 p-2.5 text-accent"><ShieldCheck className="h-6 w-6" /></div><div><h2 className="text-lg font-semibold text-foreground">你的持仓，你的决策</h2><p className="mt-1 max-w-3xl text-sm leading-relaxed text-secondary">按策略选股并加入候选后，基于技术规则与现金、仓位约束计算参考股数。可主动请求 LLM 解读；不会自动下单。价格为人民币，数量为股（不是手）。</p></div></div>
        </section>
        <section id="quant-analysis-actions" className="min-w-0 rounded-card border-2 border-accent/40 bg-surface p-4 sm:p-5" aria-label="生成分析建议">
          <h2 className="font-semibold text-foreground">生成分析建议</h2>
          <div className="mt-3 flex min-w-0 flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-end">
            <label className="block min-w-0 text-xs text-secondary sm:w-44">分析时段
              <select aria-label="分析时段" className={inputClass + ' mt-1'} value={mode} disabled={disabled} onChange={e => { if (busyRef.current) return; setMode(e.target.value as QuantMode); setActiveTrade(null); setResultHasTrades(false); setResult(null); setResultPortfolio(null); setNotice(''); setError('') }}>{modes.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}</select>
            </label>
            <button id="quant-generate-suggestions" className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn border border-blue-700 bg-blue-700 px-6 py-2 text-base font-semibold text-white shadow-sm enabled:hover:border-blue-800 enabled:hover:bg-blue-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:border-slate-600 disabled:bg-slate-600 disabled:text-white disabled:opacity-100" disabled={disabled} onClick={() => void submit('suggest')}>
              {busy === 'suggest' ? <Loader2 className="h-5 w-5 animate-spin" /> : <Sparkles className="h-5 w-5" />}
              {busy === 'suggest' ? '正在生成…' : '生成建议（' + (modes.find(m => m.value === mode)?.label || '') + '）'}
            </button>
            <button className={buttonClass + ' min-h-11'} disabled={disabled || !dirty} onClick={() => void submit('save')}>{busy === 'save' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}保存持仓与策略</button>
          </div>
          <p className="mt-3 text-xs leading-relaxed text-secondary">① 选择策略并点击「按策略选股」 → ② 加入候选，填写可用现金与真实持仓 → ③ 点击蓝色「生成建议」，在下方查看参考股数。候选的持有数量为 0，表示尚未买入。</p>
          <button className={buttonClass + ' mt-3'} disabled={disabled || !draft.holdings.length} onClick={() => void explainStrategy()}>{busy === 'explain' ? '正在计算并解读…' : '重新计算并 LLM 解读'}</button>
          <p className="mt-2 text-xs text-muted">仅点击 LLM 按钮时，将当前组合与分析结果发送给设置中配置的模型服务，可能产生费用。LLM 只解释结果，参考股数由规则计算。未配置时请先到设置页配置模型。</p>
          <p className="mt-1 text-xs text-muted">{modes.find(m => m.value === mode)?.hint}；时段不表示定时任务。</p>
          {saved && dirty && <p className="mt-4 flex items-center gap-2 text-xs text-amber-500"><AlertTriangle className="h-4 w-4 shrink-0" />有未保存修改：生成建议使用当前草稿。{draftRetained ? '草稿已保留，切换页面或刷新不会丢失；请点击保存提交组合。' : '草稿缓存不可用，请先保存再离开。'}</p>}
          {saved && !dirty && <p className="mt-4 flex items-center gap-2 text-xs text-muted"><CheckCircle2 className="h-4 w-4" />当前组合已保存。编辑任意字段或切换时段将清除旧建议。</p>}
          {error && <p role="alert" className="mt-3 rounded-btn bg-red-500/10 p-3 text-sm text-red-500">{error}</p>}
          {notice && <p role="status" className="mt-3 text-sm text-secondary">{notice}</p>}
        </section>
        {busy === 'load' && <p role="status" className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />正在读取已保存组合…</p>}
        {loadError && <div role="alert" className="rounded-card border border-amber-500/30 bg-amber-500/5 p-4"><p className="text-sm text-amber-500">读取持仓失败：{loadError}</p><p className="mt-1 text-xs text-secondary">尚未读取服务器组合，未覆盖已有数据。请重试后再编辑。</p><button className={buttonClass + ' mt-3'} disabled={busy !== null} onClick={() => setLoadAttempt(x => x + 1)}>重新读取持仓</button></div>}
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-5">
          {[
            { title: '可用现金', value: money(result ? result.summary.cash : cash), hint: result ? '本次建议快照' : '当前草稿 · 人民币' },
            { title: '总资产', value: money(result?.summary.total_assets), hint: '现金 + 参考市值' },
            { title: '持仓市值', value: money(result?.summary.market_value), hint: '缺失行情时不估算' },
            { title: '持仓浮盈亏', value: money(pnl), hint: '参考市值 − 持仓成本' },
            { title: '仓位 / 标的', value: result ? percent(result.summary.position_pct) : held + ' 个持仓', hint: draft.holdings.length + ' / 20 个持仓与候选' },
          ].map(card => <div key={card.title} className="rounded-card border border-border bg-surface p-4"><p className="text-xs text-muted">{card.title}</p><p className="mt-2 break-words font-mono text-lg font-semibold text-foreground">{card.value}</p><p className="mt-1 text-xs text-muted">{card.hint}</p></div>)}
        </div>

        <section className="rounded-card border border-border bg-surface p-4 sm:p-5" aria-labelledby="quant-holdings-title">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 id="quant-holdings-title" className="flex items-center gap-2 font-semibold text-foreground"><Wallet className="h-4 w-4 text-accent" />持仓与候选</h2><p className="mt-1 text-xs text-muted">数量为 0 可作为候选；最多 20 个唯一代码。可卖数量请按实际账户填写。</p></div><div className="flex flex-wrap gap-2"><button className={buttonClass + ' border-accent text-accent'} disabled={disabled || !dirty} onClick={() => void submit('save')}>{busy === 'save' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}保存持仓与策略</button><button className={buttonClass} disabled={disabled || draft.holdings.length >= 20} onClick={() => edit(current => ({ ...current, holdings: [...current.holdings, newHoldingDraft()] }))}><Plus className="h-4 w-4" />添加标的</button></div></div>
          {saved && <div className="mt-3 text-xs" role="status">
            {pendingDraft ? <div className="rounded-btn bg-amber-500/10 p-3 text-amber-600"><p>服务器组合已有更新，本标签页还保留另一份未保存草稿。请先选择：</p><div className="mt-2 flex flex-wrap gap-2"><button className={buttonClass} disabled={busy !== null} onClick={() => edit(() => pendingDraft)}>恢复未保存草稿</button><button className={buttonClass} disabled={busy !== null} onClick={discardDraft}>放弃草稿，使用已保存组合</button></div></div>
              : dirty ? <div className="flex flex-wrap items-center gap-2 text-amber-600"><span>{draftRetained ? '草稿已自动保留：切换页面或刷新后可恢复；点击保存才会提交组合。' : '浏览器草稿暂未保留，请先点击保存，避免离开后丢失。'}</span><button className="text-accent hover:underline" disabled={disabled} onClick={discardDraft}>放弃未保存修改</button></div>
              : <span className="text-muted">当前组合已保存。</span>}
          </div>}
          {error && <p role="alert" className="mt-3 text-xs text-red-500">{error}</p>}
          <QuantCandidateSearch disabled={disabled || draft.holdings.length >= 20} onSelect={addCandidate} />
          <label className="mt-4 block max-w-sm text-xs text-secondary">可用现金（人民币元）<input className={inputClass + ' mt-1'} inputMode="decimal" value={draft.cash} onChange={e => edit(current => ({ ...current, cash: e.target.value }))} disabled={disabled} placeholder="请输入可用现金" /></label>
          {draft.holdings.length === 0 ? <div className="mt-4 rounded-card border border-dashed border-border p-8 text-center"><Wallet className="mx-auto h-7 w-7 text-muted" /><p className="mt-3 text-sm text-secondary">尚未录入持仓或候选标的</p><p className="mt-1 text-xs text-muted">从真实账户手动填写，不会自动导入或展示示例持仓。</p></div> : <div className="mt-4 min-w-0 max-w-full overflow-x-auto"><table className="w-full min-w-[1120px] text-left text-xs"><caption className="sr-only">编辑持仓，数量单位股，价格单位人民币元</caption><thead className="text-muted"><tr>{['证券代码', '名称', '持有数量（股）', '可卖数量（股）', '成本价（元）', '买入单位（股）', '备注', '操作'].map(label => <th key={label} scope="col" className="px-2 pb-2 font-medium">{label}</th>)}</tr></thead><tbody>{draft.holdings.map((h, index) => <tr key={index} className="border-t border-border">
            <QuantInstrumentFields holding={h} index={index} disabled={disabled} inputClass={inputClass}
              onEdit={(field, value) => editHolding(index, field, value)} onSelect={(item, expected) => completeHolding(index, item, expected)} />
            {(['quantity', 'available_quantity', 'cost_price'] as const).map((field, i) => <td key={field} className="px-2 py-3 align-top"><input className={inputClass + ' min-w-[105px]'} aria-label={'第 ' + (index + 1) + ' 行' + ['持有数量（股）', '可卖数量（股）', '成本价（人民币元）'][i]} inputMode={field === 'cost_price' ? 'decimal' : 'numeric'} value={h[field]} disabled={disabled} onChange={e => editHolding(index, field, e.target.value)} /></td>)}
            <td className="px-2 py-3"><select className={inputClass + ' min-w-[100px]'} aria-label={'第 ' + (index + 1) + ' 行买入单位（股）'} value={h.lot_size} disabled={disabled} onChange={e => editHolding(index, 'lot_size', e.target.value)}><option value="100">100 股</option><option value="200">200 股</option></select></td>
            <td className="px-2 py-3"><input className={inputClass + ' min-w-[160px]'} aria-label={'第 ' + (index + 1) + ' 行备注'} value={h.note} maxLength={1000} disabled={disabled} onChange={e => editHolding(index, 'note', e.target.value)} /></td>
            <td className="px-2 py-3"><button className={buttonClass + ' text-muted hover:text-red-500'} disabled={disabled} aria-label={'删除第 ' + (index + 1) + ' 行'} onClick={() => deleteHolding(index)}><Trash2 className="h-4 w-4" /></button></td>
          </tr>)}</tbody></table></div>}
          <p className="mt-3 text-xs text-muted">代码或名称可搜索补全，完整代码/唯一完整名称自动匹配；多条结果可用 ↑↓ 和 Enter 选择。匹配后同时补齐代码与名称，草稿会自动保留在本标签页，点击保存提交组合。代码规范为 .SH / .SZ；数量必须是非负整数，不能为空，可卖数量不得超过持有数量。有持仓时成本价必须大于 0。</p>
        </section>

        <section className="rounded-card border border-border bg-surface p-4 sm:p-5" aria-labelledby="quant-strategy-title">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="quant-strategy-title" className="font-semibold text-foreground">策略与风险约束</h2>
            <button className={buttonClass} disabled={busy !== null || catalogBusy} onClick={() => void syncStrategies()}>
              <RefreshCw className={'h-4 w-4' + (catalogBusy ? ' animate-spin' : '')} />{catalogBusy ? '正在同步…' : '从选股页同步策略'}
            </button>
          </div>
          {catalogError && <p role="alert" className="mt-3 text-xs text-amber-500">策略目录读取失败：{catalogError}，请点击同步重试。</p>}
          {!!catalog?.load_errors?.length && <Warnings warnings={catalog.load_errors.map(item => '策略加载失败：' + item.file + '，请在选股页检查。')} />}
          <div className="mt-4 grid gap-5 lg:grid-cols-[minmax(220px,1fr)_2fr]">
            <div>
              <label className="block text-xs text-secondary">选股策略
                <select aria-label="选股策略" className={inputClass + ' mt-1'} value={draft.strategy.screener_strategy_id || ''} disabled={disabled || catalogBusy}
                  onChange={e => {
                    const id = e.target.value
                    if (!id || catalog?.strategies.some(p => p.id === id)) edit(current => ({ ...current, strategy: { ...current.strategy, screener_strategy_id: id } }))
                  }}>
                  <option value="">助手内置规则（自定义）</option>
                  {draft.strategy.screener_strategy_id && !catalog?.strategies.some(p => p.id === draft.strategy.screener_strategy_id) && <option value={draft.strategy.screener_strategy_id} disabled>已保存策略待核验：{draft.strategy.screener_strategy_id}</option>}
                  {(catalog?.strategies || []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </label>
              <p className="mt-2 text-xs text-muted">与<a className="text-accent hover:underline" href="/screener" target="_blank" rel="noreferrer">选股页</a>共用目录与已保存配置{catalog ? ' · ' + catalog.strategies.length + ' 个策略' : ''}。同步目录不会修改持仓。</p>
              {draft.strategy.screener_strategy_id ? <div className="mt-3">
                <p className="rounded-btn border border-accent/20 bg-accent/5 p-3 text-xs leading-relaxed text-secondary">{catalog?.strategies.find(p => p.id === draft.strategy.screener_strategy_id)?.description || '请同步目录核验此策略；不可用时不会回退到其他规则。'}</p>
                <p className="mt-2 text-xs leading-relaxed text-muted">{catalog?.disclaimer}</p>
                <p className="mt-2 text-xs leading-relaxed text-muted">基础筛选、排序及结果数量沿用选股页保存设置；下方止损、止盈、仓位与现金约束独立生效。筛选候选需要先添加到本页组合，不会自动成为持仓。</p>
              </div> : <div>
                <label className="mt-3 block text-xs text-secondary">规则方向<select className={inputClass + ' mt-1'} value={draft.strategy.kind} disabled={disabled} onChange={e => edit(current => ({ ...current, strategy: { ...current.strategy, kind: e.target.value } }))}><option value="trend">趋势跟随</option><option value="mean_reversion">均值回归</option></select></label>
                <p className="mt-3 text-xs leading-relaxed text-muted">{draft.strategy.kind === 'trend' ? '价格高于 MA20、MA20 高于 MA60 且 RSI14 在 45–65 时参考买入；弱趋势时检查退出。' : 'RSI14 低于 30 且价格低于 MA20 时参考买入；RSI14 高于 70 时检查减仓。'}</p>
              </div>}
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{strategyFields.map(field => <label key={field.key} className="text-xs text-secondary">{field.label}（%）<input className={inputClass + ' mt-1'} inputMode="decimal" value={draft.strategy[field.key]} disabled={disabled} onChange={e => edit(current => ({ ...current, strategy: { ...current.strategy, [field.key]: e.target.value } }))} /><span className="mt-1 block text-[11px] text-muted">{field.min}–{field.key === 'target_position_pct' ? '仓位上限' : field.max}% · {field.hint}</span></label>)}</div>
          </div>
        </section>



        <section className="rounded-card border border-border bg-surface p-4 sm:p-5" aria-label="策略候选">
          <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold text-foreground">第一步：按策略选股</h2><button className={buttonClass} disabled={disabled || !draft.strategy.screener_strategy_id} onClick={() => void screenStrategy()}>{busy === 'screen' ? '正在选股…' : '按策略选股'}</button></div>
          <p className="mt-2 text-xs text-secondary">使用所选策略和选股页保存的配置，按上一已完成工作日日线筛选。这里展示前 20 个候选，不会自动买入。环境情绪多头子模型筛选 A 股，纳指 ETF 不属于该模型的选股范围。</p>
          {candidates && candidates.strategy_id === draft.strategy.screener_strategy_id && <div className="mt-3">
            <p className="text-sm">{candidates.name} · 数据日期 {candidates.as_of} · 筛选结果 {candidates.total} 个</p>
            {!candidates.candidates.length && <p className="mt-2 text-sm text-amber-600">当前没有满足条件的候选，保持观察；不会降低条件强行选股。</p>}
            <div className="mt-2 space-y-2">{candidates.candidates.map((item, index) => {
              const included = draft.holdings.some(h => h.symbol === item.symbol)
              return <div key={item.symbol} className="flex flex-wrap items-center justify-between gap-2 rounded-btn bg-base p-3"><span className="text-sm">{index + 1}. {item.name || item.symbol} <span className="font-mono text-xs text-secondary">{item.symbol}</span>{item.score !== null && <span className="ml-2 text-xs">策略分值 {item.score.toFixed(2)}</span>}</span><button className={buttonClass} disabled={disabled || included || draft.holdings.length >= 20} onClick={() => addCandidate(item)}>{included ? '已加入组合' : '加入候选'}</button></div>
            })}</div>
          </div>}
          <p className="mt-3 text-xs text-secondary">第二步：加入候选后点击「生成建议」。股数按目标仓位、已有持仓、共享现金预算及买入单位向下取整；现金不足、行情缺失或条件未命中时可能为 0 股。候选列表不代表可成交订单。</p>
        </section>

        <ContextResearchCard />
        {explanation && explanation.result === result && !resultHasTrades && <section className="rounded-card border border-border bg-surface p-4 sm:p-5" aria-label="LLM 解读"><h2 className="font-semibold">LLM 解读 · {explanation.model}</h2><p className="mt-1 text-xs text-amber-600">模型解读可能有误，请以决策参考中的规则计算、数据日期和参考股数为准。</p><div className="mt-3 whitespace-pre-wrap break-words text-sm leading-relaxed">{explanation.text}</div></section>}

        <section className="overflow-hidden rounded-card border border-border bg-surface" aria-labelledby="quant-results-title"><div className="border-b border-border p-4 sm:p-5"><div className="flex flex-wrap items-center justify-between gap-2"><h2 id="quant-results-title" className="font-semibold text-foreground">决策参考</h2>{result && <div className="flex flex-wrap items-center gap-3"><button className={buttonClass} onClick={downloadReport}><Download className="h-4 w-4" />导出参考报告</button><span className="text-xs text-muted">{modes.find(m => m.value === result.mode)?.label || result.mode} · 生成时间：{result.generated_at}</span></div>}</div>{result && <div className="mt-3 space-y-3"><p className="text-xs text-secondary">买入预算：<span className="font-mono text-foreground">{money(result.summary.buy_budget)}</span> · 数量均为股，参考价不保证可成交；早盘可能参考上一交易日收盘，免费分钟数据可能延迟或不完整。</p>{totals && <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="建议汇总">
              {[{ label: '买入参考', value: totals.buyCount + ' 个 · ' + money(totals.buyAmount) },
                { label: '卖出 / 减仓参考', value: totals.sellCount + ' 个 · ' + money(totals.sellAmount) },
                { label: '持有 / 观察', value: totals.holdCount + ' 个' },
                { label: '暂不可操作', value: totals.blockedCount + ' 个' }].map(card => <div key={card.label} className="rounded-btn bg-base p-3"><p className="text-xs text-muted">{card.label}</p><p className="mt-1 font-mono text-sm text-foreground">{card.value}</p></div>)}
            </div>}
            <p className="text-xs text-muted">汇总金额为参考数量 × 参考价，未含费用；卖出收入不增加本批买入预算。导出文件包含本次持仓、策略和数据快照。</p>
            <Warnings warnings={result.warnings} /></div>}</div>
          {result && dirty && <p className="mx-4 mt-3 text-xs text-amber-600">记录成交前，请先保存当前组合并重新生成建议。</p>}
          {resultHasTrades && <p role="status" className="mx-4 mt-3 rounded-btn bg-accent/10 p-3 text-xs text-accent">已按实际成交更新持仓与现金。下表及资产汇总保留成交前建议快照，请重新生成建议查看最新仓位。</p>}
          {activeTrade && saved && <QuantTradeForm key={activeTrade.key} item={activeTrade.item} suggestionKey={activeTrade.key} portfolio={saved} onSubmit={recordCompletedTrade} onClose={() => setActiveTrade(null)} />}
          {!result ? <div className="p-10 text-center"><Sparkles className="mx-auto h-7 w-7 text-accent/60" /><p className="mt-3 text-sm text-secondary">等待你主动生成建议</p><p className="mt-1 text-xs text-muted">录入持仓或候选、调整约束，再选择时段。没有后台自动分析。</p></div> : result.suggestions.length === 0 ? <p className="p-8 text-center text-sm text-muted">本次没有标的建议。可添加持仓或数量为 0 的候选后重新生成。</p> : <div className="min-w-0 max-w-full overflow-x-auto"><table className="w-full text-left text-sm text-secondary"><caption className="sr-only">仅供参考的量化建议，不会自动执行</caption><thead className="bg-elevated text-xs text-muted"><tr>{['标的', '动作', '参考数量（股）', '参考价（元）', '操作记录', '持仓市值（元）', '仓位', '盈亏', '依据 / 风险提示', '数据来源 / 时间 / 周期'].map(label => <th key={label} scope="col" className="whitespace-nowrap p-3 font-medium">{label}</th>)}</tr></thead><tbody>{result.suggestions.map(item => <SuggestionRow key={item.symbol} item={item} completed={trades.some(t => t.suggestion_key === suggestionKey(item))} disabled={disabled || dirty} onRecord={() => setActiveTrade({ item, key: suggestionKey(item) })} />)}</tbody></table></div>}
          <p className="border-t border-border px-4 py-3 text-xs leading-relaxed text-muted">不构成投资建议。请自行核对停牌、涨跌停、T+1 可卖数量及实际资金；本页不接入交易执行。</p>
        </section>
        <section className="min-w-0 rounded-card border border-border bg-surface p-4">
          <h2 className="font-semibold">成交记录</h2>
          <p className="mt-1 text-xs text-muted">展示最近10条已确认的本地记账记录，不代表券商成交回执。</p>
          {tradesError && <p role="alert" className="mt-2 text-xs text-amber-600">成交记录读取失败：{tradesError}，请刷新重试。</p>}
          {trades.length === 0 ? <p className="mt-3 text-sm text-muted">暂无已记录成交</p> : <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr>{['成交时间', '标的', '方向', '实际数量', '实际均价', '全部费用', '成交后现金'].map(label => <th key={label} className="whitespace-nowrap p-2">{label}</th>)}</tr></thead><tbody>{trades.slice(0, 10).map(trade => <tr key={trade.client_id} className="border-t border-border"><td className="whitespace-nowrap p-2">{new Date(trade.executed_at).toLocaleString('zh-CN')}</td><td className="whitespace-nowrap p-2">{trade.name} {trade.symbol}</td><td className="p-2">{trade.side === 'buy' ? '买入' : '卖出'}</td><td className="p-2">{trade.quantity} 股</td><td className="p-2">{quantReferencePrice(trade.price)}</td><td className="p-2">{money(trade.fees)}</td><td className="p-2">{money(trade.cash_after)}</td></tr>)}</tbody></table></div>}
        </section>
      </div>
    </div>
  </div>
}
