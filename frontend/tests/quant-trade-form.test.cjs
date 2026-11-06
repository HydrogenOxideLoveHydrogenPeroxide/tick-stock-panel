const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const ts = require('typescript')
function screen(onSubmit) {
  let cursor = 0; const slots = []; let tree
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }] },
    useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i] },
  }
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, '../src/components/QuantTradeForm.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText
  vm.runInNewContext('(function(require,module,exports){' + code + '\n})', { Error, crypto: { randomUUID: () => 'a'.repeat(32) } })(name => name === 'react' ? react : name === '@/lib/quant-assistant' ? { quantReferencePrice: p => '¥' + p } : require(name), module, module.exports)
  const item = { symbol: '513100.SH', name: '纳指ETF', action: 'reduce', quantity: 3200, price: 2.229 }
  const portfolio = { cash: 1000 }
  function render() { cursor = 0; tree = module.exports.QuantTradeForm({ item, suggestionKey: 'snapshot', portfolio, onSubmit, onClose() {} }) }
  function nodes() { const out = []; function walk(n) { if (Array.isArray(n)) n.forEach(walk); else if (n?.props) { out.push(n); walk(n.props.children) } } walk(tree); return out }
  const field = label => nodes().find(n => n.type === 'input' && n.props['aria-label'] === label)
  const type = (label, value) => { field(label).props.onChange({ target: { value } }); render() }
  render()
  return { render, field, type, submit: () => tree.props.onSubmit({ preventDefault() {} }), text: () => JSON.stringify(tree) }
}

test('reference price is not submitted as an actual fill without input', async () => {
  let calls = 0
  const ui = screen(async () => { calls++ })
  assert.equal(ui.field('实际成交均价').props.value, '')
  assert.equal(ui.field('全部费用').props.value, '')
  await ui.submit(); ui.render()
  assert.equal(calls, 0)
  assert.match(ui.text(), /请填写实际成交价/)
})

test('actual reduction maps to sell, preserves ETF precision and blocks double submit', async () => {
  let calls = 0; let finish; let received
  const ui = screen(payload => { calls++; received = payload; return new Promise(resolve => { finish = resolve }) })
  ui.type('实际成交均价','2.229'); ui.type('全部费用','5.00')
  const pending = ui.submit(); await ui.submit()
  assert.equal(calls, 1)
  assert.equal(received.side, 'sell')
  assert.equal(received.quantity, 3200)
  assert.equal(received.price, 2.229)
  assert.equal(received.fees, 5)
  finish(); await pending
})

test('retry after a response error uses the same identity and retains actual inputs', async () => {
  const ids = []
  const ui = screen(async payload => { ids.push(payload.client_id); throw Error('network offline') })
  ui.type('实际成交均价','2.229'); ui.type('全部费用','0')
  await ui.submit(); ui.render()
  assert.match(ui.text(), /network offline/)
  assert.equal(ui.field('实际成交均价').props.value, '2.229')
  await ui.submit()
  assert.equal(ids.length, 2)
  assert.equal(ids[0], ids[1])
})
