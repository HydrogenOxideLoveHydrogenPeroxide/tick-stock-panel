import { useEffect, useState } from 'react'
import { api, type ContextResearchStatus } from '@/lib/api'
import { FundamentalSyncCard } from '@/components/FundamentalSyncCard'

export function ContextResearchCard() {
  const [data, setData] = useState<ContextResearchStatus | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    api.getContextResearchStatus().then(value => { if (active) { setData(value); setError('') } })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : '报告读取失败') })
    return () => { active = false }
  }, [attempt])
  return <section className="min-w-0 rounded-card border border-border bg-surface p-4 sm:p-5">
    <div className="flex flex-wrap items-center gap-3"><h2 className="font-semibold">综合策略与市场估值</h2><span className="rounded-btn bg-amber-500/10 px-2 py-1 text-xs text-amber-600">研究候选 · 完整策略数据未齐</span></div>
    <p className="mt-2 text-xs leading-relaxed text-secondary">选股与回测中可选择「环境情绪多头（研究子模型）」。仅包含日线环境、广度情绪和技术趋势，完整版本需要竞价、题材及历史估值输入。</p>
    {error && <div role="alert" className="mt-3 text-xs text-amber-600">{error}<button className="ml-3 text-accent hover:underline" onClick={() => setAttempt(x => x + 1)}>重新读取研究状态</button></div>}
    {data && <>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{data.components.map(item => <div key={item.name} className="rounded-btn bg-base p-3"><p className="text-xs font-medium">{item.name}</p><p className="mt-1 text-xs text-muted">{item.status}</p></div>)}</div>
      {data.buffett && <div className="mt-3 rounded-btn border border-border bg-base p-3">
        <p className="text-sm font-semibold">市场整体估值（巴菲特指标）：{data.buffett.ratio_pct.toFixed(2)}% <span className="text-xs font-normal text-muted">（{data.buffett.as_of} 年度参考，非实时）</span></p>
        <p className="mt-2 text-xs text-secondary">境内上市公司已发行股份总市值 {data.buffett.market_cap_trillion} 万亿元 ÷ 年度名义 GDP {data.buffett.nominal_gdp_trillion} 万亿元 × 100</p>
        <p className="mt-2 text-xs leading-relaxed text-secondary">适用范围：境内股票市场整体。该比值不代表当前选中公司的估值，也不代表某只 ETF 的估值或折溢价。</p>
        <p className="mt-1 text-xs leading-relaxed text-muted">{data.buffett.note}</p>
        <p className="mt-2 flex flex-wrap gap-3 text-xs"><a className="text-accent hover:underline" href={data.buffett.market_cap_source} target="_blank" rel="noreferrer">中上协市值口径</a><a className="text-accent hover:underline" href={data.buffett.gdp_source} target="_blank" rel="noreferrer">国家统计局 GDP</a></p>
      </div>}
      <div className="mt-3 rounded-btn border border-border p-3" aria-label="个股价值投资估值说明">
        <h3 className="text-sm font-semibold">个股价值投资估值（与市场整体估值分开）</h3>
        <p className="mt-2 text-xs leading-relaxed text-secondary">单家公司需要分析盈利质量、资本回报、负债及可分配现金流，再在明确的增长、折现率和再投资假设下估算内在价值与安全边际。不能用“单家公司市值 ÷ GDP”代替。</p>
        <p className="mt-1 text-xs leading-relaxed text-muted">个股质量—价值因子独立于日线子模型，在回测工作台的「因子回测」中选择“个股价值（研究）”因子。不会将市场比值当作个股估值。ETF 应另行分析基金净值、折溢价及底层资产。</p>
        {data.value_factors && <div className="mt-3 space-y-2">
          <p className="text-xs font-medium">{data.value_factors.name}</p>
          <p className="text-xs text-amber-600">{data.value_factors.note}</p>
          <div className="grid gap-2 sm:grid-cols-2">{data.value_factors.factors.map(factor => <div key={factor.id} className="rounded-btn bg-base p-2 text-xs"><p className="font-medium">{factor.label}</p><p className="mt-1 text-secondary">{factor.formula}</p></div>)}</div>
          <p className="text-xs text-muted">财报版本 {data.value_factors.statement_versions} 条 · 市值版本 {data.value_factors.market_versions} 条。仅在决策时点数据完整、行业内有效样本至少5家时生成综合分。</p>
          {!!data.value_factors.strategy_variants?.length && <div className="rounded-btn border border-border p-2 text-xs"><p className="font-medium">已接入策略的研究版本</p><ul className="mt-1 space-y-1">{data.value_factors.strategy_variants.map(variant => <li key={variant.strategy_id}>{variant.name}</li>)}</ul><p className="mt-2 text-secondary">默认原策略分80%＋个股价值分20%，缺价值数据不回退原策略。原版保留作对照，尚未证明收益改善。</p></div>}
          {data.value_integration && <p className="text-xs leading-relaxed text-amber-600">{data.value_integration.code_current ? `链路检查 ${data.value_integration.total_tests} 项通过` : '代码已更新，旧校验报告需重新运行'}；真实收益未验证，{data.value_integration.real_historical_windows_blocked} 个年度策略窗口因数据缺失被阻断，并非零收益回测。</p>}
          <details><summary className="cursor-pointer text-xs text-accent">因子口径与限制</summary><ul className="mt-2 space-y-1 text-xs text-muted">{data.value_factors.limitations.map(text => <li key={text}>{text}</li>)}</ul></details>
        </div>}
        <a className="mt-2 inline-block text-xs text-accent hover:underline" href="https://www.berkshirehathaway.com/owners.html" target="_blank" rel="noreferrer">巴菲特关于企业内在价值的说明</a>
      </div>
      <FundamentalSyncCard onUpdated={() => setAttempt(x => x + 1)} />
      {!!data.annual_results?.length && <details className="mt-3"><summary className="cursor-pointer text-xs text-accent">查看日线子模型年度检验（{data.scenarios} 个场景，{data.core_gate_passed ? '通过统计门槛，仍待数据验证' : '未通过统计门槛'}）</summary><div className="mt-2 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr>{['年份', '10bp 滑点收益', '年度内最大回撤', '成交数'].map(label => <th className="p-2" key={label}>{label}</th>)}</tr></thead><tbody>{data.annual_results.map(row => <tr key={row.year} className="border-t border-border"><td className="p-2">{row.year}</td><td className="p-2">{row.trades ? (row.total_return * 100).toFixed(2) + '%' : '空仓，无成交'}</td><td className="p-2">{(row.max_drawdown * 100).toFixed(2)}%</td><td className="p-2">{row.trades}</td></tr>)}</tbody></table></div></details>}
    </>}
  </section>
}
