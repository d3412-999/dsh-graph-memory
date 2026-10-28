# panel/ — dsh-graph-memory 本地面板

纯原生 HTML / CSS / JS 的本地只读展示面板，附带受控写操作。**无 npm、无 CDN、无构建链、无外部网络请求**（只请求同源 `/api/*`）。

## 主题说明

当前为**米白色（暖白）主题**：`--bg #f6f1e6`、`--panel #fffdf8`、`--text #3a3226`，
点缀色为焦糖 `#a6723d` / 橄榄 `#8a7a4f` / 琥珀 `#c08a2e` / 砖红 `#b4574d`。
`app.js` 中的预算分布图例、图谱节点/边、权重曲线与阈值线使用同一套配色；深色主题已移除。
预览：`docs/panel-cream.png`。
## 文件

| 文件 | 职责 |
| --- | --- |
| `index.html` | 页面结构；所有区块带稳定 `id`，与 `app.js` 的 `cacheElements()` 一一对应 |
| `style.css` | 米白色暖色主题（cream / warm off-white）、12 列响应式网格、表格/图谱/曲线/降级态样式 |
| `app.js` | 全部逻辑：API 封装与降级、5s 轮询、SVG 简化力导向、30 天衰减曲线、受控操作 |
| `README.md` | 本文件 |

## 打开方式

sidecar 启动后访问 **http://127.0.0.1:9629/**（端口被占用时服务端会顺延，以 sidecar 日志为准）。

`sidecar/panel.py` 的路由约定：

- `/`、`/index.html` → `panel/index.html`
- `/static/<file>` → `panel/<file>`（本页引用 `static/style.css`、`static/app.js`）
- 服务端仅接受回环地址（127.0.0.1 / ::1 / localhost）；**无鉴权，请勿反向代理到非回环网络**。

## 区块 ↔ API 映射

| # | 区块 | 使用 API | 关键字段 / 说明 |
| --- | --- | --- | --- |
| 1 | 硬预算仪表盘 | `GET /api/injection`（失败回退 `panel_state.injection_last`）<br>`GET /api/panel_state` | `injection_last.tokens.{system,memory,day,detail,history,total}`、`turn`、`ts`、`injected_ids`；上限常量见下。`stats.scopes.{workspace,user}` 显示节点/活跃/关系/chunks/水位 low/high/hard/phase/round/day_tokens/history_tokens/data_home |
| 2 | 图谱可视化 | `GET /api/graph?scope=` | `nodes[].properties.{w,status,pinned}` + `description`；`relations[].{source,target}`；`counts`。失败时回退 `panel_state.nodes[scope]`（仅节点、无关系边，界面标注降级） |
| 3 | 记忆列表 / 语义搜索 | 搜索：`GET /api/search?q=&scope=`<br>默认列表：`/api/graph` 缓存或 `panel_state.nodes`<br>操作：`POST /api/action` | 搜索返回 `items[].{id,text,w,sim,pinned,status,updated_at}`、`top1_sim`；默认列表按前端计算的当前 w 降序。操作体 `{action:"pin"|"unpin"|"forget"|"set_weight"|"update", id?, w?, query?, new_value?, scope}` |
| 4 | 节点详情 + 权重曲线 | 复用 `/api/graph` 节点 `properties`（无额外请求） | `w`、`last_activated_at`、`updated_at`、`valid_from`、`protect_until`、`compressed_at`、`archived_at`、`workspace_hit`、`source_turn_ids` 等；曲线按 CONTRACT §2.1 前端本地计算 |
| 5 | 变更历史 / Journal | `GET /api/journal?scope=&target_id=` | `entries[].{id,ts,target_id,kind,before,after,confidence,rolled_back}`；`before/after` 为 `{description,properties}` 快照。支持 target_id 过滤；失败回退 `panel_state.journal_recent` |
| 6 | 剪枝日志 | `GET /api/prune?scope=` | `history[].{ts,kind,result}`；兼容 compress 报告（`compressed/merged/candidates/protected`）与 prune 报告（`dry_run/archived/deleted/would_*/count_*/reason`）。失败回退 `panel_state.prune_recent` |
| 7 | 死信 / 告警 | `GET /api/panel_state` | `dead_letters[].{id,ts,scope,payload,attempts,last_error,replayed}`、`alarms[].{id,ts,scope,kind,detail}`（只读） |
| 8 | 运行状态 | `GET /api/panel_state` + 页面自身信息 | `stats.sidecar.{version,pid,llm_mode,started_at}`、`stats.graph_backend`、API 地址、轮询间隔、上次刷新、本批失败项 |

## 契约常量（页面内硬编码，与 docs/CONTRACT.md 对齐）

- §2.1 权重惰性衰减：`w(t) = min(10, w_last · 2^(−Δt/604800))`（7 天半衰期），`Δt = t − last_activated_at`。
- §2.2 恒定注入预算：`TOTAL_HARD=3000`；分段上限 `system=128`、`memory=2048`（动态上限 1024~2048）、`day_text=512`、`detail=312`、`history=200`。
- §2.6 阈值参考线：压缩 `w<0.6`、归档 `w<0.3`（曲线虚线）。
- 仪表盘总量优先取 `tokens.total`，缺失时回退为五段求和；无注入记录时显示 `0`，并注明「注入总量硬顶 3000 tokens，不随使用时长/轮数/图谱规模增长」。

## 交互行为

1. **scope 切换**：顶栏 workspace/user 切换后，图谱/记忆/Journal/剪枝全部按新 scope 重新拉取；已提交的搜索词会在新 scope 重跑；选中节点清空。
2. **轮询**：每 5 秒刷新（`/api/panel_state`、`/api/injection`、`/api/graph`、`/api/journal`、`/api/prune`，有搜索词时加 `/api/search`）。页面隐藏时暂停，恢复可见立即刷新；scope 切换后尚未返回的旧响应会被丢弃。写操作完成后会立即补一次刷新（`pendingRefresh` 机制，不等下一轮轮询）。
3. **图谱**：SVG 简化力导向（全对斥力 + 边弹簧 + 向心引力）。节点半径 ∝ 当前 w，颜色按 status（active/compressed/archived/pending_forget），金色 = pinned；hover 显示 name/type/w/status/description 气泡；点击选中 → 右侧详情 + 节点白描边 + 关联边高亮。默认最多渲染 140 个当前 w 最高的节点（计数显示「显示/总数」），布局复用已有坐标避免轮询抖动；悬空边/自环忽略。
4. **记忆列表 / 搜索**：无查询时展示当前 scope 节点（按当前 w 降序，最多 60 条）；输入自然语言后回车或点「搜索」调用 `/api/search`，条目格式 `[#id | w=x | 日期] 描述`，并显示 `sim`。点击条目选中详情。
5. **详情与受控操作**：`pin/unpin`、`forget`（`window.confirm` 二次确认）、`set_weight`（数字框 + 滑块联动，0~10 步进 0.1）、`update` 改口（旧说法/新说法两输入）。写操作失败只弹错误 toast，不会影响轮询与页面。
6. **权重曲线**：Canvas 绘制从「当前」起未来 30 天的惰性衰减预测（61 个采样点），标注当前值、7d 半衰点、30d 值；虚线为 0.6 / 0.3 阈值。窗口缩放时重绘。
7. **Journal**：时间/kind/target/before→after/回滚状态。回滚状态按契约仅**展示**：`rolled_back` → 已回滚；kind ∈ {supersede,update,weight,pin,forget,prune_archive} 且 7 天内 → 可回滚；否则不可回滚/已过 7 天。点击 target 跳转选中并过滤。
8. **降级与容错**：所有请求都经 `requestJson()`/`guard()` 封装，失败只产生 `{ok:false,error}`，渲染层用 `safeRender()` 包裹并显示黄色「降级显示」横幅；页面不会抛未捕获异常、不会白屏。顶栏 chip 显示「已连接 / 部分失败（N 项降级）/ 连接失败（降级显示）」。

### 失败降级速查

| 失败接口 | 表现 |
| --- | --- |
| `/api/injection`、`/api/panel_state` | 仪表盘保留上次数据 + 降级横幅；无数据时显示 0；scope 统计显示「暂无统计」 |
| `/api/graph` | 保留上次图；首次失败时用 `panel_state.nodes[scope]` 渲染仅节点版本，图区标注「降级（仅节点）」并显示错误 |
| `/api/journal`、`/api/prune` | 表格保留上次数据 + 降级横幅；首次失败回退 `panel_state.journal_recent` / `prune_recent` |
| `/api/search` | 列表清空/保持上次结果，搜索栏提示失败 |
| `/api/action` | 错误 toast（含服务端 error 文本），不改变页面状态；下一次轮询校正 |
| 全部失败 | chip「连接失败（降级显示）」+ 各区块降级文本，5s 后自动重试 |

## 自测（无需启动 sidecar、无需浏览器）

```powershell
# 1) 语法检查（必需）
node --check panel/app.js

# 2) 视图模型自测：app.js 在非浏览器环境只暴露 __DSH_PANEL__，不启动轮询
node --input-type=module -e "await import('./panel/app.js'); const P = globalThis.__DSH_PANEL__; const w = P.decayValue(10, 1000, 1000 + 7 * 86400); if (Math.abs(w - 5) > 1e-9) throw new Error('decay mismatch'); const b = P.budgetViewModel({ ts: 1, tokens: { system: 100, memory: 900, total: 1000 } }); if (b.total !== 1000 || b.pct !== 33.3) throw new Error('budget mismatch'); console.log('view-model ok');"
```

`globalThis.__DSH_PANEL__` 同时是浏览器控制台调试入口，包含视图模型函数（`decayValue/decaySeries/budgetViewModel/journalRows/pruneRows/graphModel/layoutGraph/detailViewModel` 等）与运行时钩子（`refreshAll/selectNode/clearSelection/switchScope/runSearch/submitAction/state`）。

本次开发的实际验证（2026-10-28，供 Lead 复核）：

- `node --check panel/app.js`：通过。
- 136 项静态 + mock 断言：HTML `id` ↔ JS 缓存接线一一对应、CSS 关键 class 存在、衰减/预算/详情/记忆/Journal/剪枝视图模型与手写 mock JSON 吻合。
- 56 项渲染管线集成断言（Node DOM stub 注入 `fetch` mock）：三场景——全 API 成功、全 API 失败、仅 `/api/graph` 失败。覆盖仪表盘、scope 统计、SVG 节点/边、记忆列表、搜索渲染、Journal/剪枝/死信/告警、详情模板、`POST /api/action` 载荷、scope 切换、降级横幅与 chip 状态。
- **未做真机浏览器视觉验证**：本沙箱内 Edge headless 启动被拒绝（crashpad `OpenProcess: 拒绝访问 (0x5)`）。建议 Lead 启动 sidecar 后人工打开 `http://127.0.0.1:9629/` 核对视觉与 hover/点击手感。

## 已知限制 / 遗留问题

1. **回滚 / 死信重放无 UI**：`sidecar/panel.py` 的 `/api/action` 仅支持 `pin/unpin/forget/set_weight/update`，未暴露 `journal_rollback` 与 `dead_letter_replay` 路由（契约 §2.7 提到面板可重放）。因此本面板只展示 Journal「可回滚」状态、死信只读列表。如需要按钮，需先扩展 sidecar（不在本任务写权限内）。
2. **时钟来源**：前端衰减曲线用浏览器 `Date.now()` 计算；若 sidecar 通过 `debug_set_now` 模拟时钟，面板显示会与 sidecar 内部不一致（面板不读取 sidecar 时钟）。
3. **图谱规模**：最多渲染 140 节点（按当前 w 截断），全量计数仍显示「显示/总数」；大图交互（缩放/平移/子图过滤）未实现。
4. **memory 动态上限**：图例固定展示 `memory ≤2048`；规则中的 1024/1536/1792/2048 动态档位本次未从 API 获取，未在 UI 实时标示。
5. **数据实时性**：面板为轮询快照（5s），非 WebSocket；写操作后不做乐观更新，以重新拉取结果为准。
6. **兼容性**：面向现代 Chrome/Edge（ES2019+，SVG `closest`、`fetch`、Canvas 2D）；未兼容 IE/旧版浏览器。仅回环访问，无登录鉴权。
7. **写权限边界**：本目录 4 个文件由面板任务维护；未改动 `sidecar/**`、`src/**`、`tests/**`、`docs/**`、`config/**`。
