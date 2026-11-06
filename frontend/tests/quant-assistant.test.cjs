const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const realRequire = require
function load(relative, overrides = {}, globals = {}) {
  const filename = path.resolve(__dirname, '../src', relative)
  const module = { exports: {} }
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  }}).outputText
  const requireMock = name => name in overrides ? overrides[name] : realRequire(name)
  vm.runInNewContext('(function(require,module,exports){' + output + '\n})', { console, Error, ...globals })(requireMock, module, module.exports)
  return module.exports
}
const helper = load('lib/quant-assistant.ts')
const cacheHelper = load('lib/quant-draft-cache.ts', { './quant-assistant': helper })
function memoryStorage() { const data = new Map(); return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) } }
const plain = value => JSON.parse(JSON.stringify(value))
const portfolio = () => plain(helper.DEFAULT_QUANT_PORTFOLIO)
function holding(symbol = '600000') { return { ...helper.newHoldingDraft(), symbol } }
function validDraft() { return { ...helper.portfolioToDraft(portfolio()), cash: '10000', holdings: [holding()] } }

test('empty defaults, zero-share candidate and canonical symbols preserve RMB/share units', () => {
  assert.deepEqual(portfolio(), { cash: 0, holdings: [], strategy: { kind: 'trend', screener_strategy_id: '', stop_loss_pct: 8, take_profit_pct: 20, max_position_pct: 25, target_position_pct: 15, cash_reserve_pct: 10 } })
  const draft = validDraft()
  const output = helper.validateQuantDraft(draft)
  assert.equal(output.holdings[0].symbol, '600000.SH')
  assert.equal(output.holdings[0].quantity, 0)
  assert.equal(output.holdings[0].cost_price, 0)
  draft.holdings[0] = { ...holding('000001.sz'), quantity: '100', available_quantity: '50', cost_price: '10.25', lot_size: '200' }
  const held = helper.validateQuantDraft(draft).holdings[0]
  assert.equal(held.symbol, '000001.SZ')
  assert.equal(held.quantity, 100)
  assert.equal(held.available_quantity, 50)
  assert.equal(held.cost_price, 10.25)
  assert.equal(held.lot_size, 200)
})

for (const [field, value] of [['quantity', ''], ['quantity', '1.5'], ['quantity', '-1'], ['quantity', '1e3'], ['quantity', '1000000001'], ['available_quantity', '1'], ['cost_price', 'Infinity'], ['cost_price', '1000001'], ['lot_size', '10']]) {
  test('reject invalid holding ' + field + '=' + JSON.stringify(value), () => {
    const draft = validDraft(); draft.holdings[0][field] = value
    assert.throws(() => helper.validateQuantDraft(draft))
  })
}

test('nonzero holdings need cost; normalized duplicates and >20 candidates rejected', () => {
  const draft = validDraft(); draft.holdings[0].quantity = '100'
  assert.throws(() => helper.validateQuantDraft(draft), /成本价/)
  draft.holdings = [holding('600000'), holding('600000.SH')]
  assert.throws(() => helper.validateQuantDraft(draft), /重复/)
  draft.holdings = Array.from({ length: 21 }, (_, i) => holding(String(600000 + i)))
  assert.throws(() => helper.validateQuantDraft(draft), /20/)
})

test('cash/threshold bounds finite and target <= max; invalid edits remain dirty', () => {
  for (const cash of ['', 'NaN', 'Infinity', '-1', '1000000000001']) {
    const draft = validDraft(); draft.cash = cash
    assert.throws(() => helper.validateQuantDraft(draft))
  }
  for (const [key, value] of [['stop_loss_pct', '0'], ['stop_loss_pct', '51'], ['take_profit_pct', '201'], ['max_position_pct', '101'], ['target_position_pct', '26'], ['cash_reserve_pct', '101'], ['cash_reserve_pct', '']]) {
    const draft = validDraft(); draft.strategy[key] = value
    assert.throws(() => helper.validateQuantDraft(draft))
  }
  const saved = portfolio(); const draft = helper.portfolioToDraft(saved)
  assert.equal(helper.quantDraftDirty(draft, saved), false)
  draft.cash = ''; assert.equal(helper.quantDraftDirty(draft, saved), true)
})

test('API GET does not fetch quotes; POST sends draft only and PUT saves separately', async () => {
  const calls = []
  const { api } = load('lib/api.ts', { '@/components/Toast': { toast() { throw Error('unexpected toast') } } }, {
    fetch: async (url, init) => { calls.push({ url, init }); return { ok: true, json: async () => portfolio() } },
  })
  const draft = helper.validateQuantDraft(validDraft())
  await api.getQuantPortfolio()
  await api.getQuantSuggestions(draft, 'tail')
  assert.deepEqual(calls.map(c => c.url), ['/api/quant-assistant/portfolio', '/api/quant-assistant/suggest'])
  assert.equal(calls[1].init.method, 'POST')
  assert.deepEqual(JSON.parse(calls[1].init.body), { portfolio: plain(draft), mode: 'tail' })
  await api.saveQuantPortfolio(draft)
  assert.equal(calls[2].init.method, 'PUT')
  assert.deepEqual(JSON.parse(calls[2].init.body), plain(draft))
})

test('Pydantic 422 and non-JSON HTTP failures produce readable retry errors', async () => {
  let jsonFailure = false
  const { api } = load('lib/api.ts', { '@/components/Toast': { toast() {} } }, {
    fetch: async () => ({ ok: false, status: 422, statusText: 'Unprocessable Entity', json: async () => {
      if (jsonFailure) throw Error('not json')
      return { detail: [{ loc: ['body', 'holdings', 0, 'quantity'], msg: 'Input should be a valid integer' }] }
    } }),
  })
  await assert.rejects(api.saveQuantPortfolio(portfolio()), /holdings.0.quantity：Input should be a valid integer/)
  jsonFailure = true
  await assert.rejects(api.getQuantPortfolio(), /422 Unprocessable Entity/)
})

// Small hook harness exercises the component's mocked API interactions without browser dependencies.
function screen(mockApi, confirm = () => true, storage = memoryStorage()) {
  let cursor = 0; const slots = []; let effects = []; let tree
  const ReactMock = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }] },
    useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i] },
    useEffect(effect, deps) { const i = cursor++; const before = slots[i]; if (!before || deps.some((value, j) => before[j] !== value)) { slots[i] = deps; effects.push(effect) } },
  }
  const fieldStub = props => ({ type: 'span', props: { children: ['symbol', 'name'].map(field => ({ type: 'input', props: {
    'aria-label': '第 ' + (props.index + 1) + ' 行' + (field === 'symbol' ? '代码' : '名称'),
    value: props.holding[field], disabled: props.disabled, onChange: e => props.onEdit(field, e.target.value),
  } })) } })
  const { QuantAssistant } = load('pages/QuantAssistant.tsx', {
    react: ReactMock, '@/lib/api': { api: { getQuantTrades: async () => [], getQuantStrategyCatalog: async () => ({ strategies: [] }), ...mockApi } }, '@/lib/quant-assistant': helper,
    '@/lib/quant-draft-cache': cacheHelper,
    '@/components/PageHeader': { PageHeader: () => null },
    '@/components/QuantCandidateSearch': { QuantCandidateSearch: () => null },
    '@/components/QuantInstrumentFields': { QuantInstrumentFields: fieldStub },
    '@/components/QuantTradeForm': { QuantTradeForm: () => null },
    '@/components/ContextResearchCard': { ContextResearchCard: () => null },
  }, { window: { confirm, sessionStorage: storage } })
  function render() { cursor = 0; tree = QuantAssistant(); const pending = effects; effects = []; pending.forEach(effect => effect()); return tree }
  function nodes() { const output = []; function walk(node) { if (node?.type === fieldStub) { output.push(node); walk(fieldStub(node.props)); return } if (Array.isArray(node)) node.forEach(walk); else if (node && typeof node === 'object' && node.props) { output.push(node); walk(node.props.children) } } walk(tree); return output }
  function text(node) { if (Array.isArray(node)) return node.map(text).join(''); if (node && typeof node === 'object' && node.props) return text(node.props.children); return typeof node === 'string' ? node : '' }
  const button = label => nodes().find(n => n.type === 'button' && text(n).includes(label))
  const input = label => nodes().find(n => n.type === 'input' && n.props['aria-label'] === label)
  render()
  return { render, nodes, button, input, text: () => text(tree), settle: async () => { await new Promise(resolve => setImmediate(resolve)); render() } }
}
function suggestion() { return { generated_at: '2026-11-04T12:00:00+08:00', mode: 'morning', summary: { cash: 10000, total_assets: null, market_value: null, position_pct: null, buy_budget: 9000 }, suggestions: [], warnings: ['缺少行情'] } }

test('screen mounts GET only, generates unsaved validated draft, saves separately, edits invalidate output', async () => {
  const calls = []
  const api = {
    getQuantPortfolio: async () => { calls.push('GET'); return portfolio() },
    getQuantSuggestions: async (p, mode) => { calls.push({ kind: 'POST', p: plain(p), mode }); return suggestion() },
    saveQuantPortfolio: async p => { calls.push('PUT'); return p },
  }
  const ui = screen(api); await ui.settle()
  assert.deepEqual(calls, ['GET'])
  ui.button('添加标的').props.onClick(); ui.render()
  ui.input('第 1 行代码').props.onChange({ target: { value: '600000' } }); ui.render()
  assert.match(ui.text(), /未保存修改/)
  ui.button('生成建议').props.onClick(); ui.render()
  assert.equal(ui.button('添加标的').props.disabled, true)
  assert.equal(ui.button('保存持仓与策略').props.disabled, true)
  await ui.settle()
  assert.equal(calls[1].kind, 'POST')
  assert.equal(calls[1].p.holdings[0].symbol, '600000.SH')
  assert.equal(calls[1].mode, 'morning')
  assert.match(ui.text(), /2026-11-04/)
  assert.ok(!calls.includes('PUT'))
  ui.input('第 1 行名称').props.onChange({ target: { value: '候选' } }); ui.render()
  assert.doesNotMatch(ui.text(), /2026-11-04/)
  ui.button('保存持仓与策略').props.onClick(); await ui.settle()
  assert.equal(calls[2], 'PUT')
  assert.match(ui.text(), /当前组合已保存/)
})

test('initial GET failure can retry, blocks unsafe editing/save and does not auto-write', async () => {
  let attempts = 0
  const ui = screen({ getQuantPortfolio: async () => { if (++attempts === 1) throw Error('offline'); return portfolio() } })
  await ui.settle()
  assert.match(ui.text(), /读取持仓失败：offline/)
  assert.equal(ui.button('添加标的').props.disabled, true)
  assert.equal(ui.button('生成建议').props.disabled, true)
  ui.button('重新读取持仓').props.onClick(); ui.render(); await ui.settle()
  assert.equal(attempts, 2)
  assert.equal(ui.button('添加标的').props.disabled, false)
})

test('invalid draft never reaches POST and delete requires confirmation', async () => {
  let posted = false; let confirmed = false
  const p = portfolio(); p.holdings = [{ symbol: '600000.SH', name: '', quantity: 100, available_quantity: 100, cost_price: 10, lot_size: 100, note: '' }]
  const ui = screen({ getQuantPortfolio: async () => p, getQuantSuggestions: async () => { posted = true; return suggestion() } }, () => { confirmed = true; return false })
  await ui.settle()
  ui.input('第 1 行持有数量（股）').props.onChange({ target: { value: '' } }); ui.render()
  ui.button('生成建议').props.onClick(); ui.render()
  assert.equal(posted, false)
  assert.match(ui.text(), /非负整数/)
  ui.nodes().find(n => n.type === 'button' && n.props['aria-label'] === '删除第 1 行').props.onClick(); ui.render()
  assert.equal(confirmed, true)
  assert.ok(ui.input('第 1 行代码'))
})


test('search adds a normalized ETF candidate, rejects duplicate without replacing holdings', () => {
  const draft = validDraft()
  draft.holdings[0] = { ...holding('600000'), quantity: '100', available_quantity: '100', cost_price: '12.5' }
  const next = helper.addQuantCandidate(draft, { symbol: '513100.sh', name: '纳指ETF国泰' })
  assert.equal(next.holdings.length, 2)
  assert.equal(next.holdings[1].symbol, '513100.SH')
  assert.equal(next.holdings[1].quantity, '0')
  assert.equal(next.holdings[0].cost_price, '12.5')
  assert.equal(draft.holdings.length, 1)
  assert.throws(() => helper.addQuantCandidate(draft, { symbol: '600000.SH', name: '重复' }), /已在组合/)
  assert.equal(helper.addQuantCandidate(draft, { symbol: '688001', name: '科创板' }).holdings[1].lot_size, '200')
  assert.throws(() => helper.addQuantCandidate(draft, { symbol: '800001', name: '' }))
  draft.holdings = Array.from({ length: 20 }, (_, i) => holding(String(600000 + i)))
  assert.throws(() => helper.addQuantCandidate(draft, { symbol: '513100', name: '' }), /20/)
})

test('reference summary separates buys, sells, holds and blocked rows; ignores unusable prices', () => {
  const result = suggestion()
  const row = (action, quantity, price) => ({ action, quantity, price })
  result.suggestions = [row('buy', 200, 2.197), row('sell', 100, 10), row('reduce', 50, 10),
    row('hold', 0, 10), row('blocked', 100, 10), row('buy', 100, null)]
  const totals = plain(helper.quantSuggestionTotals(result))
  assert.ok(Math.abs(totals.buyAmount - 439.4) < 1e-9)
  assert.deepEqual({ ...totals, buyAmount: 439.4 }, {
    buyCount: 1, sellCount: 2, holdCount: 1, blockedCount: 1, buyAmount: 439.4, sellAmount: 1500,
  })
  const p = portfolio()
  const report = JSON.parse(helper.quantReportJson(p, result))
  p.cash = 999
  assert.equal(report.portfolio.cash, 0)
  assert.deepEqual(report.result, result)
  assert.equal(report.schema_version, 1)
})

test('search selection adds candidate to draft only and reports duplicates without losing edits', async () => {
  const calls = []
  const ui = screen({ getQuantPortfolio: async () => portfolio(), saveQuantPortfolio: async p => { calls.push('PUT'); return p } })
  await ui.settle()
  const search = () => ui.nodes().find(n => typeof n.props.onSelect === 'function')
  assert.equal(search().props.onSelect({ symbol: '513100', name: '纳指ETF国泰' }), true)
  ui.render()
  assert.equal(ui.input('第 1 行代码').props.value, '513100.SH')
  assert.equal(ui.input('第 1 行持有数量（股）').props.value, '0')
  assert.match(ui.text(), /未保存修改/)
  assert.equal(search().props.onSelect({ symbol: '513100.SH', name: '重复' }), false)
  ui.render()
  assert.match(ui.text(), /已在组合/)
  assert.deepEqual(calls, [])
})

test('candidate search debounces, ignores stale responses and stops when disabled', async () => {
  let cursor = 0; const slots = []; const effects = []; const timers = new Map(); let timerId = 0
  const requests = []; let tree; let disabled = false
  const ReactMock = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }] },
    useEffect(effect, deps) {
      const i = cursor++; const prior = slots[i]
      if (!prior || deps.some((value, j) => prior.deps[j] !== value)) {
        if (prior?.cleanup) prior.cleanup()
        slots[i] = { deps }
        effects.push(() => { slots[i].cleanup = effect() })
      }
    },
  }
  const { QuantCandidateSearch } = load('components/QuantCandidateSearch.tsx', {
    react: ReactMock, '@/lib/api': { api: { instrumentSearch: query => new Promise(resolve => requests.push({ query, resolve })) } },
  }, { window: { setTimeout: fn => { timers.set(++timerId, fn); return timerId }, clearTimeout: id => timers.delete(id) } })
  function render() { cursor = 0; tree = QuantCandidateSearch({ disabled, onSelect: () => true }); effects.splice(0).forEach(fn => fn()) }
  function input(node) {
    if (Array.isArray(node)) return node.map(input).find(Boolean)
    if (node?.type === 'input') return node
    return node?.props ? input(node.props.children) : null
  }
  function type(query) { input(tree).props.onChange({ target: { value: query } }); render() }
  function flushTimers() { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()) }
  render(); assert.equal(timers.size, 0)
  type('513'); type('513100')
  assert.equal(timers.size, 1); flushTimers()
  assert.equal(requests.length, 1); assert.equal(requests[0].query, '513100')
  type('浦发'); flushTimers()
  requests[1].resolve({ results: [{ symbol: '600000.SH', name: '浦发银行' }] })
  await new Promise(resolve => setImmediate(resolve)); render()
  requests[0].resolve({ results: [{ symbol: '513100.SH', name: '旧结果' }] })
  await new Promise(resolve => setImmediate(resolve)); render()
  assert.match(JSON.stringify(tree), /浦发银行/)
  assert.doesNotMatch(JSON.stringify(tree), /旧结果/)
  disabled = true; render(); render()
  assert.equal(timers.size, 0)
  assert.equal(input(tree).props.disabled, true)
  assert.doesNotMatch(JSON.stringify(tree), /浦发银行/)
})

test('Screener directory sync exposes full strategy IDs and selection is saved only on request', async () => {
  const calls = []; let fetches = 0
  const catalog = { strategies: [{ id: 'quant_weekly_selection', name: 'Quant 一周多因子选股', description: '真实筛选逻辑', source: 'custom' }], mode: 'screener', disclaimer: '共享筛选规则' }
  const ui = screen({
    getQuantPortfolio: async () => portfolio(),
    getQuantStrategyCatalog: async () => { fetches++; return catalog },
    saveQuantPortfolio: async p => { calls.push(plain(p)); return p },
  })
  await ui.settle()
  assert.match(ui.text(), /Quant 一周多因子选股/)
  const selector = ui.nodes().find(n => n.type === 'select' && n.props['aria-label'] === '选股策略')
  selector.props.onChange({ target: { value: 'quant_weekly_selection' } }); ui.render()
  assert.match(ui.text(), /真实筛选逻辑/)
  assert.deepEqual(calls, [])
  ui.button('从选股页同步策略').props.onClick(); await ui.settle()
  assert.equal(fetches, 2)
  assert.deepEqual(calls, [])
  ui.button('保存持仓与策略').props.onClick(); await ui.settle()
  assert.equal(calls[0].strategy.screener_strategy_id, 'quant_weekly_selection')
})

test('catalog errors remain visible and sync can retry without clearing the saved strategy', async () => {
  let attempts = 0
  const p = portfolio(); p.strategy.screener_strategy_id = 'consecutive_limit_ups'
  const ui = screen({
    getQuantPortfolio: async () => p,
    getQuantStrategyCatalog: async () => {
      if (++attempts === 1) throw Error('directory offline')
      return { strategies: [{ id: 'consecutive_limit_ups', name: '连板股', description: '连板信号', source: 'builtin' }] }
    },
  })
  await ui.settle()
  assert.match(ui.text(), /directory offline/)
  assert.match(ui.text(), /已保存策略待核验/)
  ui.button('从选股页同步策略').props.onClick(); await ui.settle()
  assert.doesNotMatch(ui.text(), /directory offline/)
  const selector = ui.nodes().find(n => n.type === 'select' && n.props['aria-label'] === '选股策略')
  assert.equal(selector.props.value, 'consecutive_limit_ups')
  assert.match(ui.text(), /连板信号/)
})

test('row completion updates draft only and simultaneous lookups cannot create duplicate holdings', async () => {
  let saves = 0
  const ui = screen({ getQuantPortfolio: async () => portfolio(), saveQuantPortfolio: async p => { saves++; return p } })
  await ui.settle()
  ui.button('添加标的').props.onClick(); ui.render()
  ui.button('添加标的').props.onClick(); ui.render()
  const fields = ui.nodes().filter(n => n.props.holding && typeof n.props.onSelect === 'function')
  const item = { symbol: '513100.SH', name: '纳指ETF国泰' }
  // Both callbacks originate from the same render, like two network responses in one React batch.
  assert.equal(fields[0].props.onSelect(item, fields[0].props.holding), null)
  assert.match(fields[1].props.onSelect(item, fields[1].props.holding), /已在组合中/)
  ui.render()
  assert.equal(ui.input('第 1 行代码').props.value, '513100.SH')
  assert.equal(ui.input('第 1 行名称').props.value, item.name)
  assert.equal(ui.input('第 2 行代码').props.value, '')
  assert.equal(saves, 0)
  assert.match(ui.text(), /未保存修改/)
})

test('navigation/remount restores incomplete drafts; saving persists and clears cache', async () => {
  const storage = memoryStorage(); let persisted = portfolio(); let writes = 0
  const mockApi = { getQuantPortfolio: async () => persisted, saveQuantPortfolio: async p => { writes++; persisted = plain(p); return p } }
  const first = screen(mockApi, () => true, storage); await first.settle()
  first.button('添加标的').props.onClick(); first.render()
  first.input('第 1 行代码').props.onChange({ target: { value: '513' } }); first.render()
  const second = screen(mockApi, () => true, storage); await second.settle()
  assert.equal(second.input('第 1 行代码').props.value, '513')
  assert.match(second.text(), /已恢复本标签页未保存草稿/)
  assert.equal(writes, 0)
  second.input('第 1 行代码').props.onChange({ target: { value: '513100' } }); second.render()
  second.button('保存持仓与策略').props.onClick(); await second.settle()
  assert.equal(writes, 1)
  assert.equal(storage.getItem(cacheHelper.QUANT_DRAFT_KEY), null)
  const third = screen(mockApi, () => true, storage); await third.settle()
  assert.equal(third.input('第 1 行代码').props.value, '513100.SH')
})

test('server changes require explicit recovery; storage failures are visible', async () => {
  const storage = memoryStorage(); const draft = validDraft(); draft.cash = '99'
  cacheHelper.retainQuantDraft(storage, draft, portfolio())
  const changed = portfolio(); changed.cash = 5000
  const ui = screen({ getQuantPortfolio: async () => changed }, () => true, storage); await ui.settle()
  assert.match(ui.text(), /服务器组合已有更新/)
  assert.equal(ui.button('添加标的').props.disabled, true)
  ui.button('恢复未保存草稿').props.onClick(); ui.render()
  assert.equal(ui.input('第 1 行代码').props.value, '600000')
  const blockedStorage = { getItem() { throw Error('denied') }, setItem() { throw Error('denied') }, removeItem() { throw Error('denied') } }
  const blocked = screen({ getQuantPortfolio: async () => portfolio() }, () => true, blockedStorage); await blocked.settle()
  blocked.button('添加标的').props.onClick(); blocked.render()
  assert.match(blocked.text(), /浏览器草稿暂未保留/)
})

test('failed save retains draft; explicit discard restores saved data', async () => {
  const storage = memoryStorage()
  const ui = screen({ getQuantPortfolio: async () => portfolio(), saveQuantPortfolio: async () => { throw Error('offline') } }, () => true, storage)
  await ui.settle(); ui.button('添加标的').props.onClick(); ui.render()
  ui.input('第 1 行代码').props.onChange({ target: { value: '513100' } }); ui.render()
  ui.button('保存持仓与策略').props.onClick(); await ui.settle()
  assert.ok(storage.getItem(cacheHelper.QUANT_DRAFT_KEY))
  ui.button('放弃未保存修改').props.onClick(); ui.render()
  assert.equal(ui.input('第 1 行代码'), undefined)
  assert.equal(storage.getItem(cacheHelper.QUANT_DRAFT_KEY), null)
})

test('confirmed trade updates holdings and cash, keeps completion status, and shows stale-suggestion notice', async () => {
  const p = portfolio(); p.cash = 10000
  p.holdings = [{ symbol: '600000.SH', name: '测试', quantity: 100, available_quantity: 100, cost_price: 10, lot_size: 100, note: '' }]
  const item = { symbol: '600000.SH', name: '测试', action: 'reduce', quantity: 50, price: 11, market_value: 1100, position_pct: 10, pnl_pct: 10, reason: [], warnings: [], data_context: null }
  const result = { ...suggestion(), suggestions: [item] }
  const after = plain(p); after.cash = 10549; after.holdings[0].quantity = 50; after.holdings[0].available_quantity = 50
  let calls = 0
  const ui = screen({ getQuantPortfolio: async () => p, getQuantSuggestions: async () => result,
    recordQuantTrade: async payload => { calls++; return { portfolio: after, trade: { ...payload, client_id: 'test-trade-00001', name: '测试', cash_after: 10549, executed_at: '2026-11-06T00:00:00+08:00' }, duplicate: false } },
  })
  await ui.settle(); ui.button('生成建议').props.onClick(); await ui.settle()
  ui.nodes().find(n => n.props.item === item && n.props.onRecord).props.onRecord(); ui.render()
  const form = ui.nodes().find(n => n.props.suggestionKey && n.props.onSubmit)
  await form.props.onSubmit({ expected_portfolio: p, symbol: item.symbol, side: 'sell', quantity: 50, price: 11, fees: 1, suggestion_key: form.props.suggestionKey })
  ui.render()
  assert.equal(calls, 1)
  assert.equal(ui.input('第 1 行持有数量（股）').props.value, '50')
  assert.equal(ui.nodes().find(n => n.props.item === item && n.props.onRecord).props.completed, true)
  assert.match(ui.text(), /持仓、成本与现金已更新/)
  assert.match(ui.text(), /成交前建议快照/)
})

test('strategy candidates are explicit and adding one preserves zero holding until sizing', async () => {
  const p = portfolio(); p.strategy.screener_strategy_id = 'context_sentiment_bull_core'
  let calls = 0
  const ui = screen({ getQuantPortfolio: async () => p,
    getQuantCandidates: async () => { calls++; return { strategy_id: p.strategy.screener_strategy_id, name: '研究', as_of: '2026-11-05', total: 1, candidates: [{ symbol: '600000.SH', name: '浦发银行', score: 70 }] } },
  })
  await ui.settle(); assert.equal(calls, 0)
  ui.button('按策略选股').props.onClick(); await ui.settle()
  assert.equal(calls, 1)
  ui.button('加入候选').props.onClick(); ui.render()
  assert.equal(ui.input('第 1 行代码').props.value, '600000.SH')
  assert.equal(ui.input('第 1 行持有数量（股）').props.value, '0')
  assert.equal(ui.button('已加入组合').props.disabled, true)
})

test('LLM requires explicit click and edits hide the old explanation', async () => {
  const p = portfolio(); p.holdings = [helper.validateQuantDraft(validDraft()).holdings[0]]
  let calls = 0
  const ui = screen({ getQuantPortfolio: async () => p,
    explainQuantPortfolio: async () => { calls++; return { result: suggestion(), analysis: '这是模型解读', model: 'configured' } },
  })
  await ui.settle(); assert.equal(calls, 0)
  ui.button('重新计算并 LLM 解读').props.onClick(); await ui.settle()
  assert.equal(calls, 1); assert.match(ui.text(), /这是模型解读/)
  ui.input('第 1 行代码').props.onChange({ target: { value: '000001' } }); ui.render()
  assert.doesNotMatch(ui.text(), /这是模型解读/)
})
