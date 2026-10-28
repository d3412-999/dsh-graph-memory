/**
 * 配置解析 / scope 选择 / sidecar 覆盖载荷 node 测试。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_CONFIG,
  deepMerge,
  normalizeConfig,
  pickScope,
  resolvePaths,
  sidecarConfigPayload,
} from '../../lib/config.js'

test('默认配置满足硬性预算（总数 3000、各容器上限）', () => {
  const config = normalizeConfig(undefined)
  assert.equal(config.injection.totalHardTokens, 3000)
  assert.equal(config.injection.systemMaxTokens, 128)
  assert.equal(config.injection.memoryMaxTokens, 2048)
  assert.equal(config.injection.memoryDefaultTokens, 1024)
  assert.equal(config.injection.dayMaxTokens, 1024)
  assert.equal(config.injection.dayTextMaxTokens, 512)
  assert.equal(config.injection.detailMaxTokens, 312)
  assert.equal(config.injection.historyMaxTokens, 200)
  assert.equal(config.sidecar.idleExitSeconds, 300)
  assert.equal(config.sidecar.transport, 'auto')
  assert.equal(config.sidecar.panelPort, 9629)
  assert.equal(config.llm.mode, 'host')
})

test('deepMerge 深合并且不修改基准默认值', () => {
  const merged = deepMerge({ a: { b: 1, c: 2 } }, { a: { c: 3 }, d: 4 })
  assert.deepEqual(merged, { a: { b: 1, c: 3 }, d: 4 })
  assert.equal(DEFAULT_CONFIG.injection.totalHardTokens, 3000)
})

test('normalizeConfig 把 totalHardTokens 夹紧到 [3000, 2000000]（R1 硬顶可调但有界）', () => {
  // 区间内的值原样保留——这是用户能自己调的部分
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 99999 } }).injection.totalHardTokens, 99999)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 3000 } }).injection.totalHardTokens, 3000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 2000000 } }).injection.totalHardTokens, 2000000)
  // 越界夹到边界，非法值回落默认 3000：R1 仍然是「恒定上限」而不是「不限」
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 200 } }).injection.totalHardTokens, 3000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 9e9 } }).injection.totalHardTokens, 2000000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 0 } }).injection.totalHardTokens, 3000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: -1 } }).injection.totalHardTokens, 3000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: NaN } }).injection.totalHardTokens, 3000)
  assert.equal(normalizeConfig({ injection: { totalHardTokens: 'abc' } }).injection.totalHardTokens, 3000)
})

test('resolvePaths 默认落在 ~/.dsh/graph-memory 与 {ws}/.dsh/graph-memory', () => {
  const paths = resolvePaths(normalizeConfig(undefined), 'C:\\work\\proj', 'C:\\Users\\someone')
  assert.match(paths.userDataHome.replace(/\\/g, '/'), /C:\/Users\/someone\/\.dsh\/graph-memory$/)
  assert.match(paths.workspaceDataHome.replace(/\\/g, '/'), /C:\/work\/proj\/\.dsh\/graph-memory$/)
})

test('resolvePaths 尊重配置覆盖', () => {
  const config = normalizeConfig({ paths: { userDataHome: 'D:\\gm\\user', workspaceDataHome: 'D:\\gm\\ws' } })
  const paths = resolvePaths(config, 'C:\\ignored', 'C:\\home')
  assert.equal(paths.userDataHome.replace(/\\/g, '/'), 'D:/gm/user')
  assert.equal(paths.workspaceDataHome.replace(/\\/g, '/'), 'D:/gm/ws')
})

test('sidecarConfigPayload 携带传输/路径/LLM 覆盖且不泄露无关字段', () => {
  const config = normalizeConfig({ llm: { mode: 'offline', timeoutMs: 1234 }, sidecar: { transport: 'tcp' } })
  const paths = resolvePaths(config, 'C:\\ws')
  const payload = sidecarConfigPayload(config, paths, 'C:\\ws')
  assert.equal(payload.llm.mode, 'offline')
  assert.equal(payload.llm.timeoutMs, 1234)
  assert.equal(payload.sidecar.transport, undefined) // transport 通过 CLI 参数传给 sidecar
  assert.ok(payload.paths.userDataHome)
  assert.equal(payload.workspaceRoot, 'C:\\ws')
})

test('pickScope：偏好/决策进 user，项目知识进 workspace', () => {
  assert.equal(pickScope('我喜欢用 pnpm'), 'user')
  assert.equal(pickScope('我不喜欢华而不实的方案'), 'user')
  assert.equal(pickScope('我们要用 TypeScript'), 'user')
  assert.equal(pickScope('记得明天下午三点开会'), 'user')
  assert.equal(pickScope('这个项目的接口在 config.yaml 里'), 'workspace')
  assert.equal(pickScope('改用 Redis 做缓存'), 'workspace')
  assert.equal(pickScope('价格改成 80'), 'workspace')
})
