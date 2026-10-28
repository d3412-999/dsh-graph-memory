/**
 * 插件配置：默认值与路径解析。
 *
 * 配置来源优先级：Cordis 行 config（profile patch） > 环境变量 > 内置默认值。
 * Sidecar 自身配置由 sidecar/config.py 的 config/default.json 提供；这里只负责
 * 宿主侧需要的字段，并把 sidecar 需要的覆盖项合并成一份 JSON 传给子进程。
 */
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import type { Scope } from './types.js'

export interface GraphMemoryConfig {
  enabled: boolean
  sidecar: {
    pythonPath?: string | null
    pythonCandidates: string[]
    transport: 'auto' | 'stdio' | 'tcp'
    idleExitSeconds: number
    requestTimeoutMs: number
    retrieveTimeoutMs: number
    ingestTimeoutMs: number
    maxRestartsPer10Min: number
    panelPort: number
    panelHost: string
    scriptPath: string
  }
  paths: {
    userDataHome?: string | null
    workspaceDataHome?: string | null
  }
  graph: {
    backend: 'embedded' | 'lightrag'
    lightragUrl?: string | null
    lightragApiKey?: string | null
  }
  llm: {
    mode: 'host' | 'offline' | 'scripted' | 'openai'
    timeoutMs: number
    scriptFile?: string | null
    baseUrl?: string | null
    apiKeyEnv?: string
    model?: string | null
  }
  injection: {
    totalHardTokens: number
    systemMaxTokens: number
    memoryMaxTokens: number
    memoryDefaultTokens: number
    memoryHighSimTokens: number
    memoryBoostTokens: number
    memoryExplicitTokens: number
    highSimThreshold: number
    highWeightThreshold: number
    dayMaxTokens: number
    dayTextMaxTokens: number
    detailMaxTokens: number
    historyMaxTokens: number
  }
  ingest: { asyncIngest: boolean; flushOnTurnEndMs: number; maxTurnChars: number }
  /** 成本估算用的官方单价（人民币/百万 token），仅用于面板展示，不参与计费。 */
  pricing: {
    model: string
    currency: string
    inputCacheMissPerM: number
    inputCacheHitPerM: number
    outputPerM: number
    tier: string
    note: string
  }
  logging: { level: string }
  _raw?: Record<string, unknown>
}

export const DEFAULT_CONFIG: GraphMemoryConfig = {
  enabled: true,
  sidecar: {
    pythonPath: null,
    pythonCandidates: ['py -3', 'python3', 'python'],
    transport: 'auto',
    idleExitSeconds: 300,
    requestTimeoutMs: 4000,
    retrieveTimeoutMs: 300,
    ingestTimeoutMs: 60000,
    maxRestartsPer10Min: 3,
    panelPort: 9629,
    panelHost: '127.0.0.1',
    scriptPath: '',
  },
  paths: { userDataHome: null, workspaceDataHome: null },
  graph: { backend: 'embedded', lightragUrl: null, lightragApiKey: null },
  llm: { mode: 'host', timeoutMs: 60000, scriptFile: null },
  injection: {
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
  },
  ingest: { asyncIngest: true, flushOnTurnEndMs: 2500, maxTurnChars: 12000 },
  pricing: {
    model: 'deepseek-flash',
    currency: 'CNY',
    inputCacheMissPerM: 2,
    inputCacheHitPerM: 0.04,
    outputPerM: 8,
    tier: 'peak',
    note: 'deepseek-flash 高峰价（北京时间周一至周五 9:00-12:00 / 14:00-18:00）；其余时段半价',
  },
  logging: { level: 'info' },
}

/** 注入预算的可调区间：默认 3000，可调到 2M（红线 R1 从「固定 3000」放宽为「用户设定值」）。 */
export const TOTAL_BUDGET_MIN = 3000
export const TOTAL_BUDGET_MAX = 2_000_000
export const TOTAL_BUDGET_DEFAULT = 3000

export function clampTotalBudget(value: unknown): number {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return TOTAL_BUDGET_DEFAULT
  return Math.min(TOTAL_BUDGET_MAX, Math.max(TOTAL_BUDGET_MIN, Math.round(numeric)))
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function deepMerge<T extends Record<string, any>>(base: T, overlay: unknown): T {
  // 逐层深拷贝：分支未被覆盖时也必须返回独立副本，避免调用方修改结果时污染 DEFAULT_CONFIG
  const result: Record<string, any> = JSON.parse(JSON.stringify(base))
  if (!isObject(overlay)) return result as T
  for (const [key, value] of Object.entries(overlay)) {
    if (isObject(value) && isObject(result[key])) {
      result[key] = deepMerge(result[key], value)
    } else if (value !== undefined) {
      result[key] = JSON.parse(JSON.stringify(value))
    }
  }
  return result as T
}

export function normalizeConfig(raw: unknown): GraphMemoryConfig {
  const config = deepMerge(DEFAULT_CONFIG, raw)
  if (config.sidecar.idleExitSeconds <= 0) config.sidecar.idleExitSeconds = 300
  config.injection.totalHardTokens = clampTotalBudget(config.injection.totalHardTokens)
  return config
}

export interface ResolvedPaths {
  userDataHome: string
  workspaceDataHome: string
}

export function resolvePaths(config: GraphMemoryConfig, workspaceRoot: string, home = homedir()): ResolvedPaths {
  const userDataHome =
    config.paths.userDataHome && config.paths.userDataHome.trim()
      ? resolve(config.paths.userDataHome)
      : resolve(join(home, '.dsh', 'graph-memory'))
  const workspaceDataHome =
    config.paths.workspaceDataHome && config.paths.workspaceDataHome.trim()
      ? resolve(config.paths.workspaceDataHome)
      : resolve(join(workspaceRoot, '.dsh', 'graph-memory'))
  return { userDataHome, workspaceDataHome }
}

/** 传给 sidecar 的配置覆盖（sidecar 自己也有 config/default.json 默认值）。 */
export function sidecarConfigPayload(
  config: GraphMemoryConfig,
  paths: ResolvedPaths,
  workspaceRoot: string,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    paths: {
      userDataHome: paths.userDataHome,
      workspaceDataHome: paths.workspaceDataHome,
    },
    sidecar: {
      idleExitSeconds: config.sidecar.idleExitSeconds,
      panelPort: config.sidecar.panelPort,
      panelHost: config.sidecar.panelHost,
    },
    graph: {
      backend: config.graph.backend,
      lightragUrl: config.graph.lightragUrl,
      lightragApiKey: config.graph.lightragApiKey,
    },
    llm: {
      mode: config.llm.mode,
      timeoutMs: config.llm.timeoutMs,
      baseUrl: config.llm.baseUrl ?? null,
      apiKeyEnv: config.llm.apiKeyEnv,
      model: config.llm.model ?? null,
    },
    injection: {
      totalHardTokens: config.injection.totalHardTokens,
      systemMaxTokens: config.injection.systemMaxTokens,
      memoryMaxTokens: config.injection.memoryMaxTokens,
      dayMaxTokens: config.injection.dayMaxTokens,
      dayTextMaxTokens: config.injection.dayTextMaxTokens,
      detailMaxTokens: config.injection.detailMaxTokens,
      historyMaxTokens: config.injection.historyMaxTokens,
    },
    ingest: { maxTurnChars: config.ingest.maxTurnChars, flushOnTurnEndMs: config.ingest.flushOnTurnEndMs },
    logging: { level: config.logging.level },
    workspaceRoot,
  }
  return payload
}

/** 根据文本内容决定写入哪个 scope：偏好/决策类进 user，其余进 workspace。 */
export function pickScope(text: string): Scope {
  const userSignals = [
    '我喜欢',
    '我不喜欢',
    '我偏好',
    '我更喜欢',
    '我们要',
    '我想',
    '记得',
    '记住',
    '以后',
    '从现在起',
    '不再',
    '决定',
    'preference',
    'i like',
    'i prefer',
    'remember',
  ]
  const lowered = text.toLowerCase()
  return userSignals.some((signal) => lowered.includes(signal)) ? 'user' : 'workspace'
}
