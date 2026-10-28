/**
 * Injector 行为测试（R1 / R5 / R7）：
 * - 成功路径：query 捕获 → retrieve/day_pack → 注入 section → 只对 injected_ids 调 activate；
 * - 失败路径（R7）：sidecar 启动失败/检索抛错 → 不注入、不抛错、对话继续；
 * - 超时路径（R7）：检索 hang 住时 300ms 预算内旁路返回。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Injector } from '../../lib/injector.js'
import { DEFAULT_CONFIG, normalizeConfig } from '../../lib/config.js'

const AGENT = { id: 'agent-1', session: { id: 'session-1' } }

function makeConfig(overrides = {}) {
  const config = normalizeConfig(overrides)
  config.sidecar.retrieveTimeoutMs = 60
  return config
}

function makeCtx() {
  const handlers = {}
  return {
    handlers,
    on(event, listener) {
      handlers[event] = listener
      return () => {
        delete handlers[event]
      }
    },
    tools: { register: () => () => undefined },
  }
}

function makeClient(overrides = {}) {
  const calls = []
  return {
    calls,
    ensureStarted: async () => undefined,
    isRunning: () => true,
    setRoute: () => undefined,
    getRoute: () => ({ provider: 'test', model: 'test-model' }),
    async request(method, params = {}) {
      calls.push({ method, params })
      if (overrides[method]) return overrides[method](params)
      switch (method) {
        case 'retrieve':
          return {
            items: [
              {
                id: 'ent_1',
                text: '用户用 pnpm 管理依赖',
                w: 2.0,
                pinned: true,
                updated: '2026-10-28',
                sim: 0.91,
                scope: 'workspace',
              },
            ],
            injected_ids: ['ent_1'],
            tokens: { memory: 18 },
            top1_sim: 0.91,
            high_weight_count: 1,
          }
        case 'day_pack':
          return {
            day_text: '用户：我用 pnpm 9.15 管理依赖',
            detail_text: '',
            history_text: '',
            tokens: { day: 14, detail: 0, history: 0, total: 14 },
          }
        case 'activate':
          return { updated: (params.ids || []).map((id) => ({ id, w: 2.6 })) }
        default:
          return { ok: true }
      }
    },
    notify: () => undefined,
  }
}

async function runAssembly(ctx, injector) {
  const assembly = { sections: [] }
  const handler = ctx.handlers['system-prompt/assemble']
  assert.equal(typeof handler, 'function')
  const result = await handler(assembly, { scope: AGENT }, async () => assembly)
  return result
}

function captureQuery(ctx) {
  ctx.handlers['agent/inbox/inserted']({
    agent: AGENT,
    message: { source: { kind: 'user' }, content: [{ type: 'text', text: '上次我说过用什么包管理器' }] },
  })
}

test('成功路径：注入一个 section + 只对 injected_ids activate（R5）', async () => {
  const ctx = makeCtx()
  const client = makeClient()
  const injector = new Injector({ client, config: makeConfig(), logger: () => undefined })
  const dispose = injector.install(ctx)
  try {
    captureQuery(ctx)
    const assembly = await runAssembly(ctx, injector)
    const section = assembly.sections.find((item) => item.name === 'graph-memory')
    assert.ok(section, 'must inject graph-memory section')
    assert.match(section.text, /<memory_context/)
    assert.match(section.text, /pnpm/)
    assert.equal(section.interpolate, false)
    const activate = client.calls.find((call) => call.method === 'activate')
    assert.ok(activate, 'activate must be called')
    assert.deepEqual(activate.params.ids, ['ent_1'])
    const record = client.calls.find((call) => call.method === 'injection_record')
    assert.ok(record)
    assert.equal(record.params.tokens.total <= 3000, true)
    assert.deepEqual(record.params.injected_ids, ['ent_1'])
  } finally {
    dispose()
  }
})

test('R7：retrieve 抛错时旁路，不注入、不抛错、对话继续', async () => {
  const ctx = makeCtx()
  const client = makeClient({
    retrieve: () => Promise.reject(new Error('sidecar exploded')),
    day_pack: () => Promise.reject(new Error('sidecar exploded')),
  })
  const injector = new Injector({ client, config: makeConfig(), logger: () => undefined })
  const dispose = injector.install(ctx)
  try {
    captureQuery(ctx)
    const assembly = await runAssembly(ctx, injector) // 不应 throw
    assert.equal(assembly.sections.find((item) => item.name === 'graph-memory'), undefined)
    const record = client.calls.find((call) => call.method === 'injection_record')
    assert.ok(record, 'bypass 也记录一次注入尝试')
    assert.equal(record.params.bypassed, true)
  } finally {
    dispose()
  }
})

test('R7：ensureStarted 失败时旁路（sidecar 未启动/被拔出）', async () => {
  const ctx = makeCtx()
  const client = makeClient()
  client.ensureStarted = async () => {
    throw new Error('spawn EPERM')
  }
  const injector = new Injector({ client, config: makeConfig(), logger: () => undefined })
  const dispose = injector.install(ctx)
  try {
    captureQuery(ctx)
    const started = Date.now()
    const assembly = await runAssembly(ctx, injector)
    assert.ok(Date.now() - started < 2000)
    assert.equal(assembly.sections.find((item) => item.name === 'graph-memory'), undefined)
  } finally {
    dispose()
  }
})

test('R7：检索 hang 住时在超时预算内旁路返回（不阻塞对话）', async () => {
  const ctx = makeCtx()
  const never = () => new Promise(() => undefined)
  const client = makeClient({ retrieve: never, day_pack: never })
  const injector = new Injector({ client, config: makeConfig(), logger: () => undefined })
  const dispose = injector.install(ctx)
  try {
    captureQuery(ctx)
    const started = Date.now()
    const assembly = await runAssembly(ctx, injector)
    const elapsed = Date.now() - started
    assert.ok(elapsed < 1500, `bypass must be fast, got ${elapsed}ms`)
    assert.equal(assembly.sections.find((item) => item.name === 'graph-memory'), undefined)
  } finally {
    dispose()
  }
})

test('空 memory + 空 day：不注入任何标签（零 token 浪费）', async () => {
  const ctx = makeCtx()
  const client = makeClient({
    retrieve: () => Promise.resolve({ items: [], injected_ids: [], top1_sim: 0, high_weight_count: 0 }),
    day_pack: () => Promise.resolve({ day_text: '', detail_text: '', history_text: '', tokens: { day: 0, detail: 0, history: 0, total: 0 } }),
  })
  const injector = new Injector({ client, config: makeConfig(), logger: () => undefined })
  const dispose = injector.install(ctx)
  try {
    captureQuery(ctx)
    const assembly = await runAssembly(ctx, injector)
    assert.equal(assembly.sections.find((item) => item.name === 'graph-memory'), undefined)
    assert.equal(client.calls.some((call) => call.method === 'activate'), false)
  } finally {
    dispose()
  }
})

test('DEFAULT_CONFIG：retrieveTimeoutMs 默认 300ms（R7 硬指标）', () => {
  assert.equal(DEFAULT_CONFIG.sidecar.retrieveTimeoutMs, 300)
})
