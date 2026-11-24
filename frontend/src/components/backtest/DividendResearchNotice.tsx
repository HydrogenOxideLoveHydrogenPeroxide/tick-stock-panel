interface Props {
  warning?: string
  orders?: { date: string; symbol: string; side: string; shares: number; fill_price: number; fees: { total: number } }[]
}

export function DividendResearchNotice({ warning, orders = [] }: Props) {
  if (!warning) return null
  return <section className="space-y-3 rounded-card border border-warning/30 bg-warning/[0.06] p-4 text-xs">
    <p className="font-medium text-warning">历史重建研究 · 数据版本限制</p>
    <p className="leading-relaxed text-secondary">{warning}</p>
    <a className="text-accent underline" href="/api/financials/dividend-backtest/files/index.html" target="_blank" rel="noreferrer">查看固定10万元研究报告与成本对照（与本次自定义参数结果分开）</a>
    <details>
      <summary className="cursor-pointer py-1 text-secondary">本次成交委托 {orders.length} 笔（预览前100笔；不等同平仓交易胜率）</summary>
      <div className="max-h-80 overflow-auto"><table className="w-full text-left text-[11px]"><thead><tr><th>日期</th><th>股票</th><th>方向</th><th>股数</th><th>模拟成交价</th><th>税费/元</th></tr></thead><tbody>
        {orders.slice(0, 100).map((order, index) => <tr key={index} className="border-t border-border/50"><td>{order.date}</td><td>{order.symbol}</td><td>{order.side === 'buy' ? '买入' : '卖出'}</td><td>{order.shares}</td><td>{order.fill_price.toFixed(4)}</td><td>{order.fees.total.toFixed(2)}</td></tr>)}
      </tbody></table></div>
    </details>
  </section>
}
