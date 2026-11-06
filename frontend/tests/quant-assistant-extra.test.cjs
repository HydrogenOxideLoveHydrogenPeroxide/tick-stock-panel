const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const output = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, '../src/lib/quant-assistant.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const moduleMock = { exports: {} }
vm.runInNewContext('(function(module,exports){' + output + '\n})', { Error })(moduleMock, moduleMock.exports)
const { validateQuantDraft, portfolioToDraft, DEFAULT_QUANT_PORTFOLIO, newHoldingDraft, quantContextValue } = moduleMock.exports

test('all 20 zero-share candidates are allowed with exact keys and default strategy', () => {
  const draft = portfolioToDraft(DEFAULT_QUANT_PORTFOLIO)
  draft.holdings = Array.from({ length: 20 }, (_, i) => ({ ...newHoldingDraft(), symbol: String(600000 + i) }))
  const portfolio = validateQuantDraft(draft)
  assert.equal(portfolio.holdings.length, 20)
  assert.equal(Object.keys(portfolio).sort().join(), 'cash,holdings,strategy')
  assert.equal(Object.keys(portfolio.holdings[0]).sort().join(), 'available_quantity,cost_price,lot_size,name,note,quantity,symbol')
  assert.equal(Object.keys(portfolio.strategy).sort().join(), 'cash_reserve_pct,kind,max_position_pct,screener_strategy_id,stop_loss_pct,take_profit_pct,target_position_pct')
})

test('complete numeric upper bounds and mean reversion accepted without scaling shares or RMB', () => {
  const draft = portfolioToDraft(DEFAULT_QUANT_PORTFOLIO)
  draft.cash = '1000000000000'
  draft.holdings = [{ ...newHoldingDraft(), symbol: '000001', quantity: '1000000000', available_quantity: '1000000000', cost_price: '1000000' }]
  draft.strategy = { kind: 'mean_reversion', stop_loss_pct: '50', take_profit_pct: '200', max_position_pct: '100', target_position_pct: '100', cash_reserve_pct: '100' }
  const portfolio = validateQuantDraft(draft)
  assert.equal(portfolio.cash, 1e12)
  assert.equal(portfolio.holdings[0].quantity, 1e9)
  assert.equal(portfolio.holdings[0].cost_price, 1e6)
  assert.equal(portfolio.strategy.kind, 'mean_reversion')
})

test('unsupported symbols, invalid strategy and overlong texts are rejected', () => {
  for (const symbol of ['', '60000', '600000.BJ', 'SH600000', 'NaN', '800001', '800001.SZ', '400001', '700001', '600000.SZ', '000001.SH', '500001.SZ', '900001.SZ']) {
    const draft = portfolioToDraft(DEFAULT_QUANT_PORTFOLIO)
    draft.holdings = [{ ...newHoldingDraft(), symbol }]
    assert.throws(() => validateQuantDraft(draft))
  }
  const draft = portfolioToDraft(DEFAULT_QUANT_PORTFOLIO)
  draft.strategy.kind = 'custom'
  assert.throws(() => validateQuantDraft(draft), /有效策略/)
  draft.strategy.kind = 'trend'
  draft.holdings = [{ ...newHoldingDraft(), symbol: '600000', name: 'x'.repeat(101) }]
  assert.throws(() => validateQuantDraft(draft), /100 字/)
  draft.holdings[0].name = ''; draft.holdings[0].note = 'x'.repeat(1001)
  assert.throws(() => validateQuantDraft(draft), /1000 字/)
})

test('supported code prefixes normalize to the matching exchange', () => {
  const { normalizeQuantSymbol } = moduleMock.exports
  for (const symbol of ['000001', '100001', '200001', '300001']) assert.equal(normalizeQuantSymbol(symbol), symbol + '.SZ')
  for (const symbol of ['500001', '600001', '900001']) assert.equal(normalizeQuantSymbol(symbol), symbol + '.SH')
  assert.equal(normalizeQuantSymbol(' 600000.sh '), '600000.SH')
})

test('reference prices preserve three-decimal ETF quotes and up to six decimals', () => {
  const { quantReferencePrice } = moduleMock.exports
  assert.equal(quantReferencePrice(2.197), '¥2.197')
  assert.equal(quantReferencePrice(10.25), '¥10.25')
  assert.equal(quantReferencePrice(2.197123), '¥2.197123')
  assert.equal(quantReferencePrice(null), '—')
  assert.equal(quantReferencePrice(Infinity), '—')
})

test('source/time/period metadata is shown as provided without inventing live data', () => {
  assert.equal(quantContextValue(null, 'source'), '未提供')
  assert.equal(quantContextValue({}, 'as_of'), '未提供')
  assert.equal(quantContextValue({ period: 'm60' }, 'period'), 'm60')
  assert.equal(quantContextValue({ source: { prices: 'local_day' } }, 'source'), '{"prices":"local_day"}')
})
