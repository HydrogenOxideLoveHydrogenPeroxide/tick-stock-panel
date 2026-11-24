const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const filename = path.resolve(__dirname, '../src/components/stock-analysis/AnalysisDataControls.tsx')
const moduleObject = { exports: {} }
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
}}).outputText
const deps = name => name === '@/lib/api' ? { api: {} } : require(name)
vm.runInNewContext(`(function(require,module,exports){${code}\n})`, { console })(deps, moduleObject, moduleObject.exports)
const { AnalysisDataControls } = moduleObject.exports
function find(node, predicate) {
  if (node == null || typeof node !== 'object') return undefined
  if (predicate(node)) return node
  for (const child of [node.props?.children].flat(Infinity)) {
    const result = find(child, predicate)
    if (result) return result
  }
}
test('default day mode news checkbox is clickable and changes only the news option', () => {
  const changed = []; const modes = []
  const tree = AnalysisDataControls({symbol:'601988.SH', mode:'existing', includeNews:false,
    onIncludeNewsChange:v => changed.push(v), onModeChange:v => modes.push(v)})
  const input = find(tree, n => n.type === 'input' && n.props.type === 'checkbox')
  assert.equal(Boolean(input.props.disabled), false)
  input.props.onChange({target:{checked:true}})
  assert.deepEqual(changed, [true])
  assert.deepEqual(modes, [])
})
test('returning to existing mode preserves explicitly selected news', () => {
  const news = []; const modes = []
  const tree = AnalysisDataControls({symbol:'601988.SH', mode:'noon', includeNews:true,
    onIncludeNewsChange:v => news.push(v), onModeChange:v => modes.push(v)})
  find(tree, n => n.type === 'select').props.onChange({target:{value:'existing'}})
  assert.deepEqual(modes, ['existing'])
  assert.deepEqual(news, [])
})
test('controls remain disabled while an analysis is running', () => {
  const tree = AnalysisDataControls({symbol:'601988.SH', mode:'existing', includeNews:false,
    disabled:true, onIncludeNewsChange:()=>{}, onModeChange:()=>{}})
  assert.equal(find(tree, n => n.type === 'input').props.disabled, true)
})
