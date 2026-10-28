# 架构与实现说明

> 内部实现契约（数据模型/JSON-RPC 方法/模块接口/预算算法）见 [CONTRACT.md](CONTRACT.md)。
> 本文件解释“为什么这样实现”，以及 DSH 集成点、状态机与关键取舍。

## 1. 总体形态

```
DSH (Cordis)                          dsh-graph-memory
┌─────────────────────────┐          ┌──────────────────────────────────────┐
│ system-prompt assemble   │◄──注入───│ src/injector.ts  (±3000 token, R1/R5)│
│ tools registry           │◄──注册───│ src/toolBridge.ts (5 个 memory_*)    │
│ agent/session events     │─────────►│ src/index.ts 生命周期 / turn-end     │
└─────────────────────────┘          │ src/budgeter.ts 本地计量与截断        │
                                     │ src/sidecarClient.ts 懒启动/双传输    │
                                     └───────────────┬──────────────────────┘
                                                     │ 行分隔 JSON-RPC 2.0
                     ┌───────────────────────────────▼──────────────────────────────┐
                     │ sidecar/server.py  (lazy, idle 300s)                          │
                     │  ├─ service.py   业务编排（retrieve/ingest/activate/…）        │
                     │  ├─ graph.py     图谱唯一真相源（实体/关系/属性/向量索引）      │
                     │  ├─ graph_lightrag.py  真实 LightRAG HTTP 适配器               │
                     │  ├─ noise/extract/supersede/weight/retrieval                   │
                     │  ├─ daykeeper.py 天缓冲/全量压缩/单份历史摘要（R8）             │
                     │  ├─ prune.py     规模门控 + 两阶段压缩/剪枝（R4）               │
                     │  ├─ journal.py   SQLite 审计（7 天回滚/死信/注入）             │
                     │  └─ panel.py     127.0.0.1:9629 面板                           │
                     └──────────────────────────────────────────────────────────────┘
                                                     ▲
                                             反向 host/llm
                                     （复用 DSH 当前会话模型，零额外配置）
```

## 2. 注入点：为什么是 `system-prompt/assemble`

DSH 每次 model step 的顺序是：`systemPrompt.assemble()` → `agent/pre-step` → `step/start` → 渲染 prompt → LLM 请求。
`agent/pre-step` 拿不到 assembly、也不允许替换它；而 `system-prompt/assemble` 是瀑布事件，
监听器拿到可修改的 assembly，且 `assembleContextFor(agent, signal)` 会把 **scope=agent** 一并传入。
因此：

```ts
ctx.on('system-prompt/assemble', async (assembly, context, next) => {
  const result = await next()               // 先让其它插件完成装配
  await injectIntoAssembly(result, context) // context.scope 即 Agent，可读 session/模型路由
  return result
})
```

插件把 `<graph_memory_instructions> + <memory_context> + <day_context>` 作为**一个动态 section** push 到 sections 末尾：
- 位于 system prompt 内部 → 在会话历史之前（避免被长历史挤掉注意力）；
- 同一 step 内重复调用会**覆盖**同名 section，而不是追加第二份；
- 每个 step 重新计算，不读取历史注入 → 内存与 token 都不随会话轮数累积（R1）。

### query 捕获
注入发生在 assembly 时，此时本 step 的 user message 可能还在 inbox（尚未 append 到 session）。插件同时监听：
- `agent/inbox/inserted`（先于 turn 开始，捕获第一条用户消息）；
- `session/event` 的 `user/message`，**只接受 `source.kind === 'user'`**。

后者很关键：DSH 的 runtime-context / time-context / 其它插件会注入 user-role 快照消息，
它们不是用户说的话。混入会污染 query、把运行时提示词原文送进记忆管线（我们实测因此触发了凭据误判）。
兜底路径：若状态为空，从 `agent.session` 日志向前扫当前 turn 的真实用户消息。

## 3. 恒定预算（R1）

三个有界容器：固定指令段 ≤128 / memory_context ≤2048（动态 1024/1536/1792/2048）/ day_context ≤1024（512+312+200）。
总硬顶 3000，且**为标签/换行包装预留 96 token**，最终以实测文本 token 为准：

```
剩余 = 3000 - 96
按优先级装填：system → 当天原文/摘要 → 补召回 → memory → 历史摘要
超出时从低优先级开始丢：历史 → memory → 补召回 → 当天
```

token 计量公式在 TS/Python 两侧逐字节一致（CJK 字符 1 token，其余 4 字符 1 token 向上取整），
`tests/python/test_tokens.py` 通过 `node scripts/emit-tokens.mjs` 做跨语言交叉校验。
「memory 富余不回补 day」由装填顺序保证：memory 用它的固定上限，未用满的余量只可能被后面的 detail/history 使用；
「day 富余可回补 memory」不实现（契约明确禁止反向挤占当天上下文）。

## 4. 写入闭环

`agent/turn-stopping`（回合已结束、答案已交付）：
1. 取本回合收集的 `用户：… / 助手：…` 文本（工具结果原文不进入管线，闸门 1 的摘要由 sidecar 统一实现）；
2. `ingest` RPC（有界等待 `flushOnTurnEndMs=2500`，保证 headless 退出前落盘；失败只记 warning）；
3. `maintenance` RPC（推进维护轮次，按图谱规模门控决定是否 compress/prune，fire-and-forget）。

Sidecar 的 ingest 流程：
```
当天原文落盘（无论是否进图）→ 四道闸门 → 抽取（LLM，重试 2 次，失败入死信）
→ 逐候选：改口判定（先于同名合并）→ >0.95 去重/激活 → 同名合并 → 新建
→ 共现关系 → Journal → 图落盘
```
- 闸门 3 的顺序是 **先放行信号、后丢弃判定**（宁可多抽一次，不可误杀一条记忆）；
- 抽取没有实体但整句含否定/改口信号时，走 `_supersede_from_utterance` 兜底
  （C 用例 Day2 “现在不喜欢了”靠它命中最近邻并覆盖式改写，`new_value` 由 LLM 生成、失败用“喜欢→不喜欢”确定性规则）；
- 死信：`dead_letter_list` / `dead_letter_replay` 可查看与重放。

## 5. 权重与激活（R5）

- 惰性衰减：`w(t)=min(10, w_last·2^(-Δt/7d))`，读时计算，无定时任务；
- 激活只发生在 `injected_ids` 上（真正进入注入文本的条目），公式 `w=min(10,w+0.5·(1+boost))`；
- `boost = 1.0·pinned + 1.0·tool_boosted + 0.2·workspace_hit`；
- score = `sim·(0.3+0.7·min(1,w/5))`，pinned×1.5，workspace scope ×1.2；
- 新建 3 天保护、改口后 protect_until=+7d，期间不参与剪枝；
- compressed 状态节点被激活后自动复活（active）。

## 6. 改口（记忆可变）

两道防线：
1. **写入时**：候选实体描述向量近邻 top-5 → 相似度落在 `[0.62, 0.95)` 才请 LLM 判
   `{is_same_attribute, is_contradiction, confidence}`；≥0.7（含否定/改口词 0.6）触发改写。
2. **每日首次会话**：`conflicts_sweep` 扫近 30 天活跃节点的矛盾对，产出 conflict_report（只报告，不自动改写）。

改写严格六步（`sidecar/supersede.py::apply_supersede`）：
`edit_entity(description) → valid_from/updated_at → w=max(旧,新) 不清零 → 重算向量（R6）→ Journal before/after → protect_until+7d`。
不同 scope 不冲突（user 偏好与 workspace 项目知识各自保留）。

## 7. 日缓冲与恒定 day_context（R8）

- 每个 turn 的原文写入 `days/<date>/turns.jsonl`（永不因压缩删除）；
- 当天原文 >512 token → **全量压缩**（一次通读当天全部原文 + 已有摘要，压到 ≤360 token，保留最近 2 条原文）；
  限流：距上次压缩 <30min 或 <20 轮则跳过；原文仍保留供补召回；
- `day_pack` = 当天摘要/原文（≤512）+ 当天 turn chunks 向量补召回 top3（≤312）+ 历史滚动摘要（≤200）；
- 跨天：`history = LLM(旧 history + 当日 summary)`，写回**唯一** `days/history_summary.json` 并清空重来；
- 压缩/跨天合并可能调 LLM（慢），service.day_pack 把它们放后台任务，绝不阻塞 300ms 注入预算。

## 8. 剪枝（R4 + 规模门控）

核心原则：**不为遗忘而遗忘，只为空间而遗忘**。时间本身不是删除理由，图谱容量才是开关。

- 门控（唯一开关=规模）：`count < high` 完全不剪（权重照常衰减、排序变低，但随时可召回/激活回升）；
  `> high` 剪到 `<= low`；`> hard` 强制剪+告警。默认 workspace 20000/16000/50000，user 5000/4000/12000。
- 双水位迟滞：`low = high×0.8`，一次剪到 low 防止阈值附近抖动。
- 轮次状态机（`sidecar/prune.py`）：
  - `round_tick()` 递增维护轮次；
  - 第 N 轮 `compress()`：`w<0.6` 且非保护节点描述压到约 40%、`status=compressed`，**零删除**；相似度 >0.9 碎片先 LLM merge；
  - 第 N+1 轮起 `prune()`：仅 `status=compressed && w<0.3 && age>3d` 才归档（先剪边再归档点）；
    冷区滞留 30 天且期间未命中才真删；单轮最多处理 8% 节点；连续 3 轮剪不到 low 停止并告警；
  - `dry_run` 默认 true（手动调用只出报告）；自动维护在 compress 之后的轮次才用 `dry_run=False`。
- 免疫：pinned / Preference / 近 7 天改过 / protect_until / 新建 3 天 / 活跃工作区命中。
- 自适应：每日记录检索 P95；连续 3 天 >150ms 且未到 high → high×0.8；长期 <50ms 且逼近 high → ×1.2；
  夹逼 `[2000, 校准 high×3]`。上界建议用 `scripts/calibrate.py` 实测反推（`high = P95<150ms 的最大档 × 0.7`）。

## 9. Sidecar 进程与传输

- 懒启动：第一次 retrieve/ingest 才 spawn；空闲 `idleExitSeconds=300` 无请求自动退出（先 flush 落盘）；
- 崩溃重启：10 分钟最多 3 次，超限本次对话旁路记忆（不影响对话）；
- **传输层**：生产默认 `stdio`（红线要求的形态）。若宿主在受限沙箱里（例如 DSH agent 的 workspace-write 沙箱），
  Node 用 piped stdio spawn 子进程会 `EPERM`；`transport: auto` 会自动降级为 **loopback TCP**
  （`127.0.0.1` 随机端口 + 一次性 token，端口/token 写入 `<workspaceDataHome>/sidecar.runtime.json`），
  协议仍是同一套行分隔 JSON-RPC 2.0。真实 DSH headless E2E 即通过该降级通道跑通。
- 反向调用 `host/llm`：sidecar 需要 LLM 时向宿主发 JSON-RPC 请求，宿主用 `ctx.llm.stream`（当前会话模型）执行；
  宿主不可用/超时 → sidecar 降级到 offline 确定性规则，任何情况下都不阻塞对话（R7 精神）。

## 10. LightRAG 复用与 embedded 兼容

- `graph.backend=lightrag`：`graph_lightrag.py` 以 HTTP 调真实 LightRAG 服务，
  复用其 `POST /insert`（抽取/去重）、`POST /query(mode=mix, top_k=20, only_need_context=true)`（双层检索），
  图操作映射到 `/graph/entity/create|edit|delete`、`/graph/relation/*`、`/graph/entities/merge`；
  权重/时间等元数据写入 `node_data` 自定义属性（默认 NetworkX/JSON 图存储保留，Neo4j 固定 schema 会丢，需换存储）。
- `graph.backend=embedded`（默认）：不 fork LightRAG，而是在仓库内实现一份 **LightRAG-compatible 语义**的
  单机图存储：实体/关系/属性 + 确定性哈希稀疏向量 + 倒排索引 + 双层近似检索，
  保证“没有 LightRAG 服务时插件仍完整可用、全部验收可离线跑”。它是图谱唯一真相源（R2），
  向量索引是可重建的派生结构，不是第二份可写副本。

## 11. 与 DSH 会话日志的关系

注入记录通过 `injection_record` 写入 sidecar Journal（`injections` 表：时间/会话/轮次/token 分布/injected_ids），
面板的预算仪表盘与 `memory_stats` 都读它。DSH 的会话日志本身可见注入后的 system prompt 替换。
DSH 公开 API 不允许第三方插件向会话日志追加自定义事件类型（`appendPluginRecord` 仅对 experimental 包开放），
因此“Trajectory 视图追溯某条回答用了哪几条记忆”通过面板/`memory_recall`/Journal 提供，不写自定义会话事件。

## 12. 卸载

`ctx.on('dispose')` + Cordis `effect` 双保险：撤销工具注册、事件监听，flush 待落盘任务（有界 2.5s），
`client.stop()` 发送 `shutdown` 并等待进程退出，删除 `sidecar.runtime.json`；**数据文件保留**。
`scripts/uninstall-plugin.ps1` 从 profile 移除 bundle；E2E 断言卸载后无插件日志、无残留 sidecar 进程、数据仍在。
