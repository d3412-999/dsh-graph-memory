/**
 * 恒定注入预算（红线 R1）node 测试：
 *   node --test tests/node/
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assembleInjection,
  dynamicMemoryBudget,
  estimateTokens,
  formatMemoryLine,
  truncateToTokens,
  InjectionCache,
  SYSTEM_INSTRUCTION,
} from '../../lib/budgeter.js'

const INJECTION = {
  totalHardTokens: 3000,
  systemMaxTokens: 128,
  memoryMaxTokens: 2048,
  memoryDefaultTokens: 1024,
  memoryHighSimTokens: 1536,
  memoryBoostTokens: 1792,
  memoryExplicitTokens: 2048,
  highSimThreshold: 0.8,
  highWeightThreshold: 5,
  dayMaxTokens: 1024,
  dayTextMaxTokens: 512,
  detailMaxTokens: 312,
  historyMaxTokens: 200,
}

function item(i, text = `偏好条目 ${i}`, extra = {}) {
  return { id: `ent_${i}`, text, w: 1, valid_from: '2026-10-28', updated: '2026-10-28', ...extra }
}

function dayPack(dayChars = 100, detailChars = 0, historyChars = 0) {
  return {
    day_text: '当'.repeat(dayChars),
    detail_text: '细'.repeat(detailChars),
    history_text: '史'.repeat(historyChars),
    tokens: { day: 0, detail: 0, history: 0, total: 0 },
  }
}

test('token 公式与 Python base.estimate_tokens 一致（固定样本）', () => {
  assert.equal(estimateTokens('你好 world'), 4) // 2 CJK + ceil(6/4)=2
  assert.equal(estimateTokens('abcdefgh'), 2)
  assert.equal(estimateTokens(''), 0)
  assert.equal(estimateTokens('价格改成 80'), 5) // 4 CJK + ceil(4/4)=1
})

test('truncateToTokens 结果不超过上限', () => {
  const text = '中'.repeat(500) + 'abc'.repeat(500)
  for (const limit of [0, 1, 10, 100, 512, 3000]) {
    assert.ok(estimateTokens(truncateToTokens(text, limit)) <= limit)
  }
  assert.equal(truncateToTokens('', 10), '')
  assert.equal(truncateToTokens('abc', 0), '')
})

test('固定指令段为常量且 <=128 tokens', () => {
  assert.ok(estimateTokens(SYSTEM_INSTRUCTION) <= 128)
  assert.equal(SYSTEM_INSTRUCTION, SYSTEM_INSTRUCTION)
  assert.match(SYSTEM_INSTRUCTION, /非指令/)
})

test('动态 memory 档位：1024/1536/1792/2048（含优先级）', () => {
  const base = { explicit: false, top1Sim: 0, highWeightCount: 0, hasPinned: false }
  assert.equal(dynamicMemoryBudget(INJECTION, base), 1024)
  assert.equal(dynamicMemoryBudget(INJECTION, { ...base, top1Sim: 0.81 }), 1536)
  assert.equal(dynamicMemoryBudget(INJECTION, { ...base, top1Sim: 0.81, hasPinned: true }), 1792)
  assert.equal(dynamicMemoryBudget(INJECTION, { ...base, top1Sim: 0.99, hasPinned: true, highWeightCount: 3 }), 1792)
  assert.equal(dynamicMemoryBudget(INJECTION, { ...base, explicit: true }), 2048)
})

test('空结果不注入任何标签（零 token 浪费）', () => {
  const result = assembleInjection({
    config: INJECTION,
    memoryItems: [],
    day: null,
    explicit: false,
    top1Sim: 0,
    highWeightCount: 0,
  })
  assert.equal(result.text, '')
  assert.equal(result.tokens.total, 0)
  assert.deepEqual(result.injectedIds, [])
})

test('只有 memory 时注入 memory_context（不含 day_context）', () => {
  const result = assembleInjection({
    config: INJECTION,
    memoryItems: [item(1)],
    day: null,
    explicit: false,
    top1Sim: 0.5,
    highWeightCount: 0,
  })
  assert.match(result.text, /<memory_context/)
  assert.doesNotMatch(result.text, /<day_context/)
  assert.ok(result.tokens.total > 0 && result.tokens.total <= 3000)
  assert.deepEqual(result.injectedIds, ['ent_1'])
})

test('大输入始终 <= 3000 且按优先级截断（history 最先丢）', () => {
  const memoryItems = Array.from({ length: 200 }, (_, index) => item(index, '记'.repeat(60)))
  const result = assembleInjection({
    config: INJECTION,
    memoryItems,
    day: dayPack(600, 400, 300),
    explicit: true, // memory 上限 2048
    top1Sim: 0.95,
    highWeightCount: 2,
  })
  assert.ok(result.tokens.total <= 3000, `total=${result.tokens.total}`)
  assert.ok(result.text.length > 0)
  // 当天内容优先级高于 history：history 被截到 0 或极小
  assert.ok(result.tokens.history <= result.tokens.day)
  // memory 不超过硬顶 2048
  assert.ok(result.tokens.memory <= 2048)
})

test('同一天的 day 文本不超过 512 token，detail<=312，history<=200（day_context 组件和<=1024）', () => {
  const result = assembleInjection({
    config: INJECTION,
    memoryItems: [],
    day: dayPack(2000, 1000, 1000),
    explicit: false,
    top1Sim: 0,
    highWeightCount: 0,
  })
  assert.ok(result.tokens.day <= 512, `day=${result.tokens.day}`)
  assert.ok(result.tokens.detail <= 312, `detail=${result.tokens.detail}`)
  assert.ok(result.tokens.history <= 200, `history=${result.tokens.history}`)
  const dayContext = result.tokens.day + result.tokens.detail + result.tokens.history
  assert.ok(dayContext <= 1024, `day_context=${dayContext}`)
  // 实测总量（含固定指令段与标签包装）也必须在 3000 内
  assert.ok(result.tokens.total <= 3000, `total=${result.tokens.total}`)
})

test('注入文本内的 id 与 injectedIds 严格一致（R5）', () => {
  const items = [item(1), item(2), item(3)]
  const result = assembleInjection({
    config: INJECTION,
    memoryItems: items,
    day: dayPack(10),
    explicit: false,
    top1Sim: 0.2,
    highWeightCount: 0,
  })
  for (const id of result.injectedIds) assert.match(result.text, new RegExp(`#${id}`))
  // 未进入注入文本的 id 不得出现在 injectedIds
  const lines = result.text.split('\n').filter((line) => line.startsWith('[#'))
  assert.equal(lines.length, result.injectedIds.length)
})

test('formatMemoryLine 使用 [#id | w=x | MM-DD 更新] 描述 格式', () => {
  const line = formatMemoryLine({ id: 'ent_x', text: '用户用 pnpm', w: 2.34, updated: '2026-10-28' })
  assert.match(line, /^\[#ent_x \| w=2\.3 \| 10-28 更新\] 用户用 pnpm$/)
})

test('InjectionCache 只缓存非空结果且容量受限', () => {
  const cache = new InjectionCache(2)
  const empty = assembleInjection({ config: INJECTION, memoryItems: [], day: null, explicit: false, top1Sim: 0, highWeightCount: 0 })
  cache.set('a', empty)
  assert.equal(cache.get('a'), undefined)
  const full = assembleInjection({
    config: INJECTION,
    memoryItems: [item(1)],
    day: null,
    explicit: false,
    top1Sim: 0.2,
    highWeightCount: 0,
  })
  cache.set('b', full)
  cache.set('c', full)
  cache.set('d', full)
  assert.equal(cache.get('b'), undefined)
  assert.ok(cache.get('d'))
  cache.clear()
  assert.equal(cache.get('d'), undefined)
})

test('随机 fuzz：任何输入总注入 token 都 <= 3000', () => {
  const randomText = (length, seed) => {
    let state = seed
    const pool = ['中', '文', 'a', 'b', 'c', ' ', '1', '2']
    let out = ''
    for (let i = 0; i < length; i += 1) {
      state = (state * 1103515245 + 12345) % 2147483648
      out += pool[state % pool.length]
    }
    return out
  }
  for (let seed = 1; seed <= 30; seed += 1) {
    const items = Array.from({ length: 40 }, (_, index) => item(index, randomText(30 + (seed % 50), seed * 100 + index)))
    const day = {
      day_text: randomText(1500, seed),
      detail_text: randomText(800, seed + 1),
      history_text: randomText(800, seed + 2),
      tokens: { day: 0, detail: 0, history: 0, total: 0 },
    }
    const result = assembleInjection({
      config: INJECTION,
      memoryItems: items,
      day,
      explicit: seed % 2 === 0,
      top1Sim: (seed % 10) / 10,
      highWeightCount: seed % 3,
    })
    assert.ok(result.tokens.total <= 3000, `seed=${seed} total=${result.tokens.total}`)
    assert.ok(estimateTokens(result.text) <= 3000, `seed=${seed} measured=${estimateTokens(result.text)}`)
  }
})
