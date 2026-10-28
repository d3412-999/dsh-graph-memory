/**
 * Sidecar 客户端：懒启动、JSON-RPC 通信、空闲退出、反向 host/llm 调用、失败旁路。
 *
 * 传输层：
 * - 默认 `stdio`（红线形态：本地 JSON-RPC over stdio）；
 * - `auto`（默认值）：先尝试 stdio；若宿主被沙箱限制（Node 用 piped stdio spawn 子进程
 *   会 EPERM，例如 DSH 的 workspace-write 沙箱），自动降级到 **loopback TCP**，
 *   协议仍是同一套行分隔 JSON-RPC 2.0，仅传输层不同（127.0.0.1 随机端口 + 一次性 token，
 *   端口/token 通过 `<workspaceDataHome>/sidecar.runtime.json` 交给宿主）。
 *
 * 红线 R7：任何调用失败/超时都只抛出可被上层捕获的错误，绝不阻断对话。
 */
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, unlinkSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { connect } from 'node:net';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { sidecarConfigPayload } from './config.js';
const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MAX_RESTARTS_WINDOW_MS = 10 * 60 * 1000;
const RUNTIME_FILE = 'sidecar.runtime.json';
export class SidecarClient {
    options;
    child = null;
    socket = null;
    transport = 'stdio';
    buffer = '';
    pending = new Map();
    nextId = 1;
    startPromise = null;
    restartTimes = [];
    stopping = false;
    privateRoute = {};
    _state = 'stopped';
    constructor(options) {
        this.options = options;
    }
    get state() {
        return this._state;
    }
    get activeTransport() {
        return this.transport;
    }
    getRoute() {
        return this.privateRoute;
    }
    setRoute(provider, model) {
        this.privateRoute = { provider, model };
    }
    setHostLlm(handler) {
        this.options.onHostLlm = handler;
    }
    isRunning() {
        return this._state === 'ready' && (this.child !== null || this.socket !== null);
    }
    log(level, message, fields) {
        try {
            this.options.logger?.(level, message, fields);
        }
        catch {
            /* 日志失败不得影响主流程 */
        }
    }
    // ------------------------------------------------------------------ #
    // 启动
    // ------------------------------------------------------------------ #
    async ensureStarted(timeoutMs = 12000) {
        if (this._state === 'ready' && (this.child || this.socket))
            return;
        if (this.startPromise) {
            await this.startPromise;
            return;
        }
        this.startPromise = this.start(timeoutMs);
        try {
            await this.startPromise;
        }
        finally {
            this.startPromise = null;
        }
    }
    candidates() {
        const configured = this.options.config.sidecar.pythonPath;
        const raw = configured && configured.trim() ? [configured] : this.options.config.sidecar.pythonCandidates;
        const candidates = [];
        for (const candidate of raw) {
            const parts = candidate.trim().split(/\s+/);
            if (parts.length === 0 || !parts[0])
                continue;
            candidates.push({ command: parts[0], args: parts.slice(1) });
        }
        return candidates;
    }
    baseArgs() {
        const configPayload = sidecarConfigPayload(this.options.config, this.options.paths, this.options.workspaceRoot);
        return [
            join(PLUGIN_ROOT, 'sidecar', 'server.py'),
            '--user-home',
            this.options.paths.userDataHome,
            '--workspace-home',
            this.options.paths.workspaceDataHome,
            '--workspace-root',
            this.options.workspaceRoot,
            '--config',
            JSON.stringify(configPayload),
            '--panel-port',
            String(this.options.config.sidecar.panelPort),
        ];
    }
    env() {
        const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
        if (this.options.config.llm.mode === 'scripted' && this.options.config.llm.scriptFile) {
            env.GRAPH_MEMORY_LLM_SCRIPT = this.options.config.llm.scriptFile;
        }
        env.GRAPH_MEMORY_LOG_FILE = join(this.options.paths.workspaceDataHome, 'sidecar.log');
        return env;
    }
    async start(timeoutMs) {
        const now = Date.now();
        this.restartTimes = this.restartTimes.filter((time) => now - time < MAX_RESTARTS_WINDOW_MS);
        if (this.restartTimes.length >= this.options.config.sidecar.maxRestartsPer10Min) {
            this._state = 'failed';
            throw new Error('graph-memory sidecar restart limit reached; bypassing memory for this turn');
        }
        const serverPath = join(PLUGIN_ROOT, 'sidecar', 'server.py');
        if (!existsSync(serverPath)) {
            this._state = 'failed';
            throw new Error(`graph-memory sidecar not found: ${serverPath}`);
        }
        const mode = this.options.config.sidecar.transport ?? 'auto';
        let lastError = null;
        if (mode !== 'tcp') {
            for (const candidate of this.candidates()) {
                try {
                    await this.spawnStdio(candidate, timeoutMs);
                    this.restartTimes.push(Date.now());
                    this.transport = 'stdio';
                    this._state = 'ready';
                    return;
                }
                catch (error) {
                    lastError = error instanceof Error ? error : new Error(String(error));
                    const message = lastError.message;
                    if (!/EPERM|EACCES|ENOENT|not found/i.test(message)) {
                        this.log('warning', 'graph-memory stdio sidecar failed', { command: candidate.command, error: message });
                    }
                }
            }
        }
        if (mode !== 'stdio') {
            try {
                await this.startTcp(timeoutMs);
                this.restartTimes.push(Date.now());
                this.transport = 'tcp';
                this._state = 'ready';
                this.log('info', 'graph-memory sidecar started (loopback tcp fallback; stdio unavailable under this host sandbox)');
                return;
            }
            catch (error) {
                lastError = error instanceof Error ? error : new Error(String(error));
                this.log('warning', 'graph-memory tcp sidecar failed', { error: lastError.message });
            }
        }
        this._state = 'failed';
        throw lastError ?? new Error('graph-memory sidecar failed to start');
    }
    // --------------------------- stdio ------------------------------ #
    spawnStdio(candidate, timeoutMs) {
        return new Promise((resolvePromise, rejectPromise) => {
            let settled = false;
            let child;
            try {
                child = spawn(candidate.command, [...candidate.args, ...this.baseArgs()], {
                    cwd: PLUGIN_ROOT,
                    windowsHide: true,
                    env: this.env(),
                    stdio: ['pipe', 'pipe', 'pipe'],
                });
            }
            catch (error) {
                rejectPromise(error instanceof Error ? error : new Error(String(error)));
                return;
            }
            const timer = setTimeout(() => finishErr(new Error(`sidecar stdio start timed out after ${timeoutMs}ms`)), timeoutMs);
            const finishOk = () => {
                if (settled)
                    return;
                settled = true;
                clearTimeout(timer);
                resolvePromise();
            };
            const finishErr = (error) => {
                if (settled)
                    return;
                settled = true;
                clearTimeout(timer);
                try {
                    child.kill();
                }
                catch {
                    /* ignore */
                }
                rejectPromise(error);
            };
            child.once('error', (error) => finishErr(error instanceof Error ? error : new Error(String(error))));
            child.once('exit', (code, signal) => {
                this.onChildExit(child);
                if (!settled)
                    finishErr(new Error(`sidecar exited during startup (code=${code ?? 'null'} signal=${signal ?? 'null'})`));
            });
            child.stderr.setEncoding('utf8');
            child.stderr.on('data', (chunk) => {
                for (const line of String(chunk).split(/\r?\n/)) {
                    if (line.trim())
                        this.log('debug', 'sidecar', { line: line.trim().slice(0, 500) });
                }
            });
            child.stdout.setEncoding('utf8');
            child.stdout.on('data', (chunk) => this.onChunk(child, chunk));
            child.stdin.on('error', () => {
                /* EPIPE on exit */
            });
            this.child = child;
            this.socket = null;
            void this.waitReadyStdio(child, timeoutMs).then(finishOk, finishErr);
        });
    }
    async waitReadyStdio(child, timeoutMs) {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeoutMs) {
            if (child.exitCode !== null || child.signalCode !== null)
                throw new Error('sidecar exited before ready');
            if (this.child !== child)
                throw new Error('sidecar replaced before ready');
            try {
                const result = await this.sendRequestRaw('ping', {}, 500);
                if (result && result.pong)
                    return;
            }
            catch {
                /* not ready yet */
            }
            await new Promise((r) => setTimeout(r, 120));
        }
        throw new Error('sidecar did not answer ping within timeout');
    }
    // ---------------------------- tcp ------------------------------- #
    async startTcp(timeoutMs) {
        const token = randomBytes(18).toString('hex');
        const runtimePath = join(this.options.paths.workspaceDataHome, RUNTIME_FILE);
        try {
            unlinkSync(runtimePath);
        }
        catch {
            /* ignore */
        }
        let lastError = null;
        for (const candidate of this.candidates()) {
            let child = null;
            try {
                child = spawn(candidate.command, [...candidate.args, ...this.baseArgs(), '--transport', 'tcp', '--token', token], {
                    cwd: PLUGIN_ROOT,
                    windowsHide: true,
                    env: this.env(),
                    stdio: 'ignore',
                    detached: true,
                });
                const spawnError = await new Promise((resolvePromise) => {
                    const onError = (error) => {
                        child?.removeListener('exit', onExit);
                        resolvePromise(error);
                    };
                    const onExit = () => {
                        child?.removeListener('error', onError);
                        resolvePromise(new Error(`sidecar tcp process exited during startup (${candidate.command})`));
                    };
                    child.once('error', onError);
                    child.once('exit', onExit);
                    setTimeout(() => {
                        child?.removeListener('error', onError);
                        child?.removeListener('exit', onExit);
                        resolvePromise(null);
                    }, 400);
                });
                if (spawnError)
                    throw spawnError;
                const tcpChild = child;
                this.child = tcpChild;
                tcpChild.once('exit', () => this.onChildExit(tcpChild));
                const runtime = await this.waitRuntimeFile(runtimePath, token, timeoutMs);
                const socket = await this.connectSocket(runtime.host, runtime.port, timeoutMs);
                this.socket = socket;
                socket.on('data', (chunk) => this.onChunk(tcpChild, chunk.toString('utf8')));
                socket.on('error', (error) => this.log('warning', 'sidecar tcp socket error', { error: error.message }));
                socket.on('close', () => this.onSocketClose());
                await this.sendRequestRaw('auth', { token }, 3000);
                const result = await this.sendRequestRaw('ping', {}, 3000);
                if (!result || !result.pong)
                    throw new Error('sidecar tcp ping failed');
                return;
            }
            catch (error) {
                lastError = error instanceof Error ? error : new Error(String(error));
                if (child) {
                    try {
                        child.kill();
                    }
                    catch {
                        /* ignore */
                    }
                }
                this.child = null;
                this.socket = null;
            }
        }
        throw lastError ?? new Error('sidecar tcp start failed');
    }
    async waitRuntimeFile(runtimePath, token, timeoutMs) {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeoutMs) {
            try {
                const raw = JSON.parse(readFileSync(runtimePath, 'utf8'));
                if (raw.transport === 'tcp' && raw.token === token && Number.isInteger(raw.port) && raw.port > 0) {
                    return { host: String(raw.host ?? '127.0.0.1'), port: Number(raw.port) };
                }
            }
            catch {
                /* not written yet */
            }
            await new Promise((r) => setTimeout(r, 120));
        }
        throw new Error('sidecar tcp runtime file not ready');
    }
    connectSocket(host, port, timeoutMs) {
        return new Promise((resolvePromise, rejectPromise) => {
            const socket = connect({ host, port });
            const timer = setTimeout(() => {
                socket.destroy();
                rejectPromise(new Error(`sidecar tcp connect timeout (${timeoutMs}ms)`));
            }, timeoutMs);
            socket.once('connect', () => {
                clearTimeout(timer);
                socket.setNoDelay(true);
                resolvePromise(socket);
            });
            socket.once('error', (error) => {
                clearTimeout(timer);
                rejectPromise(error instanceof Error ? error : new Error(String(error)));
            });
        });
    }
    // ------------------------------------------------------------------ #
    // 生命周期 / 消息
    // ------------------------------------------------------------------ #
    onChildExit(child) {
        if (this.child !== child)
            return;
        this.child = null;
        this.socket?.destroy();
        this.socket = null;
        if (this._state !== 'stopped')
            this._state = 'stopped';
        const error = new Error('graph-memory sidecar exited');
        for (const [, call] of this.pending) {
            clearTimeout(call.timer);
            call.reject(error);
        }
        this.pending.clear();
    }
    onSocketClose() {
        this.socket = null;
        if (this._state === 'stopped')
            return;
        this._state = 'stopped';
        const error = new Error('graph-memory sidecar connection closed');
        for (const [, call] of this.pending) {
            clearTimeout(call.timer);
            call.reject(error);
        }
        this.pending.clear();
    }
    onChunk(_child, chunk) {
        this.buffer += chunk;
        let index = this.buffer.indexOf('\n');
        while (index >= 0) {
            const line = this.buffer.slice(0, index).trim();
            this.buffer = this.buffer.slice(index + 1);
            if (line)
                this.onMessage(line);
            index = this.buffer.indexOf('\n');
        }
        if (this.buffer.length > 8 * 1024 * 1024)
            this.buffer = '';
    }
    onMessage(line) {
        let message;
        try {
            message = JSON.parse(line);
        }
        catch {
            return;
        }
        if (typeof message.method === 'string') {
            void this.handleHostRequest(message);
            return;
        }
        const id = message.id;
        if (!Number.isInteger(id))
            return;
        const call = this.pending.get(id);
        if (!call)
            return;
        this.pending.delete(id);
        clearTimeout(call.timer);
        if (message.error) {
            const error = new Error(String(message.error.message ?? 'sidecar rpc error'));
            error.code = message.error.code;
            call.reject(error);
        }
        else {
            call.resolve(message.result);
        }
    }
    async handleHostRequest(message) {
        const respond = (payload) => {
            this.writeMessage({ jsonrpc: '2.0', id: message.id, ...payload });
        };
        try {
            if (message.method === 'host/llm') {
                const params = (message.params ?? {});
                if (!this.options.onHostLlm)
                    throw new Error('host/llm handler not attached');
                const text = await this.options.onHostLlm(params);
                respond({ result: { text } });
            }
            else if (message.method === 'host/log') {
                const params = message.params ?? {};
                this.log(String(params.level ?? 'info'), String(params.message ?? ''));
                respond({ result: { ok: true } });
            }
            else {
                respond({ error: { code: -32601, message: `unknown host method: ${message.method}` } });
            }
        }
        catch (error) {
            respond({ error: { code: -32603, message: error instanceof Error ? error.message : String(error) } });
        }
    }
    writeMessage(payload) {
        const data = JSON.stringify(payload) + '\n';
        try {
            if (this.socket && !this.socket.destroyed) {
                this.socket.write(data);
                return;
            }
            if (this.child && 'stdin' in this.child && this.child.stdin?.writable) {
                ;
                this.child.stdin.write(data);
            }
        }
        catch (error) {
            this.log('warning', 'sidecar write failed', { error: error instanceof Error ? error.message : String(error) });
        }
    }
    sendRequestRaw(method, params, timeoutMs) {
        if (!this.child && !this.socket)
            return Promise.reject(new Error('graph-memory sidecar is not running'));
        const id = this.nextId++;
        return new Promise((resolvePromise, rejectPromise) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                rejectPromise(new Error(`graph-memory rpc timeout: ${method} (${timeoutMs}ms)`));
            }, timeoutMs);
            this.pending.set(id, { resolve: resolvePromise, reject: rejectPromise, timer });
            this.writeMessage({ jsonrpc: '2.0', id, method, params });
        });
    }
    async request(method, params = {}, options = {}) {
        if (!options.skipStart)
            await this.ensureStarted();
        return (await this.sendRequestRaw(method, params, options.timeoutMs ?? this.options.config.sidecar.requestTimeoutMs));
    }
    notify(method, params = {}) {
        this.writeMessage({ jsonrpc: '2.0', method, params });
    }
    async completeViaHost(prompt, system, maxTokens) {
        if (!this.options.onHostLlm)
            throw new Error('host llm bridge not attached');
        return await this.options.onHostLlm({ prompt, system, max_tokens: maxTokens, purpose: 'memory' });
    }
    // ------------------------------------------------------------------ #
    // 关闭
    // ------------------------------------------------------------------ #
    async stop() {
        this.stopping = true;
        const child = this.child;
        const socket = this.socket;
        if (!child && !socket) {
            this._state = 'stopped';
            return;
        }
        try {
            await this.sendRequestRaw('shutdown', {}, 800);
        }
        catch {
            /* ignore */
        }
        await new Promise((resolvePromise) => {
            const timer = setTimeout(() => {
                try {
                    socket?.destroy();
                    child?.kill();
                }
                catch {
                    /* ignore */
                }
                resolvePromise();
            }, 2000);
            if (socket)
                socket.once('close', () => {
                    clearTimeout(timer);
                    resolvePromise();
                });
            else if (child)
                child.once('exit', () => {
                    clearTimeout(timer);
                    resolvePromise();
                });
            else {
                clearTimeout(timer);
                resolvePromise();
            }
        });
        this.child = null;
        this.socket = null;
        this._state = 'stopped';
        this.stopping = false;
        for (const [, call] of this.pending)
            clearTimeout(call.timer);
        this.pending.clear();
        try {
            unlinkSync(join(this.options.paths.workspaceDataHome, RUNTIME_FILE));
        }
        catch {
            /* ignore */
        }
    }
}
