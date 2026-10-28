/**
 * dsh-graph-memory — 插件共享类型与最小 DSH 上下文声明。
 *
 * 设计约束：本插件在运行时只依赖 DSH 提供的 Cordis 上下文能力，**不 import 任何
 * @deepseek-ai/* 运行时模块**，因此既能以 npm 包安装，也能以 `link:` 本地目录安装
 * （link 安装时 Node 的 ESM 解析不会回退到 DSH 安装目录）。
 */

export type Scope = 'user' | 'workspace'

export interface InjectionTokens {
  system: number
  memory: number
  day: number
  detail: number
  history: number
  total: number
}

export interface MemoryItem {
  id: string
  text: string
  w: number
  valid_from?: string
  updated?: string | number
  sim?: number
  pinned?: boolean
  scope?: Scope
  recently_updated?: boolean
  rendered?: string
  token_cost?: number
}

export interface RetrieveResult {
  items: MemoryItem[]
  injected_ids: string[]
  tokens: { memory: number }
  top1_sim: number
  high_weight_count: number
}

export interface DayPackResult {
  day_text: string
  detail_text: string
  history_text: string
  tokens: { day: number; detail: number; history: number; total: number }
}

export interface InjectionRecord {
  ts: number
  session_id: string
  turn: number
  tokens: InjectionTokens
  injected_ids: string[]
  scope: Scope
}

/** Cordis 上下文的最小子集（运行时由 DSH 提供，类型仅本地使用）。 */
export interface PluginContext {
  on(event: string, listener: (...args: any[]) => any, options?: { prepend?: boolean }): () => void
  emit?(event: string, ...args: any[]): void
  tools?: {
    register(definition: Record<string, any>): () => void
  }
  llm?: {
    stream(options: Record<string, any>): AsyncIterable<Record<string, any>>
  }
  logger?: (level: string, message: string) => void
  [key: string]: any
}

export interface PromptAssembly {
  sections: Array<{ name: string; text: string; interpolate?: boolean }>
  contexts?: Array<{ name: string; text: string; interpolate?: boolean }>
  tools?: unknown[]
  variables?: Record<string, string | undefined>
}

export interface AgentLike {
  id?: string
  session?: SessionLike
  [key: string]: any
}

export interface SessionLike {
  id?: string
  seq?: number
  header?: { cwd?: string }
  requestHeader?(): Record<string, any> | undefined
  [key: string]: any
}

export interface AssembleContextLike {
  scope?: unknown
  agent?: AgentLike
  signal?: AbortSignal
  [key: string]: any
}
