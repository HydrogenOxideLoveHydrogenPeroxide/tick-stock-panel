const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { createRequire } = require('node:module')
const realRequire = createRequire(path.resolve(__dirname, '../package.json'))
const ts = realRequire('typescript')
const modules = new Map()
function load(name) {
  const base = path.resolve(__dirname, '../src', name)
  const filename = ['.ts', '.tsx'].map(x => base + x).find(fs.existsSync)
  if (modules.has(filename)) return modules.get(filename).exports
  const module = { exports: {} }; modules.set(filename, module)
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  }}).outputText
  const requireLocal = name => name.startsWith('@/') ? load(name.slice(2)) : realRequire(name)
  vm.runInNewContext(`(function(require,module,exports){${output}\n})`, { console })(requireLocal, module, module.exports)
  return module.exports
}
const { renderBuiltinDataCell } = load('components/stock-table/primitives')
const { getSortValue } = load('lib/stock-table')
test('fund prices preserve three decimals and stocks keep two', () => {
  const priceColumn = { id: 'builtin:price', source: { type: 'builtin', key: 'price' } }
  for (const [asset_type, close, expected] of [
    ['etf', 2.244, '2.244'], ['etf', 1.33, '1.330'], ['stock', 4.56, '4.56'],
    ['etf', null, '—'],
  ]) {
    assert.equal(renderBuiltinDataCell({ asset_type, close }, priceColumn).props.children, expected)
  }
})
for (const key of ['momentum', 'momentum_60d']) {
  test(`60-day momentum renders and sorts with ${key}`, () => {
    const column = { id: 'builtin:momentum_60d', source: { type: 'builtin', key } }
    const row = { momentum_60d: .12 }
    const cell = renderBuiltinDataCell(row, column)
    assert.ok(cell)
    assert.match(String(cell.props.children), /12\.00%/)
    assert.equal(getSortValue(row, column), .12)
  })
}
