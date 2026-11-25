const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const source = fs.readFileSync(path.resolve(__dirname, '../src/lib/httpErrorNotice.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const exportsObject = {}
vm.runInNewContext(compiled, { exports: exportsObject })
const { createHttpErrorNotifier } = exportsObject

test('one gateway notice for concurrent endpoints and repeated polling during an outage', () => {
  let clock = 0
  const messages = []
  const notify = createHttpErrorNotifier((...args) => messages.push(args), () => clock)
  for (let i = 0; i < 20; i++) notify([502, 503, 504][i % 3], 'Bad Gateway')
  assert.equal(messages.length, 1)
  clock = 29000; notify(502, 'Bad Gateway'); assert.equal(messages.length, 1)
  clock = 30000; notify(502, 'Bad Gateway'); assert.equal(messages.length, 2)
  assert.match(messages[0][0], /HTTP 502/)
})
test('validation errors stay visible and login expiry is handled by authentication', () => {
  const messages = []
  const notify = createHttpErrorNotifier(message => messages.push(message))
  notify(502, 'Bad Gateway'); notify(401, 'Unauthorized')
  notify(400, '日期范围无效'); notify(422, '参数错误')
  assert.deepEqual(messages.slice(1), ['日期范围无效', '参数错误'])
})
