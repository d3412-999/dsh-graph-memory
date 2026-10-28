import { assembleInjection, InjectionCache, estimateTokens } from './budgeter.js';
function extractText(content) {
    if (typeof content === 'string')
        return content;
    if (!Array.isArray(content))
        return '';
    const parts = [];
    for (const block of content) {
        if (typeof block === 'string')
            parts.push(block);
        else if (block && typeof block === 'object') {
            const candidate = block;
            if (candidate.type === 'text' && typeof candidate.text === 'string')
                parts.push(candidate.text);
        }
    }
    return parts.join('\n').trim();
}
function sessionIdOf(agent) {
    const id = agent?.session?.id;
    return typeof id === 'string' ? id : String(agent?.id ?? 'unknown-session');
}
async function withTimeout(promise, timeoutMs) {
    let timer;
    try {
        return await Promise.race([
            promise,
            new Promise((resolvePromise) => {
                timer = setTimeout(() => resolvePromise(null), timeoutMs);
            }),
        ]);
    }
    finally {
        if (timer)
            clearTimeout(timer);
    }
}
export class Injector {
    deps;
    cache;
    states = new Map();
    disposeFns = [];
    lastError = '';
    constructor(deps) {
        this.deps = deps;
        this.cache = deps.cache ?? new InjectionCache();
    }
    get lastInjectionError() {
        return this.lastError;
    }
    log(level, message, fields) {
        this.deps.logger?.(level, message, fields);
    }
    stateFor(sessionId) {
        let state = this.states.get(sessionId);
        if (!state) {
            state = {
                sessionId,
                turn: 0,
                step: 0,
                lastQuery: '',
                turnTexts: [],
                explicitTurn: -1,
                pinnedTurns: new Set(),
            };
            this.states.set(sessionId, state);
            if (this.states.size > 256) {
                const oldest = this.states.keys().next().value;
                if (oldest !== undefined)
                    this.states.delete(oldest);
            }
        }
        return state;
    }
    /** 注册 DSH 事件监听（返回释放函数；卸载插件时由 Cordis/插件入口调用）。 */
    install(ctx) {
        const disposers = [];
        // 1) 捕获用户 query / 回合文本（session 级）
        //    只接受真实用户消息（source.kind === 'user'）；DSH 的 runtime-context / time-context /
        //    其他插件注入的 user-role 快照不是用户说的话，混入会污染 query、放大 turn 文本、
        //    甚至把运行时提示词里的普通英文误判成凭据。
        disposers.push(ctx.on('session/event', (session, event) => {
            try {
                const sessionId = String(session?.id ?? '');
                if (!sessionId || !event)
                    return;
                const state = this.stateFor(sessionId);
                if (event.type === 'turn/start') {
                    state.turn = Number(event.data?.turn ?? state.turn + 1);
                    state.step = 0;
                    state.turnTexts = [];
                }
                else if (event.type === 'step/start') {
                    state.step = Number(event.data?.step ?? state.step + 1);
                }
                else if (event.type === 'user/message') {
                    const sourceKind = String(event.data?.source?.kind ?? 'user');
                    if (sourceKind !== 'user')
                        return;
                    const text = extractText(event.data?.content);
                    if (text) {
                        state.lastQuery = text;
                        state.turnTexts.push(`用户：${text}`);
                    }
                }
                else if (event.type === 'assistant/message') {
                    const text = extractText(event.data?.content);
                    if (text)
                        state.turnTexts.push(`助手：${text}`);
                }
                else if (event.type === 'tool/result') {
                    // 工具结果原文不进 turn 文本：闸门 1 的“一句话摘要”由 sidecar noise.py 统一实现，
                    // 插件侧只传对话文本，避免把环境内容/文件原文/凭据误送进记忆管线。
                }
                else if (event.type === 'turn/end') {
                    // turn 文本由 turn-stopping 消费，这里保留到下一 turn/start 清空
                }
            }
            catch {
                /* 事件处理失败绝不阻断会话 */
            }
        }));
        // 2) inbox 插入捕获最新 query（先于 turn/start 发生，保证第一次 assemble 有 query）
        disposers.push(ctx.on('agent/inbox/inserted', (payload) => {
            try {
                const agent = payload?.agent;
                const sourceKind = String(payload?.message?.source?.kind ?? 'user');
                if (sourceKind !== 'user')
                    return;
                const text = extractText(payload?.message?.content);
                if (agent && text) {
                    const state = this.stateFor(sessionIdOf(agent));
                    state.lastQuery = text;
                }
            }
            catch {
                /* ignore */
            }
        }));
        // 3) 捕获当前会话模型路由（sidecar 反向 host/llm 复用）
        disposers.push(ctx.on('agent/request', async (payload, next) => {
            const config = await next();
            try {
                const agent = payload?.agent;
                if (agent && config) {
                    const state = this.stateFor(sessionIdOf(agent));
                    state.lastRoute = { provider: config.provider, model: config.model };
                    state.turn = Number(payload?.turn ?? state.turn);
                    state.step = Number(payload?.step ?? state.step);
                    this.deps.client.setRoute(config.provider, config.model);
                }
            }
            catch {
                /* ignore */
            }
            return config;
        }));
        // 4) 每步注入：system-prompt/assemble waterfall
        disposers.push(ctx.on('system-prompt/assemble', async (assembly, context, next) => {
            const result = (await next());
            try {
                await this.injectIntoAssembly(result ?? assembly, context);
            }
            catch (error) {
                this.lastError = error instanceof Error ? error.message : String(error);
                this.log('warning', 'graph-memory injection bypassed', { error: this.lastError });
            }
            return result ?? assembly;
        }));
        this.disposeFns = disposers;
        return () => {
            for (const dispose of this.disposeFns) {
                try {
                    dispose();
                }
                catch {
                    /* ignore */
                }
            }
            this.disposeFns = [];
        };
    }
    // ------------------------------------------------------------------ #
    // 注入主流程
    // ------------------------------------------------------------------ #
    async injectIntoAssembly(assembly, context) {
        const agent = this.resolveAgent(context);
        if (!agent)
            return;
        const sessionId = sessionIdOf(agent);
        const state = this.stateFor(sessionId);
        const query = this.resolveQuery(agent, state);
        if (!query)
            return;
        const explicit = state.explicitTurn === state.turn;
        const timeout = this.deps.config.sidecar.retrieveTimeoutMs;
        const cacheKey = `${sessionId}:${state.turn}:${state.step}:${explicit ? 'e' : 'n'}:${query}`;
        const cached = this.cache.get(cacheKey);
        if (cached) {
            this.applyInjection(assembly, state, agent, cached);
            return;
        }
        let retrieve = null;
        let day = null;
        try {
            await this.deps.client.ensureStarted();
            const [retrieveResult, dayResult] = await Promise.all([
                withTimeout(this.deps.client.request('retrieve', {
                    query,
                    scope: ['user', 'workspace'],
                    budget: { mem_max: this.deps.config.injection.memoryMaxTokens },
                    explicit,
                    session_id: sessionId,
                    turn: state.turn,
                }, { timeoutMs: timeout }), timeout),
                withTimeout(this.deps.client.request('day_pack', { query, scope: 'workspace' }, { timeoutMs: timeout }), timeout),
            ]);
            retrieve = retrieveResult;
            day = dayResult;
        }
        catch (error) {
            // R7：召回失败/超时 → 旁路放行，不注入 memory/day（零 token），记一次 warning
            this.lastError = error instanceof Error ? error.message : String(error);
            this.log('warning', 'graph-memory retrieve bypassed', { error: this.lastError });
            this.deps.client
                .request('injection_record', {
                session_id: sessionId,
                turn: state.turn,
                tokens: { system: 0, memory: 0, day: 0, detail: 0, history: 0, total: 0 },
                injected_ids: [],
                scope: 'workspace',
                bypassed: true,
                error: this.lastError.slice(0, 200),
            })
                .catch(() => undefined);
            return;
        }
        // day_pack 失败/超时用上一 turn 的缓存（仍是常数预算，只是内容稍旧）
        if (!day && state.dayCache) {
            day = state.dayCache.result;
        }
        else if (day) {
            state.dayCache = { turn: state.turn, result: day };
        }
        const memoryItems = (retrieve?.items ?? []).map((item) => ({
            ...item,
            recently_updated: this.isRecentlyUpdated(item.updated),
        }));
        const assembled = assembleInjection({
            config: this.deps.config.injection,
            memoryItems,
            day,
            explicit: Boolean(explicit),
            top1Sim: retrieve?.top1_sim ?? 0,
            highWeightCount: retrieve?.high_weight_count ?? 0,
        });
        if (!assembled.text)
            return;
        this.cache.set(cacheKey, assembled);
        this.applyInjection(assembly, state, agent, assembled);
        this.afterInjection(state, assembled, explicit);
    }
    applyInjection(assembly, state, agent, assembled) {
        const sections = Array.isArray(assembly.sections) ? assembly.sections : (assembly.sections = []);
        // 同一次 assemble 内重复调用时覆盖，不追加第二份（恒定预算）
        const existingIndex = sections.findIndex((section) => section.name === 'graph-memory');
        const section = { name: 'graph-memory', text: assembled.text, interpolate: false };
        if (existingIndex >= 0)
            sections[existingIndex] = section;
        else
            sections.push(section);
        const record = {
            ts: Math.floor(Date.now() / 1000),
            session_id: state.sessionId,
            turn: state.turn,
            tokens: assembled.tokens,
            injected_ids: assembled.injectedIds,
            scope: 'workspace',
        };
        state.lastInjection = record;
    }
    afterInjection(state, assembled, explicit) {
        const record = state.lastInjection;
        if (!record)
            return;
        // 回传 injected_ids 做 activate（R5：只有真正进入注入的才算）
        if (record.injected_ids.length > 0) {
            this.deps.client.request('activate', { ids: record.injected_ids, reason: 'injected' }).catch(() => undefined);
        }
        this.deps.client
            .request('injection_record', {
            session_id: record.session_id,
            turn: record.turn,
            tokens: record.tokens,
            injected_ids: record.injected_ids,
            scope: record.scope,
            explicit,
        })
            .catch(() => undefined);
    }
    resolveAgent(context) {
        const scope = context?.scope;
        if (scope && typeof scope === 'object' && 'session' in scope) {
            return scope;
        }
        if (context?.agent && typeof context.agent === 'object')
            return context.agent;
        if (scope && typeof scope === 'object' && 'sessionId' in scope) {
            return { id: String(scope.sessionId), session: scope };
        }
        return undefined;
    }
    resolveQuery(agent, state) {
        if (state.lastQuery)
            return state.lastQuery;
        // 兜底：从 session 日志向前扫当前 turn 的用户消息
        try {
            const session = agent.session;
            if (session && typeof session.seq === 'number' && typeof session.eventAt === 'function') {
                const texts = [];
                for (let seq = session.seq - 1; seq >= 0; seq -= 1) {
                    const event = session.eventAt(seq);
                    if (!event)
                        continue;
                    if (event.type === 'turn/start')
                        break;
                    if (event.type === 'user/message') {
                        const text = extractText(event.data?.content);
                        if (text)
                            texts.unshift(text);
                    }
                }
                if (texts.length > 0)
                    return texts.join('\n');
            }
        }
        catch {
            /* ignore */
        }
        return '';
    }
    isRecentlyUpdated(updated) {
        if (updated === undefined || updated === null)
            return false;
        try {
            const timestamp = typeof updated === 'number'
                ? updated * 1000
                : new Date(`${String(updated).slice(0, 10)}T00:00:00Z`).getTime();
            if (!Number.isFinite(timestamp))
                return false;
            return Date.now() - timestamp <= 7 * 24 * 3600 * 1000;
        }
        catch {
            return false;
        }
    }
    // ------------------------------------------------------------------ #
    // 供工具与 turn 生命周期使用
    // ------------------------------------------------------------------ #
    markExplicitRecall(agent) {
        if (!agent)
            return;
        const state = this.stateFor(sessionIdOf(agent));
        state.explicitTurn = state.turn;
    }
    markPinned(agent) {
        if (!agent)
            return;
        const state = this.stateFor(sessionIdOf(agent));
        state.pinnedTurns.add(state.turn);
    }
    stateForAgent(agent) {
        if (!agent)
            return undefined;
        return this.states.get(sessionIdOf(agent));
    }
    /** turn 结束时取出本回合用于 ingest 的文本，并清空缓冲。 */
    consumeTurnText(agent, turn) {
        if (!agent)
            return { text: '', pinned: false };
        const state = this.stateFor(sessionIdOf(agent));
        const text = state.turnTexts.join('\n').trim();
        const pinned = state.pinnedTurns.has(turn);
        state.turnTexts = [];
        state.pinnedTurns.delete(turn);
        state.lastQuery = '';
        return { text, pinned };
    }
    lastInjectionOf(agent) {
        return this.stateForAgent(agent)?.lastInjection;
    }
    clearCache() {
        this.cache.clear();
    }
    dispose() {
        for (const dispose of this.disposeFns) {
            try {
                dispose();
            }
            catch {
                /* ignore */
            }
        }
        this.disposeFns = [];
        this.states.clear();
        this.cache.clear();
    }
}
export function injectionTokenSummary(assembled) {
    const parts = Object.entries(assembled.tokens)
        .filter(([key]) => key !== 'total')
        .map(([key, value]) => `${key}=${value}`);
    return `${parts.join(' ')} total=${assembled.tokens.total}/${3000} (${estimateTokens(assembled.text)} measured)`;
}
