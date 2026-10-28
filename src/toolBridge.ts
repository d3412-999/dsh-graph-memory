/**
 * 五个 memory_* 工具的注册与实现（原生 ToolDefinition，避免任何运行时 import）。
 *
 * 工具注册发生在 DSH 服务启动时，安装插件后需重启/热重载 profile 才生效。
 * 工具输出必须是 lossless JSON 值；模型可见文本由 render() 生成。
 */
import type { Injector } from './injector.js'
import type { PluginContext } from './types.js'
import type { SidecarClient } from './sidecarClient.js'
import type { GraphMemoryConfig } from './config.js'
import { estimateTokens } from './budgeter.js'

export interface ToolDeps {
  client: SidecarClient
  injector: Injector
  config: GraphMemoryConfig
  logger: (level: string, message: string, fields?: Record<string, unknown>) => void
}

const TOOL_OUTPUT = {
  schema: { type: 'object', additionalProperties: true },
  render: (_args: unknown, value: unknown) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
}

function scopeParam(): Record<string, unknown> {
  return {
    type: 'string',
    enum: ['user', 'workspace'],
    description: '记忆作用域：user=跨项目的用户偏好；workspace=当前项目知识。缺省自动选择。',
  }
}

function asObject(value: unknown): Record<string, any> {
  return typeof value === 'object' && value !== null ? (value as Record<string, any>) : {}
}

function agentOf(exec: unknown): any {
  return asObject(exec).agent
}

export function installTools(ctx: PluginContext, deps: ToolDeps): Array<() => void> {
  if (!ctx.tools || typeof ctx.tools.register !== 'function') {
    deps.logger('warning', 'graph-memory: ctx.tools unavailable, memory_* tools not registered')
    return []
  }
  const disposers: Array<() => void> = []
  const register = (definition: Record<string, any>): void => {
    try {
      disposers.push(ctx.tools!.register(definition))
    } catch (error) {
      deps.logger('warning', 'graph-memory: tool registration failed', {
        tool: definition.name,
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }

  // ---------------------------------------------------------------- pin ---
  register({
    name: 'memory_pin',
    description:
      '把用户明确要求记住的信息写入长期记忆：权重 2.0、pinned=true、永不参与自动剪枝。' +
      '用户说“记住这个/记一下/别忘”时调用；不要把工具结果或代码原文传进来。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        text: { type: 'string', description: '要记住的一句话事实（尽量完整、可独立理解）。' },
        scope: scopeParam(),
      },
      required: ['text'],
    },
    output: TOOL_OUTPUT,
    async execute(args: unknown, exec: unknown): Promise<unknown> {
      const params = asObject(args)
      const text = String(params.text ?? '').trim()
      if (!text) return { ok: false, error: 'text is required' }
      const agent = agentOf(exec)
      const scope = params.scope === 'workspace' || params.scope === 'user' ? params.scope : undefined
      deps.injector.markPinned(agent)
      try {
        const result = await deps.client.request(
          'ingest',
          {
            turn_text: text,
            turn_id: `pin-${Date.now()}`,
            scope: scope ?? 'user',
            pinned: true,
            tool_boosted: true,
            session_id: agent?.session?.id,
          },
          { timeoutMs: deps.config.sidecar.ingestTimeoutMs },
        )
        return { ok: true, scope: scope ?? 'user', node_ids: result?.node_ids ?? [], pinned: true, weight: 2.0 }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  })

  // ------------------------------------------------------------- update ---
  register({
    name: 'memory_update',
    description:
      '覆盖式改写一条已有记忆（用户改口/纠正时使用）：先检索最匹配的记忆，再把描述改成 new_value，' +
      '同步更新向量库、valid_from 与权重（不清零），并记录可回滚的变更历史。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string', description: '定位旧记忆的检索词。' },
        new_value: { type: 'string', description: '新的说法/值（完整描述）。' },
        scope: scopeParam(),
      },
      required: ['query', 'new_value'],
    },
    output: TOOL_OUTPUT,
    async execute(args: unknown): Promise<unknown> {
      const params = asObject(args)
      try {
        const result = await deps.client.request(
          'update',
          {
            query: String(params.query ?? ''),
            new_value: String(params.new_value ?? ''),
            scope: params.scope ?? 'auto',
          },
          { timeoutMs: deps.config.sidecar.ingestTimeoutMs },
        )
        return { ok: true, ...asObject(result) }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  })

  // ------------------------------------------------------------- forget ---
  register({
    name: 'memory_forget',
    description:
      '请求忘记一条记忆。不会立即物理删除（遵守“先压缩、隔轮剪枝后归档、超期才真删”的安全流程），' +
      '而是标记为 pending_forget 并在后续维护轮次中归档，7 天内可在面板回滚。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        id: { type: 'string', description: '记忆 id（ent_...）。' },
        query: { type: 'string', description: '或用于定位记忆的检索词。' },
        scope: scopeParam(),
      },
    },
    output: TOOL_OUTPUT,
    async execute(args: unknown): Promise<unknown> {
      const params = asObject(args)
      try {
        const result = await deps.client.request(
          'forget',
          { id: params.id, query: params.query, scope: params.scope ?? 'auto' },
          { timeoutMs: deps.config.sidecar.requestTimeoutMs },
        )
        return { ok: true, ...asObject(result) }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  })

  // ------------------------------------------------------------- recall ---
  register({
    name: 'memory_recall',
    description:
      '显式召回记忆（会触发一次 2048 token 上限的记忆注入并返回最相关条目）。' +
      'as_of 可选：查某天（YYYY-MM-DD）之前的版本历史，用于“我上次是怎么说的”。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string', description: '要召回什么。' },
        scope: { type: 'string', enum: ['user', 'workspace', 'auto'], description: '作用域，缺省 auto。' },
        as_of: { type: 'string', description: '可选历史日期 YYYY-MM-DD，查该日期之前的版本。' },
      },
      required: ['query'],
    },
    output: TOOL_OUTPUT,
    async execute(args: unknown, exec: unknown): Promise<unknown> {
      const params = asObject(args)
      const agent = agentOf(exec)
      deps.injector.markExplicitRecall(agent)
      try {
        const retrievResult = await deps.client.request(
          'retrieve',
          {
            query: String(params.query ?? ''),
            scope: params.scope === 'auto' ? ['user', 'workspace'] : [params.scope ?? 'workspace'],
            explicit: true,
            budget: { mem_max: deps.config.injection.memoryMaxTokens },
          },
          { timeoutMs: deps.config.sidecar.requestTimeoutMs * 3 },
        )
        const items = Array.isArray(retrievResult?.items) ? retrievResult.items : []
        if (params.as_of) {
          const journal = await deps.client.request(
            'journal_list',
            { scope: params.scope === 'user' ? 'user' : 'workspace' },
            { timeoutMs: deps.config.sidecar.requestTimeoutMs },
          )
          const entries = (Array.isArray(journal?.entries) ? journal.entries : []).filter((entry: any) => {
            const ts = Number(entry?.ts ?? 0)
            const asOf = new Date(`${String(params.as_of)}T23:59:59Z`).getTime() / 1000
            return Number.isFinite(asOf) && ts <= asOf
          })
          return {
            ok: true,
            as_of: params.as_of,
            historical_versions: entries
              .filter((entry: any) => entry.kind === 'supersede' || entry.kind === 'update')
              .slice(0, 10)
              .map((entry: any) => ({ target_id: entry.target_id, before: entry.before, after: entry.after, ts: entry.ts })),
            current_items: items.map((item: any) => ({
              id: item.id,
              w: item.w,
              text: item.rendered ?? item.text,
              sim: item.sim,
            })),
          }
        }
        return {
          ok: true,
          items: items.map((item: any) => ({
            id: item.id,
            w: item.w,
            text: item.rendered ?? item.text,
            sim: item.sim,
            valid_from: item.valid_from,
          })),
          injected_ids: retrievResult?.injected_ids ?? [],
        }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  })

  // -------------------------------------------------------------- stats ---
  register({
    name: 'memory_stats',
    description: '查看记忆系统的运行状态：两套图谱的节点/边规模、剪枝水位与阶段、注入 token 占用、死信数量。',
    parameters: { type: 'object', additionalProperties: false, properties: {} },
    output: TOOL_OUTPUT,
    async execute(): Promise<unknown> {
      try {
        const stats = asObject(
          await deps.client.request('stats', {}, { timeoutMs: deps.config.sidecar.requestTimeoutMs * 2 }),
        )
        let panel: Record<string, any> = {}
        try {
          panel = asObject(
            await deps.client.request('panel_state', {}, { timeoutMs: deps.config.sidecar.requestTimeoutMs }),
          )
        } catch {
          panel = {}
        }
        return {
          ok: true,
          ...stats,
          injection_last: panel.injection_last ?? null,
          hard_cap_tokens: deps.config.injection.totalHardTokens,
          token_formula: 'CJK chars = 1 token each; other chars = 1 token / 4 chars (ceil)',
        }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }
    },
  })

  return disposers
}
