import type { QuantHolding, QuantPortfolio, QuantStrategy, QuantSuggestions } from './api'

export const DEFAULT_QUANT_PORTFOLIO: QuantPortfolio = {
  cash: 0, holdings: [], strategy: {
    kind: 'trend', screener_strategy_id: '', stop_loss_pct: 8, take_profit_pct: 20,
    max_position_pct: 25, target_position_pct: 15, cash_reserve_pct: 10,
  },
}
export type HoldingDraft = { [K in keyof QuantHolding]: string }
export type StrategyDraft = { [K in keyof QuantStrategy]: string }
export interface PortfolioDraft { cash: string; holdings: HoldingDraft[]; strategy: StrategyDraft }
export function portfolioToDraft(portfolio: QuantPortfolio): PortfolioDraft {
  return {
    cash: String(portfolio.cash),
    holdings: portfolio.holdings.map(h => ({
      symbol: h.symbol, name: h.name, quantity: String(h.quantity),
      available_quantity: String(h.available_quantity), cost_price: String(h.cost_price),
      lot_size: String(h.lot_size), note: h.note,
    })),
    strategy: {
      kind: portfolio.strategy.kind, screener_strategy_id: portfolio.strategy.screener_strategy_id || '', stop_loss_pct: String(portfolio.strategy.stop_loss_pct),
      take_profit_pct: String(portfolio.strategy.take_profit_pct),
      max_position_pct: String(portfolio.strategy.max_position_pct),
      target_position_pct: String(portfolio.strategy.target_position_pct),
      cash_reserve_pct: String(portfolio.strategy.cash_reserve_pct),
    },
  }
}
export function newHoldingDraft(): HoldingDraft {
  return { symbol: '', name: '', quantity: '0', available_quantity: '0', cost_price: '0', lot_size: '100', note: '' }
}
export type QuantInstrument = { symbol: string; name: string }
export function editQuantIdentity(holding: HoldingDraft, field: 'symbol' | 'name', value: string): HoldingDraft {
  if (field === 'name') return { ...holding, name: value }
  let same = holding.symbol.trim().toUpperCase() === value.trim().toUpperCase()
  try { same = normalizeQuantSymbol(holding.symbol) === normalizeQuantSymbol(value) } catch { /* partial input */ }
  return { ...holding, symbol: value, name: same ? holding.name : '' }
}
/** Only an exact unique identity may be filled without choosing a search result. */
export function exactQuantInstrument(query: string, items: QuantInstrument[]): QuantInstrument | null {
  const text = query.trim().toUpperCase()
  if (!text) return null
  let symbol = ''
  try { symbol = normalizeQuantSymbol(text) } catch { /* name or incomplete code */ }
  const matches = items.filter(item => symbol ? item.symbol.toUpperCase() === symbol : item.name.trim().toUpperCase() === text)
  return matches.length === 1 ? matches[0] : null
}
export function completeQuantHolding(draft: PortfolioDraft, index: number, item: QuantInstrument, expected: Pick<HoldingDraft, 'symbol' | 'name'>): PortfolioDraft {
  const current = draft.holdings[index]
  if (!current || current.symbol !== expected.symbol || current.name !== expected.name) throw new Error('输入已变化，请重新选择标的')
  const symbol = normalizeQuantSymbol(item.symbol)
  if (draft.holdings.some((h, i) => {
    if (i === index) return false
    try { return normalizeQuantSymbol(h.symbol) === symbol } catch { return false }
  })) throw new Error('该标的已在组合中：' + symbol)
  return { ...draft, holdings: draft.holdings.map((h, i) => i === index ? {
    ...h, symbol, name: item.name,
  } : h) }
}
/** Add search results without replacing any existing position or its cost. */
export function addQuantCandidate(draft: PortfolioDraft, instrument: { symbol: string; name: string }): PortfolioDraft {
  const symbol = normalizeQuantSymbol(instrument.symbol)
  if (draft.holdings.some(h => {
    try { return normalizeQuantSymbol(h.symbol) === symbol } catch { return false }
  })) throw new Error('该标的已在组合中：' + symbol)
  if (draft.holdings.length >= 20) throw new Error('最多 20 个持仓或候选标的')
  return { ...draft, holdings: [...draft.holdings, {
    ...newHoldingDraft(), symbol, name: instrument.name, lot_size: symbol.startsWith('688') ? '200' : '100',
  }] }
}

export function quantSuggestionTotals(result: QuantSuggestions) {
  const totals = { buyCount: 0, sellCount: 0, holdCount: 0, blockedCount: 0, buyAmount: 0, sellAmount: 0 }
  for (const item of result.suggestions) {
    if (item.action === 'blocked') { totals.blockedCount++; continue }
    if (item.action === 'hold') { totals.holdCount++; continue }
    if (item.price == null || !Number.isFinite(item.price) || item.price <= 0 || !Number.isSafeInteger(item.quantity) || item.quantity <= 0) continue
    if (item.action === 'buy') { totals.buyCount++; totals.buyAmount += item.quantity * item.price }
    else if (item.action === 'sell' || item.action === 'reduce') { totals.sellCount++; totals.sellAmount += item.quantity * item.price }
  }
  return totals
}

/** A local review artifact includes the exact input snapshot, never current edits. */
export function quantReportJson(portfolio: QuantPortfolio, result: QuantSuggestions): string {
  return JSON.stringify({ schema_version: 1, report_type: 'quant-assistant-reference',
    disclaimer: '技术规则参考报告，金额按参考价计算，未含费用，不是成交记录或执行订单。',
    portfolio, result,
  }, null, 2)
}
export function normalizeQuantSymbol(value: string): string {
  const symbol = value.trim().toUpperCase()
  const match = /^([0123569]\d{5})(?:\.(SH|SZ))?$/.exec(symbol)
  if (!match) throw new Error('代码须为六位沪深代码或六位代码.SH / .SZ（首位仅支持 0、1、2、3、5、6、9；不支持北交所）')
  const exchange = /^[569]/.test(match[1]) ? 'SH' : 'SZ'
  if (match[2] && match[2] !== exchange) throw new Error('证券代码与交易所后缀不匹配：' + match[1] + ' 应为 .' + exchange)
  return match[1] + '.' + exchange
}
function numeric(value: string, label: string, min: number, max: number, integer = false): number {
  const text = value.trim()
  if (!text || !(integer ? /^\d+$/ : /^(?:\d+(?:\.\d*)?|\.\d+)$/).test(text)) {
    throw new Error(label + (integer ? '须填写非负整数，单位为股' : '须填写有效数字，不能留空'))
  }
  const number = Number(text)
  if (!Number.isFinite(number) || number < min || number > max || (integer && !Number.isSafeInteger(number))) {
    throw new Error(label + '须在 ' + min + ' 至 ' + max + ' 之间' + (integer ? '且为整数' : ''))
  }
  return number
}
/** String inputs are never silently coerced: empty and fractional quantities are errors. */
export function validateQuantDraft(draft: PortfolioDraft): QuantPortfolio {
  if (draft.holdings.length > 20) throw new Error('最多 20 个持仓或候选标的')
  const cash = numeric(draft.cash, '可用现金', 0, 1e12)
  const symbols = new Set<string>()
  const holdings: QuantHolding[] = draft.holdings.map((h, index) => {
    const label = '第 ' + (index + 1) + ' 行：'
    let symbol: string
    try { symbol = normalizeQuantSymbol(h.symbol) } catch (error) { throw new Error(label + (error as Error).message) }
    if (symbols.has(symbol)) throw new Error(label + '代码重复：' + symbol)
    symbols.add(symbol)
    const quantity = numeric(h.quantity, label + '持有数量', 0, 1e9, true)
    const available_quantity = numeric(h.available_quantity, label + '可卖数量', 0, quantity, true)
    const cost_price = numeric(h.cost_price, label + '成本价', 0, 1e6)
    if (quantity > 0 && cost_price <= 0) throw new Error(label + '持有数量大于零时，成本价必须大于零')
    if (h.lot_size !== '100' && h.lot_size !== '200') throw new Error(label + '买入单位仅支持 100 或 200 股')
    if (h.name.length > 100 || h.note.length > 1000) throw new Error(label + '名称最多 100 字，备注最多 1000 字')
    return { symbol, name: h.name.trim(), quantity, available_quantity, cost_price, lot_size: h.lot_size === '200' ? 200 : 100, note: h.note.trim() }
  })
  const s = draft.strategy
  if (s.kind !== 'trend' && s.kind !== 'mean_reversion') throw new Error('请选择有效策略')
  // Strategy IDs come from the shared Screener directory; the server verifies availability.
  const screener_strategy_id = (s.screener_strategy_id || '').trim()
  if (screener_strategy_id && !/^[A-Za-z0-9_-]{1,128}$/.test(screener_strategy_id)) throw new Error('选股策略 ID 无效，请重新同步策略')
  const max_position_pct = numeric(s.max_position_pct, '单标的仓位上限', 1, 100)
  const strategy: QuantStrategy = {
    kind: s.kind, screener_strategy_id, stop_loss_pct: numeric(s.stop_loss_pct, '止损比例', 1, 50),
    take_profit_pct: numeric(s.take_profit_pct, '止盈比例', 1, 200), max_position_pct,
    target_position_pct: numeric(s.target_position_pct, '目标仓位', 1, max_position_pct),
    cash_reserve_pct: numeric(s.cash_reserve_pct, '现金保留比例', 0, 100),
  }
  return { cash, holdings, strategy }
}
export function quantDraftDirty(draft: PortfolioDraft, saved: QuantPortfolio | null): boolean {
  if (!saved) return true
  try { return JSON.stringify(validateQuantDraft(draft)) !== JSON.stringify(saved) } catch { return true }
}
/** Reference prices preserve fund precision; market-value currency remains two decimals. */
export function quantReferencePrice(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : '¥' + value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 6 })
}
export function quantContextValue(context: Record<string, unknown> | null, key: string): string {
  const value = context?.[key]
  if (value === undefined || value === null || value === '') return '未提供'
  return typeof value === 'string' ? value : JSON.stringify(value)
}
