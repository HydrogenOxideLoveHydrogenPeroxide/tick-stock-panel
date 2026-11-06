const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('../node_modules/typescript')
const source = ts.transpileModule(fs.readFileSync('frontend/src/components/FundamentalSyncCard.tsx', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
function harness(api) {
  const slots = []; let cursor = 0; let pending = []; let tree; let timer; let updates = 0
  const react = {
    useState(v) { const i = cursor++; if (!(i in slots)) slots[i] = typeof v === 'function' ? v() : v; return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v }] },
    useRef(v) { const i = cursor++; if (!(i in slots)) slots[i] = { current: v }; return slots[i] },
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || deps.some((v, j) => v !== slots[i].deps[j])) { if (slots[i]?.cleanup) slots[i].cleanup(); slots[i] = { deps }; pending.push(() => { slots[i].cleanup = fn() }) } },
  }
  const jsx = (type, props) => ({ type, props })
  const module = { exports: {} }
  vm.runInNewContext(source, { exports: module.exports, module, require: name => name === 'react' ? react : name === 'react/jsx-runtime' ? { jsx, jsxs: jsx } : { api }, setTimeout: fn => { timer = fn; return 1 }, clearTimeout: () => { timer = null } })
  function render() { cursor = 0; tree = module.exports.FundamentalSyncCard({ onUpdated: () => updates++ }); const p = pending; pending = []; p.forEach(f => f()) }
  function nodes() { const out = []; function walk(n) { if (Array.isArray(n)) n.forEach(walk); else if (n?.props) { out.push(n); walk(n.props.children) } } walk(tree); return out }
  function text(n) { return Array.isArray(n) ? n.map(v => text(v)).join('') : n?.props ? text(n.props.children) : n == null ? '' : String(n) }
  render()
  return { render, text: () => text(tree), nodes, button: label => nodes().find(n => n.type === 'button' && text(n).includes(label)), tick: async () => { if (timer) await timer(); render() }, settle: async () => { await new Promise(resolve => setImmediate(resolve)); render() } }
}

test('mount and status refresh never opt into source collection; empty input is rejected', async () => {
  let starts = 0; let gets = 0
  const ui = harness({ getFundamentalSync: async () => { gets++; return { status: 'idle', notes: [] } }, startFundamentalSync: async () => starts++ })
  await ui.settle(); assert.equal(starts, 0)
  ui.button('抓取财报与市值').props.onClick(); ui.render()
  assert.match(ui.text(), /1～20/); assert.equal(starts, 0)
  ui.button('刷新状态').props.onClick(); ui.render(); await ui.settle()
  assert.equal(gets, 2); assert.equal(starts, 0)
})

test('explicit input starts one batch and polls progress without starting another batch', async () => {
  let starts = []; let gets = 0
  const ui = harness({ getFundamentalSync: async () => ({ status: ++gets === 1 ? 'idle' : gets === 2 ? 'running' : 'completed', completed: gets >= 3 ? 2 : 0, total: 2, notes: [] }), startFundamentalSync: async symbols => { starts.push(Array.from(symbols)); return { job_id: 'one' } } })
  await ui.settle()
  ui.nodes().find(n => n.type === 'input').props.onChange({ target: { value: '600519，000858' } }); ui.render()
  ui.button('抓取财报与市值').props.onClick(); await ui.settle(); await ui.settle()
  assert.deepEqual(starts, [['600519', '000858']])
  assert.equal(ui.button('正在采集').props.disabled, true)
  await ui.tick(); assert.match(ui.text(), /本批完成/)
  assert.equal(starts.length, 1)
})
