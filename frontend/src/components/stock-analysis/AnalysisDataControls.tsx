import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api, type StockAnalysisMode, type StockAnalysisDataContext } from '@/lib/api'

interface Props {
  symbol: string
  mode: StockAnalysisMode
  includeNews: boolean
  onModeChange: (mode: StockAnalysisMode) => void
  onIncludeNewsChange: (value: boolean) => void
  disabled?: boolean
}

export function AnalysisDataControls({ symbol, mode, includeNews, onModeChange, onIncludeNewsChange, disabled }: Props) {
  return (
    <div className="space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-secondary">
          数据模式
          <select aria-label="分析数据模式" value={mode} disabled={disabled}
            onChange={e => onModeChange(e.target.value as StockAnalysisMode)}
            className="h-8 rounded-btn border border-border bg-base px-2 text-foreground disabled:opacity-50">
            <option value="existing">现有日 K（默认）</option>
            <option value="morning">早间 · 最近完整交易日</option>
            <option value="noon">午间 · 当日上午</option>
            <option value="tail">尾盘 · 当日截至 14:50（未收盘）</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-secondary">
          <input type="checkbox" checked={includeNews} disabled={disabled}
            onChange={e => onIncludeNewsChange(e.target.checked)} className="accent-sky-400" />
          新闻辅助（可选，默认关闭）
        </label>
      </div>
      <p className="text-[10px] text-muted">模式仅影响分析输入；页面图表与关键价位仍为日 K。所有模式均可开启新闻辅助；默认日 K 模式使用本次获取的新闻。</p>
      <ContextPreview key={JSON.stringify([symbol, mode, includeNews])} symbol={symbol} mode={mode} includeNews={includeNews} disabled={disabled} />
    </div>
  )
}

/** 每个标的/模式/新闻组合独立挂载；清空旧预览并取消请求，不复用旧响应。 */
function ContextPreview({ symbol, mode, includeNews, disabled }: Pick<Props, 'symbol' | 'mode' | 'includeNews' | 'disabled'>) {
  const [context, setContext] = useState<StockAnalysisDataContext | null>(null)
  const [loading, setLoading] = useState(false)
  const requestRef = useRef<AbortController | null>(null)
  useEffect(() => () => { requestRef.current?.abort() }, [])

  const check = async () => {
    if (!symbol || loading) return
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setContext(null)
    setLoading(true)
    try {
      const data = await api.stockAnalysisContext(symbol, mode, includeNews, controller.signal)
      if (!controller.signal.aborted && requestRef.current === controller) setContext(data)
    } catch (error) {
      if (!controller.signal.aborted && requestRef.current === controller) {
        setContext({ error: error instanceof Error ? error.message : '数据检查失败' })
      }
    } finally {
      if (!controller.signal.aborted && requestRef.current === controller) setLoading(false)
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <button type="button" onClick={check} disabled={disabled || !symbol || loading}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border px-3 py-1.5 text-secondary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40">
          {loading && <Loader2 className="h-3 w-3 animate-spin" />}检查数据
        </button>
        <span className="text-[10px] text-muted">仅检查数据，不调用 AI；新闻按上方开关获取。</span>
      </div>
      {context && (
        <div className="rounded-lg border border-border/50 bg-base/40 p-3">
          <AnalysisDataContext context={context} />
          <details className="mt-2 text-[10px] text-muted">
            <summary className="cursor-pointer">查看预览 JSON</summary>
            <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(context, null, 2)}</pre>
          </details>
        </div>
      )}
    </div>
  )
}

function display(value: unknown): string {
  if (value === undefined) return '未返回'
  if (typeof value === 'string') return value || '（空）'
  return JSON.stringify(value)
}

export function AnalysisDataContext({ context }: { context: StockAnalysisDataContext }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[10px] leading-relaxed">
      {([
        ['as_of', '行情截止'], ['fetched_at', '本次获取'], ['source', '数据来源'], ['period', '数据周期'], ['adjust', '复权口径'], ['warnings', '警告'], ['error', '错误'],
      ] as const).map(([key, label]) => (
        <div key={key} className="contents">
          <dt className="text-muted">{label} / {key}</dt>
          <dd className={key === 'error' && context.error ? 'break-all text-danger' : 'break-all text-secondary'}>{display(context[key])}</dd>
        </div>
      ))}
    </dl>
  )
}
