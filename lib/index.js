import { normalizeConfig, resolvePaths } from './config.js';
import { Injector } from './injector.js';
import { SidecarClient } from './sidecarClient.js';
import { installTools } from './toolBridge.js';
export const name = 'graph-memory';
export const inject = ['tools'];
let runtime = null;
function makeLogger(config) {
    return (level, message, fields) => {
        const order = { debug: 10, info: 20, warning: 30, error: 40 };
        const threshold = order[config.logging.level] ?? 20;
        if ((order[level] ?? 20) < threshold)
            return;
        const line = `[graph-memory] ${message}${fields ? ' ' + JSON.stringify(fields) : ''}`;
        if (level === 'error')
            console.error(line);
        else if (level === 'warning')
            console.warn(line);
        else
            console.log(line);
    };
}
function currentAgentFromEvent(payload) {
    const value = payload;
    if (value && typeof value === 'object') {
        if (value.agent && typeof value.agent === 'object')
            return value.agent;
        if ('session' in value)
            return value;
    }
    return undefined;
}
function sleep(ms) {
    return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}
/** 等待 pending 集合内的 Promise 落定（有界，失败吞掉）。 */
async function settlePending(pending, timeoutMs) {
    const snapshot = Array.from(pending);
    if (snapshot.length === 0)
        return;
    await Promise.race([Promise.allSettled(snapshot).then(() => undefined), sleep(timeoutMs)]);
}
/**
 * 从 DSH 当前会话模型做一次 one-shot completion（sidecar 的 host/llm 反向调用）。
 * 使用 `ctx.llm.stream`（公开低层 API），失败交由 sidecar 降级到 offline 规则。
 */
async function hostLlmComplete(ctx, client, logger, params) {
    const llm = ctx.llm;
    if (!llm || typeof llm.stream !== 'function')
        throw new Error('ctx.llm unavailable');
    const route = client.getRoute();
    const provider = route?.provider;
    const model = route?.model;
    if (!provider || !model)
        throw new Error('no active model route captured yet');
    const prompt = String(params.prompt ?? '');
    const system = String(params.system ?? '');
    const maxTokens = Number(params.max_tokens ?? 1024);
    const messages = [
        { role: 'user', content: [{ type: 'text', text: prompt }] },
    ];
    try {
        let text = '';
        for await (const chunk of llm.stream({
            provider,
            model,
            system: system || undefined,
            messages,
            maxTokens,
            temperature: 0.1,
            purpose: 'memory',
        })) {
            if (chunk && chunk.type === 'text-delta' && typeof chunk.text === 'string')
                text += chunk.text;
            if (text.length > 20000)
                break;
        }
        return text;
    }
    catch (error) {
        logger('warning', 'host llm completion failed', { error: error instanceof Error ? error.message : String(error) });
        throw error;
    }
}
export function apply(ctx, rawConfig) {
    const config = normalizeConfig(rawConfig);
    if (!config.enabled) {
        console.log('[graph-memory] disabled by config');
        return () => undefined;
    }
    const logger = makeLogger(config);
    const workspaceRoot = String(config.workspaceRoot ?? rawConfig?.workspaceRoot ?? process.cwd());
    const paths = resolvePaths(config, workspaceRoot);
    const client = new SidecarClient({
        config,
        paths,
        workspaceRoot,
        logger,
        onHostLlm: (params) => hostLlmComplete(ctx, client, logger, params),
    });
    const injector = new Injector({ client, config, logger });
    const state = {
        config,
        client,
        injector,
        toolDisposers: [],
        pending: new Set(),
        lastSweepDay: '',
        disposed: false,
    };
    runtime = state;
    logger('info', 'graph-memory loaded (lazy sidecar, hard injection cap 3000 tokens)', {
        userDataHome: paths.userDataHome,
        workspaceDataHome: paths.workspaceDataHome,
        llmMode: config.llm.mode,
    });
    const track = (promise) => {
        state.pending.add(promise);
        promise
            .catch((error) => logger('warning', 'background memory task failed', { error: String(error) }))
            .finally(() => state.pending.delete(promise));
    };
    // ---------------------------------------------------------------- install
    const disposeListeners = injector.install(ctx);
    state.toolDisposers = installTools(ctx, { client, injector, config, logger });
    const today = () => {
        const date = new Date();
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    };
    // 每日首次会话：矛盾兜底扫描（只报告，不自动改）
    const maybeDailySweep = () => {
        const day = today();
        if (state.lastSweepDay === day)
            return;
        state.lastSweepDay = day;
        track(client
            .request('conflicts_sweep', { scope: 'workspace', days: 30 }, { timeoutMs: config.sidecar.ingestTimeoutMs })
            .then((report) => {
            logger('info', 'daily conflict sweep finished', {
                checked: report?.checked,
                conflicts: report?.conflicts,
            });
        }));
    };
    // turn 结束：异步 ingest + 维护轮次（有界等待，保证 headless 退出前落盘）
    const onTurnStopping = async (payload) => {
        if (state.disposed)
            return;
        const agent = currentAgentFromEvent(payload);
        const turn = Number(payload?.turn ?? 0);
        try {
            const { text, pinned } = injector.consumeTurnText(agent, turn);
            if (!text)
                return;
            const sessionId = String(agent?.session?.id ?? 'unknown-session');
            const turnId = `${sessionId}:${turn}`;
            maybeDailySweep();
            const ingestPromise = client.request('ingest', {
                turn_text: text,
                turn_id: turnId,
                scope: 'workspace',
                session_id: sessionId,
                pinned,
            }, { timeoutMs: config.sidecar.ingestTimeoutMs });
            track(ingestPromise);
            // 有界等待：保证回合结束前记忆尽量落盘（超时也绝不阻断对话）
            await Promise.race([ingestPromise.catch(() => undefined), sleep(config.ingest.flushOnTurnEndMs)]);
            track(client.request('maintenance', { scope: 'workspace' }, { timeoutMs: config.sidecar.requestTimeoutMs * 5 }));
        }
        catch (error) {
            logger('warning', 'turn-end ingest bypassed', { error: error instanceof Error ? error.message : String(error) });
        }
    };
    const disposeTurn = ctx.on('agent/turn-stopping', onTurnStopping);
    // 会话销毁 / 插件卸载：flush + 停止 sidecar
    const cleanup = async () => {
        if (state.disposed)
            return;
        state.disposed = true;
        logger('info', 'graph-memory unloading: flushing memory and stopping sidecar');
        try {
            disposeListeners();
        }
        catch {
            /* ignore */
        }
        for (const dispose of state.toolDisposers) {
            try {
                dispose();
            }
            catch {
                /* ignore */
            }
        }
        state.toolDisposers = [];
        try {
            disposeTurn();
        }
        catch {
            /* ignore */
        }
        injector.dispose();
        await settlePending(state.pending, 2500);
        await client.stop();
        runtime = null;
    };
    // Cordis 生命周期：dispose 事件在插件卸载时触发
    let disposeLifecycle = () => undefined;
    try {
        disposeLifecycle = ctx.on('dispose', () => {
            void cleanup();
        });
    }
    catch {
        /* 宿主不暴露 dispose 事件时依赖 cordis effect 卸载 */
    }
    // 双保险：cordis effect 清理
    try {
        const effect = ctx.effect;
        if (typeof effect === 'function') {
            effect(() => async () => {
                await cleanup();
            });
        }
    }
    catch {
        /* ignore */
    }
    return () => {
        void cleanup();
        void disposeLifecycle();
    };
}
