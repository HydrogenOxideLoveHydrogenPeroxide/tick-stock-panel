import { useEffect, useRef, useState } from 'react'
import { api, type FundamentalSync } from '@/lib/api'

export function FundamentalSyncCard({ onUpdated }: { onUpdated: () => void }) {
  const [data, setData] = useState<FundamentalSync | null>(null)
  const [symbols, setSymbols] = useState('')
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const updated = useRef(onUpdated)
  updated.current = onUpdated
  const busy = starting || data?.status === 'running'
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const next = await api.getFundamentalSync()
        if (!active) return
        setData(next); setError('')
        if (next.status === 'running') timer = setTimeout(refresh, 3000)
        else updated.current()
      } catch (err) { if (active) setError(err instanceof Error ? err.message : '读取采集进度失败') }
    }
    void refresh()
    return () => { active = false; clearTimeout(timer) }
  }, [attempt])

  async function start() {
    if (busy) return
    const codes = symbols.split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean)
    if (!codes.length || codes.length > 20) { setError('每批填写1～20个沪深A股代码，以逗号或空格分隔'); return }
    setStarting(true); setError('')
    try { await api.startFundamentalSync(codes); setAttempt(x => x + 1) }
    catch (err) { setError(err instanceof Error ? err.message : '启动采集失败') }
    finally { setStarting(false) }
  }

  return <div id="quant-fundamental-sync" className="mt-3 rounded-btn border border-border p-3 text-xs" aria-label="财报与市值采集">
    <h3 className="font-semibold">公开源财报与市值采集</h3>
    <p className="mt-2 leading-relaxed text-secondary">复用公开财报三表，补充腾讯总市值与东方财富行业。每批最多20只，可追加、重试；相同内容保留首次采集时间。</p>
    <div className="mt-3 flex flex-wrap gap-2"><input aria-label="采集A股代码" className="min-w-0 flex-1 rounded-btn border border-border bg-base px-3 py-2 text-foreground" placeholder="输入A股代码，如600519、000858；逗号分隔" maxLength={500} value={symbols} disabled={busy} onChange={e => setSymbols(e.target.value)} /><button className="rounded-btn bg-blue-700 px-3 py-2 font-medium text-white disabled:bg-slate-600 disabled:cursor-not-allowed" disabled={busy} onClick={() => void start()}>{busy ? '正在采集…' : '抓取财报与市值'}</button><button className="rounded-btn border border-border px-3 py-2" disabled={busy} onClick={() => setAttempt(x => x + 1)}>刷新状态</button></div>
    {error && <p role="alert" className="mt-2 text-amber-600">{error}</p>}
    {data && <div className="mt-3 space-y-2"><p>采集状态：{({ idle: '尚未采集', running: '采集中', completed: '本批完成', partial: '部分完成', failed: '失败', interrupted: '已中断' } as Record<string, string>)[data.status] || data.status} {data.total != null && ` · ${data.completed || 0}/${data.total}`}</p>{data.message && <p className="text-secondary">{data.message}</p>}{data.current_scored_symbols != null && <p>当前有效行业评分样本：{data.current_scored_symbols} 只，仅代表已采集子样本。</p>}{!!data.results?.length && <details><summary className="cursor-pointer text-accent">查看每只标的的采集结果</summary><ul className="mt-2 space-y-1">{data.results.map(row => <li key={row.symbol}>{row.name || row.symbol} · {row.status === 'collected' ? `${row.annual_periods}个年度 · ${row.industry}` : `失败：${row.error}`}</li>)}</ul></details>}</div>}
    {!!data?.notes?.length && <details className="mt-3"><summary className="cursor-pointer text-accent">来源与口径限制</summary><ul className="mt-2 space-y-1 text-muted">{data.notes.map(note => <li key={note}>{note}</li>)}</ul></details>}
    <p className="mt-3 leading-relaxed text-amber-600">本任务抓取当前公开版本，不能自动补出过去的原始财报版本与历史总市值。当前快照可做当前研究，历史回测仍按真实可用时间校验；不会将今天抓取的数据倒填到2018～2025年。</p>
  </div>
}
