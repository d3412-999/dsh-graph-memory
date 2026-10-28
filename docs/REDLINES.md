# 红线合规对照（R1–R8）

> “违反任一条即为实现失败”。本表给出每条红线的实现位置、关键设计、验证命令与证据。
> 验收细节见 [VERIFICATION.md](VERIFICATION.md) 与 [ACCEPTANCE-REPORT.md](ACCEPTANCE-REPORT.md)。

## R1 注入 token 总量必须是常数（硬顶 3000）

- **实现**：`src/budgeter.ts` —— 三个有界容器（固定指令段 ≤128 / memory_context ≤2048 / day_context ≤1024）
  相加，硬顶 3000，并预留 96 token 标签包装开销，最终以实测文本 token 为准；与图谱规模、轮数、使用时长无关。
- **不随使用时长增长**：注入是每 step 重新计算的动态 section（同名覆盖），**从不重放/累积历史注入**；
  插件不保存 O(history) 状态（每会话仅一个固定大小 state）。
- **不随图谱规模增长**：召回 top-k 截断 + memory 文本 token 上限截断。
- **验证**：
  - `node tests/node/budgeter.test.mjs`（含 fuzz：任何输入实测文本 ≤3000）；
  - 验收 D1（200 轮）/ D2（30 天×50 轮）/ D3（100→20000 节点）单次注入差值 <5%；
  - 真实 DSH E2E 注入记录 `total=142 ≤ 3000`。
- 代码：`src/budgeter.ts::assembleInjection / dynamicMemoryBudget / WRAPPER_OVERHEAD_TOKENS`。

## R2 图谱是唯一真相源（禁止影子副本/双写）

- **实现**：`sidecar/graph.py`（embedded）或 `sidecar/graph_lightrag.py`（HTTP）是唯一可写图谱；
  `journal.db` 只记审计流水（变更/激活/剪枝/注入），不存储可回放的记忆正文副本；
  `graph/vectors/*.bin` 是从实体描述派生的索引，可随时 `rebuild_vectors()` 重建，不是第二份可写副本；
  `days/*` 是对话原文与摘要（day_context 数据），不是记忆节点副本。
- **验证**：`sidecar/graph.py` 全部 mutation 都写 `self.entities/relations`；`rebuild_vectors()` 可从图谱重建索引；
  `py -3 -m unittest tests.python.test_retrieval`（检索只读图谱）。
- 代码：`graph.py`、`graph_lightrag.py`、`journal.py`、`service.py::_save`。

## R3 权重与时间字段只存节点 property，绝不写 description

- **实现**：`properties = {w, last_activated_at, created_at, updated_at, valid_from, pinned, status,
  protect_until, compressed_at, archived_at, workspace_hit, source_turn_ids, source_chunk_ids, embed_version}`；
  all mutation 只动 `properties`；`description` 只写描述语义。
- **LightRAG 后端**：properties 写入 `node_data` 自定义属性（要求图存储保留属性，见 INSTALL 限制）。
- **验证**：`tests/python/test_weight.py`、`test_supersede.py` 断言 description 不含 `w=`/日期元数据；
  `tests/python/test_protocol.py` 校验 retrieve/journal 返回结构。
- 代码：`base.py::PROPERTY_KEYS/make_entity`、`graph.py::edit_entity/update_properties`。

## R4 删除不能一步到位：先压缩 → 至少隔一轮剪枝 → 归档 → 超期真删

- **实现**：`sidecar/prune.py` 状态机 —
  `round_tick()` 推进轮次；`compress()` 只把 `w<0.6` 节点压缩到约 40%、`status=compressed`，**零删除**；
  `prune()` 仅在 `round > compress_round` 时归档（`status=compressed && w<0.3 && age>3d`），
  同轮 prune 只返回 dry report；冷区滞留 30 天且未命中才真删。
- **验证**：B（50 条 w<0.6 → compress 轮全压缩零删除；跨轮才出现 archived）与 `tests/python/test_prune.py`；
  真实 DSH E2E 也可见 node���被压缩/归档路径（`prune_reports`）。
- 代码：`prune.py::compress/prune/gate`、`service.py::maintenance`。

## R5 激活只在记忆真正进入注入上下文时发生

- **实现**：`retrieve` 返回候选 → `budgeter.assembleInjection` 按 token 预算真正拼进文本的条目才收集 `injectedIds`
  → `injector.afterInjection` 只对这些 id 调 `activate`；检索到但被截断的不激活；
  `activate` 也从不接受“检索命中的全部 id”。
- **验证**：`tests/node/injector.test.mjs`（成功路径只对 injected_ids activate；检索抛错/启动失败/检索 hang 三种 R7 旁路；空结果零注入）；`tests/node/budgeter.test.mjs`（注入文本中的 id 与 injectedIds 严格一致）；
  `tests/python/test_retrieval.py`；E2E `injections.injected_ids` 与注入文本一致。
- 代码：`src/injector.ts::afterInjection`、`src/budgeter.ts::assembleInjection`。

## R6 改口必须同步改写向量库条目

- **实现**：`graph.edit_entity` 在改 description 后立即 `_index_entity` 重算向量（embedded），
  LightRAG 适配器用 `edit → delete → create` 强制重嵌入；`journal.rollback` 也走 `graph.edit_entity` 同步。
- **验证**：`tests/python/test_supersede.py` 断言改口后旧说法检索不到旧值、新说法可命中、向量相似度变化；
  验收 C 全链路（Day3 提问不含旧偏好）。
- 代码：`graph.py::edit_entity/_index_entity`、`graph_lightrag.py::edit_entity`、`supersede.py::apply_supersede`。

## R7 召回失败/超时 300ms 一律旁路放行，绝不阻断对话

- **实现**：`src/injector.ts` 对 `retrieve` 与 `day_pack` 各自 `Promise.race(300ms)`；
  失败/超时 → 本次零注入（或复用上一轮 day 缓存），记 warning 与 `injection_record(bypassed)`，对话继续。
  sidecar 启动/重启失败同样只降级记忆功能（10 分钟 3 次上限后本次全部旁路）。
- **验证**：单元测（`assembleInjection` 空结果零注入）；E2E 中沙箱下 stdio 失败 → TCP 降级继续工作；
  可手动杀 sidecar 后继续对话（`retrieve bypassed` 日志）。
- 代码：`src/injector.ts::withTimeout/injectIntoAssembly`、`src/sidecarClient.ts::ensureStarted/restart 限制`。

## R8 任何一天的摘要只有一份，历史摘要单份

- **实现**：`daykeeper.py` —— 当天滚动摘要写在 `days/day_summary.json`（每 scope 单文件覆盖）；
  跨天 rollover 把“旧 history + 当日摘要”合并回**唯一** `days/history_summary.json` 并清空当天；
  绝不按天堆积文件。
- **验证**：`tests/python/test_daykeeper.py`（跨天单份 history、rollover 后旧天 turns 仍在）、
  验收 D2（30 天 50 轮后 history 文件仍为单份、day_pack≤1024）。
- 代码：`daykeeper.py::maybe_compress/rollover_if_needed/day_pack`。

## 硬指标对照

| 编号 | 内容 | 证据 |
|---|---|---|
| 8b | F1 纯应答不进图 | `tests/python/test_noise.py`（F1 25 条）、验收 F1 |
| 8c | F2 放行信号全部入库、误杀=0 | `test_noise.py`（F2 14 条 9 类信号）、验收 F2 |
| 17 | 注入总量硬顶 3000、不随规模/轮数增长 | budgeter fuzz + 验收 D1/D2/D3 |
| 18 | memory 动态档位 1024/1536/1792/2048 | `dynamicMemoryBudget` 单测 |
| 19 | day_context 恒定 512/312/200≤1024 | daykeeper 单测 + D2/D4 |
| 22 | 超预算优先级截断 + 告警 + 面板显示 | budgeter 单测、面板预算仪表盘、E2E 注入记录 |
| E1 | 规模低于 high 时 90 天零删除 | `tests/python/test_prune.py::E1`、验收 E1 |
| F4（补充） | Bearer 凭据必须拦截且不误伤普通英文 | `test_noise.py` 凭据回归 3 条 |

## 反例自查（哪些做法会被判违规，本实现如何避免）

| 违规做法 | 本实现 |
|---|---|
| 把注入历史追加成 user message（token 随轮数增长） | 动态 system section，每 step 覆盖，不写会话历史 |
| 每日一个摘要文件 | 单份 `history_summary.json` 滚动合并 |
| 检索到就 activate（权重通胀） | 只对 `injectedIds` 激活 |
| compress 后同轮直接删除 | 维护轮次状态机，同轮 prune 只报告；隔轮才归档 |
| 改口只改图不改向量 | `edit_entity` 内重算/重嵌入，验收断言旧说法不命中 |
| 权重写进 description | 全部在 `properties`，测试断言 |
| 图谱存 SQLite 且另存一份 JSON 副本 | 图谱为唯一 JSON 存储；SQLite 只做 Journal |
| 超时/失败抛给对话 | 300ms 旁路 + 零注入 |
