/**
 * dsh-graph-memory 插件入口（Cordis 生命周期）。
 *
 * 加载后的自主行为：
 * 1. 每个模型 step 之前（system-prompt/assemble）注入恒定预算的 memory/day 上下文；
 * 2. turn 结束后异步 ingest 本回合对话（失败重试 + 死信，不阻塞对话）；
 * 3. 每 turn 推进维护轮次，按图谱规模门控执行 compress / prune 两阶段；
 * 4. 每日首次会话触发 30 天矛盾兜底扫描；
 * 5. 注册 memory_pin / memory_update / memory_forget / memory_recall / memory_stats 工具。
 *
 * 卸载时撤销监听与工具、flush 数据、停止 Sidecar，保留数据文件。
 */
import type { GraphMemoryConfig } from './config.js'
import { normalizeConfig, resolvePaths } from './config.js'
import { Injector } from './injector.js'
import { SidecarClient } from './sidecarClient.js'
import { installTools } from './toolBridge.js'
import type { AgentLike, PluginContext } from './types.js'

export const name = 'graph-memory'
export const inject = ['tools'] as const

interface RuntimeState {
  config: GraphMemoryConfig
  client: SidecarClient
  injector: Injector
  toolDisposers: Array<() => void>
  pending: Set<Promise<unknown>>
  lastSweepDay: string
  disposed: boolean
}

let runtime: RuntimeState | null = null

function makeLogger(config: GraphMemoryConfig): (level: string, message: string, fields?: Record<string, unknown>) => void {
  return (level, message, fields) => {
    const order: Record<string, number> = { debug: 10, info: 20, warning: 30, error: 40 }
    const threshold = order[config.logging.level] ?? 20
    if ((order[level] ?? 20) < threshold) return
    const line = `[graph-memory] ${message}${fields ? ' ' + JSON.stringify(fields) : ''}`
    if (level === 'error') console.error(line)
    else if (level === 'warning') console.warn(line)
    else console.log(line)
  }
}

function currentAgentFromEvent(payload: unknown): AgentLike | undefined {
  const value = payload as Record<string, any> | undefined
  if (value && typeof value === 'object') {
    if (value.agent && typeof value.agent === 'object') return value.agent as AgentLike
    if ('session' in value) return value as AgentLike
  }
  return undefined
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms))
}

/** 等待 pending 集合内的 Promise 落定（有界，失败吞掉）。 */
async function settlePending(pending: Set<Promise<unknown>>, timeoutMs: number): Promise<void> {
  const snapshot = Array.from(pending)
  if (snapshot.length === 0) return
  await Promise.race([Promise.allSettled(snapshot).then(() => undefined), sleep(timeoutMs)])
}

/**
 * 从 DSH 当前会话模型做一次 one-shot completion（sidecar 的 host/llm 反向调用）。
 * 使用 `ctx.llm.stream`（公开低层 API），失败交由 sidecar 降级到 offline 规则。
 */
async function hostLlmComplete(
  ctx: PluginContext,
  client: SidecarClient,
  logger: (level: string, message: string, fields?: Record<string, unknown>) => void,
  params: Record<string, any>,
): Promise<string> {
  const llm = (ctx as any).llm
  if (!llm || typeof llm.stream !== 'function') throw new Error('ctx.llm unavailable')
  const route = client.getRoute()
  const provider = route?.provider
  const model = route?.model
  if (!provider || !model) throw new Error('no active model route captured yet')
  const prompt = String(params.prompt ?? '')
  const system = String(params.system ?? '')
  const maxTokens = Number(params.max_tokens ?? 1024)
  const messages: Array<Record<string, unknown>> = [
    { role: 'user', content: [{ type: 'text', text: prompt }] },
  ]
  try {
    let text = ''
    for await (const chunk of llm.stream({
      provider,
      model,
      system: system || undefined,
      messages,
      maxTokens,
      temperature: 0.1,
      purpose: 'memory',
    })) {
      if (chunk && chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
      if (text.length > 20000) break
    }
    return text
  } catch (error) {
    logger('warning', 'host llm completion failed', { error: error instanceof Error ? error.message : String(error) })
    throw error
  }
}

export function apply(ctx: PluginContext, rawConfig?: unknown): (() => void) | void {
  const config = normalizeConfig(rawConfig)
  if (!config.enabled) {
    console.log('[graph-memory] disabled by config')
    return () => undefined
  }
  const logger = makeLogger(config)
  const workspaceRoot = String((config as any).workspaceRoot ?? (rawConfig as any)?.workspaceRoot ?? process.cwd())
  const paths = resolvePaths(config, workspaceRoot)
  const client: SidecarClient = new SidecarClient({
    config,
    paths,
    workspaceRoot,
    logger,
    onHostLlm: (params: Record<string, any>): Promise<string> => hostLlmComplete(ctx, client, logger, params),
  })
  const injector = new Injector({ client, config, logger })
  const state: RuntimeState = {
    config,
    client,
    injector,
    toolDisposers: [],
    pending: new Set(),
    lastSweepDay: '',
    disposed: false,
  }
  runtime = state

  logger('info', 'graph-memory loaded (lazy sidecar, hard injection cap 3000 tokens)', {
    userDataHome: paths.userDataHome,
    workspaceDataHome: paths.workspaceDataHome,
    llmMode: config.llm.mode,
  })

  const track = (promise: Promise<unknown>): void => {
    state.pending.add(promise)
    promise
      .catch((error) => logger('warning', 'background memory task failed', { error: String(error) }))
      .finally(() => state.pending.delete(promise))
  }

  // ---------------------------------------------------------------- install
  const disposeListeners = injector.install(ctx)
  state.toolDisposers = installTools(ctx, { client, injector, config, logger })

  const today = (): string => {
    const date = new Date()
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  }

  // 每日首次会话：矛盾兜底扫描（只报告，不自动改）
  const maybeDailySweep = (): void => {
    const day = today()
    if (state.lastSweepDay === day) return
    state.lastSweepDay = day
    track(
      client
        .request(
          'conflicts_sweep',
          { scope: 'workspace', days: 30 },
          { timeoutMs: config.sidecar.ingestTimeoutMs },
        )
        .then((report: any) => {
          logger('info', 'daily conflict sweep finished', {
            checked: report?.checked,
            conflicts: report?.conflicts,
          })
        }),
    )
  }

  // turn 结束：异步 ingest + 维护轮次（有界等待，保证 headless 退出前落盘）
  const onTurnStopping = async (payload: any): Promise<void> => {
    if (state.disposed) return
    const agent = currentAgentFromEvent(payload)
    const turn = Number(payload?.turn ?? 0)
    try {
      const { text, pinned } = injector.consumeTurnText(agent, turn)
      if (!text) return
      const sessionId = String(agent?.session?.id ?? 'unknown-session')
      const turnId = `${sessionId}:${turn}`
      maybeDailySweep()
      const ingestPromise = client.request(
        'ingest',
        {
          turn_text: text,
          turn_id: turnId,
          scope: 'workspace',
          session_id: sessionId,
          pinned,
        },
        { timeoutMs: config.sidecar.ingestTimeoutMs },
      )
      track(ingestPromise)
      // 有界等待：保证回合结束前记忆尽量落盘（超时也绝不阻断对话）
      await Promise.race([ingestPromise.catch(() => undefined), sleep(config.ingest.flushOnTurnEndMs)])
      track(
        client.request('maintenance', { scope: 'workspace' }, { timeoutMs: config.sidecar.requestTimeoutMs * 5 }),
      )
    } catch (error) {
      logger('warning', 'turn-end ingest bypassed', { error: error instanceof Error ? error.message : String(error) })
    }
  }

  const disposeTurn = ctx.on('agent/turn-stopping', onTurnStopping)

  // 会话销毁 / 插件卸载：flush + 停止 sidecar
  const cleanup = async (): Promise<void> => {
    if (state.disposed) return
    state.disposed = true
    logger('info', 'graph-memory unloading: flushing memory and stopping sidecar')
    try {
      disposeListeners()
    } catch {
      /* ignore */
    }
    for (const dispose of state.toolDisposers) {
      try {
        dispose()
      } catch {
        /* ignore */
      }
    }
    state.toolDisposers = []
    try {
      disposeTurn()
    } catch {
      /* ignore */
    }
    injector.dispose()
    await settlePending(state.pending, 2500)
    await client.stop()
    runtime = null
  }

  // Cordis 生命周期：dispose 事件在插件卸载时触发
  let disposeLifecycle: () => void = () => undefined
  try {
    disposeLifecycle = ctx.on('dispose', () => {
      void cleanup()
    })
  } catch {
    /* 宿主不暴露 dispose 事件时依赖 cordis effect 卸载 */
  }

  // 双保险：cordis effect 清理
  try {
    const effect = (ctx as any).effect
    if (typeof effect === 'function') {
      effect(() => async () => {
        await cleanup()
      })
    }
  } catch {
    /* ignore */
  }

  return () => {
    void cleanup()
    void disposeLifecycle()
  }
}
