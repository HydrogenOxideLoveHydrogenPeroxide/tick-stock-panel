const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
function load(file, overrides = {}, globals = {}) {
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, '../src', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText
  vm.runInNewContext('(function(require,module,exports){' + code + '\n})', { Error, ...globals })(name => name in overrides ? overrides[name] : require(name), module, module.exports)
  return module.exports
}
const helper = load('lib/quant-assistant.ts')
const ETF = { symbol: '513100.SH', name: '纳指ETF国泰' }
const BANK = { symbol: '600000.SH', name: '浦发银行' }
const plain = value => JSON.parse(JSON.stringify(value))
function harness(initial, others = []) {
  let holding = initial || helper.newHoldingDraft(); let disabled = false
  const slots = []; const effects = []; const timers = new Map(); const requests = []; const resolved = []
  let cursor = 0; let timerId = 0; let tree
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }] },
    useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i] },
    useEffect(effect, deps) {
      const i = cursor++; const previous = slots[i]
      if (!previous || deps.some((value, j) => value !== previous.deps[j])) {
        previous?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = effect() })
      }
    },
  }
  const { QuantInstrumentFields } = load('components/QuantInstrumentFields.tsx', {
    react, '@/lib/quant-assistant': helper,
    '@/lib/api': { api: { instrumentSearch: query => new Promise((resolve, reject) => requests.push({ query, resolve, reject })) } },
  }, { window: { setTimeout: fn => { timers.set(++timerId, fn); return timerId }, clearTimeout: id => timers.delete(id) } })
  function render() {
    cursor = 0
    tree = QuantInstrumentFields({ holding, index: 0, disabled, inputClass: '',
      onEdit: (field, value) => { holding = helper.editQuantIdentity(holding, field, value) },
      onSelect: (item, expected) => {
        try {
          const next = helper.completeQuantHolding({ holdings: [holding, ...others] }, 0, item, expected)
          holding = next.holdings[0]; resolved.push(item); return null
        } catch (error) { return error.message }
      },
    })
    effects.splice(0).forEach(fn => fn())
  }
  function nodes() { const out = []; function walk(node) { if (Array.isArray(node)) node.forEach(walk); else if (node?.props) { out.push(node); walk(node.props.children) } } walk(tree); return out }
  const input = field => nodes().find(node => node.type === 'input' && node.props['aria-label'] === '第 1 行' + (field === 'symbol' ? '代码' : '名称'))
  function type(field, value) { input(field).props.onChange({ target: { value } }); render() }
  function flush() { const all = [...timers.values()]; timers.clear(); all.forEach(fn => fn()) }
  async function settle() { await new Promise(resolve => setImmediate(resolve)); render() }
  render()
  return { type, flush, settle, requests, resolved, input, nodes, get holding() { return holding },
    replace: value => { holding = value; render() }, disable: () => { disabled = true; render() },
    text: () => JSON.stringify(tree) }
}

test('exact unique code/name resolve while prefixes and ambiguous names require selection', () => {
  assert.equal(helper.exactQuantInstrument(' 513100.sh ', [ETF]), ETF)
  assert.equal(helper.exactQuantInstrument('纳指ETF国泰', [ETF]), ETF)
  assert.equal(helper.exactQuantInstrument('513', [ETF]), null)
  assert.equal(helper.exactQuantInstrument('', [ETF]), null)
  assert.equal(helper.exactQuantInstrument('同名', [{ ...ETF, name: '同名' }, { ...BANK, name: '同名' }]), null)
})

test('typing a complete code auto-fills canonical code/name and preserves account fields', async () => {
  const h = harness({ ...helper.newHoldingDraft(), quantity: '200', available_quantity: '100', cost_price: '2.197', note: '持仓备注', lot_size: '200' })
  assert.equal(h.requests.length, 0)
  h.type('symbol', '513'); h.type('symbol', '513100'); h.flush()
  assert.equal(h.requests.length, 1)
  h.requests[0].resolve({ results: [ETF] }); await h.settle()
  assert.deepEqual(plain(h.holding), { symbol: ETF.symbol, name: ETF.name, quantity: '200', available_quantity: '100', cost_price: '2.197', note: '持仓备注', lot_size: '200' })
})

test('unique full name auto-fills code; fuzzy name supports keyboard selection', async () => {
  const h = harness(); h.type('name', ETF.name); h.flush()
  h.requests[0].resolve({ results: [ETF] }); await h.settle()
  assert.equal(h.holding.symbol, ETF.symbol)
  const fuzzy = harness(); fuzzy.type('name', '纳指'); fuzzy.flush()
  fuzzy.requests[0].resolve({ results: [ETF, { symbol: '159941.SZ', name: '纳指ETF' }] }); await fuzzy.settle()
  assert.equal(fuzzy.holding.symbol, '')
  assert.equal(fuzzy.input('name').props['aria-expanded'], true)
  fuzzy.input('name').props.onKeyDown({ key: 'Enter', nativeEvent: { isComposing: true }, preventDefault() {} });
  assert.equal(fuzzy.holding.symbol, '')
  fuzzy.input('name').props.onKeyDown({ key: 'Enter', preventDefault() {} }); await fuzzy.settle()
  assert.equal(fuzzy.holding.symbol, ETF.symbol)
})

test('stale requests cannot fill a changed/deleted row or a disabled form', async () => {
  const h = harness(); h.type('symbol', '513100'); h.flush()
  h.type('symbol', '600000'); h.flush()
  h.requests[0].resolve({ results: [ETF] }); await h.settle()
  assert.equal(h.resolved.length, 0)
  h.requests[1].resolve({ results: [BANK] }); await h.settle()
  assert.equal(h.holding.name, BANK.name)
  const removed = harness(); removed.type('symbol', '513100'); removed.flush()
  removed.replace(helper.newHoldingDraft()); removed.requests[0].resolve({ results: [ETF] }); await removed.settle()
  assert.equal(removed.holding.symbol, '')
  const disabled = harness(); disabled.type('symbol', '513100'); disabled.flush(); disabled.disable()
  disabled.requests[0].resolve({ results: [ETF] }); await disabled.settle()
  assert.equal(disabled.resolved.length, 0)
})

test('duplicate completion is rejected; changing code clears its old name', async () => {
  const h = harness(undefined, [{ ...helper.newHoldingDraft(), symbol: '513100' }])
  h.type('symbol', '513100'); h.flush(); h.requests[0].resolve({ results: [ETF] }); await h.settle()
  assert.equal(h.holding.name, '')
  assert.match(h.text(), /已在组合中/)
  assert.equal(helper.editQuantIdentity({ ...helper.newHoldingDraft(), ...ETF }, 'symbol', '600000').name, '')
  assert.equal(helper.editQuantIdentity({ ...helper.newHoldingDraft(), ...ETF }, 'symbol', '513100').name, ETF.name)
})
