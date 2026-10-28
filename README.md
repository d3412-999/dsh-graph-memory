# dsh-graph-memory

基于 LightRAG 语义的 DSH 记忆插件：**开启后无需用户干预**，自主完成记忆的写入、召回、改口、压缩与遗忘。

- DSH 插件：TypeScript / Cordis，一行 `system-prompt/assemble` 注入 + 五个 `memory_*` 工具；
- Sidecar：Python 本地进程（stdin/stdout JSON-RPC 2.0，懒启动、空闲 300s 退出；沙箱环境自动降级 loopback TCP）；
- 图谱：embedded LightRAG-compatible 图存储（实体/关系/属性/向量索引）或真实 LightRAG HTTP 服务；
- Journal：SQLite 只记变更/激活/剪枝/注入/摘要流水，支持 7 天回滚；
- 面板：`http://127.0.0.1:9629/` 只读可视 + 受控操作（pin/forget/update/权重）。

## 红线速览

| 红线 | 实现 |
|---|---|
| R1 注入 token 恒为常数（硬顶可调 3000–2,000,000，默认 3000） | `src/budgeter.ts` 三个有界容器相加，容器按 `total/3000` 等比缩放；D1/D2/D3 三种场景实测差值 <5% |
| R2 图谱唯一真相源 | `sidecar/graph.py`（或 `graph_lightrag.py`）是唯一可写图谱；Journal 只是审计流水 |
| R3 权重/时间只存 properties | `properties.{w,last_activated_at,valid_from,...}`；`description` 无任何元数据 |
| R4 删除三阶段 | `round_tick → compress（零删除）→ 下一轮 prune 归档 → 冷区 30 天才真删` |
| R5 只有真正注入才激活 | `retrieve.injected_ids → assembleInjection.injectedIds → activate`，检索到但被截断的不激活 |
| R6 改口同步向量库 | `graph.edit_entity` 内重算/重嵌入向量；C 用例断言旧说法检索不到旧值 |
| R7 300ms 超时旁路 | `Injector` 对 retrieve/day_pack 各自 `Promise.race(300ms)`，失败/超时零注入继续对话 |
| R8 历史摘要只有一份 | `days/history_summary.json` 单文件滚动覆盖，按天合并 |

详见 [docs/REDLINES.md](docs/REDLINES.md)（含代码位置与测试证据）。

## 一键启动（本地记忆服务 + 面板）

不想先接 DSH、只想直接试用记忆服务与面板时：

```bash
./start.sh              # 构建（必要时）+ 启动 sidecar/panel；Ctrl+C 停止
./start.sh --no-build   # 跳过构建
./start.sh --port 9629  # 指定面板起始端口（被占用自动顺延）
./start.sh --install-profile web   # 顺便把插件 link 安装进 DSH profile（需重启 DSH）
./stop.sh               # 另开终端优雅停止（flush 图谱/Journal 后退出）
```

- 面板：`http://127.0.0.1:9629/`；数据：`~/.dsh/graph-memory`（user）与 `{repo}/.dsh/graph-memory`（workspace）；日志：workspace 数据目录下 `sidecar.log`。
- 支持 Git Bash / WSL / Linux / macOS；脚本会自动探测 Python（`python3` / `python` / `py -3`）并在需要时 `npm install` + `tsc` 构建。
- 调试：`py -3 scripts/sidecar_ctl.py status | ping | log | call <method>`。
## 快速开始（离线，无需模型/网络）

```powershell
cd C:\Users\15868\Desktop\练手项目\deepseek-memory
npm install            # 仅 typescript + @types/node（devDependencies）
npm run build          # src/*.ts -> lib/*.js（DSH 加载 lib/index.js）
npm run test:node      # TS 预算器/配置/注入器单测（25 tests，含 R1 硬顶 fuzz 与 R7 旁路）
npm run test:smoke     # 真实 sidecar 子进程 ping/ingest/retrieve/activate/day_pack/update/rollback 冒烟
npm run test:py        # Sidecar 全部单测（253 tests：graph/weight/journal/retrieval/noise/extract/supersede/daykeeper/prune/service/protocol/tokens）
npm run test:acceptance# 独立验收：A-F 用例 + 27 条 + 硬指标（17/17 PASS，输出 docs/ACCEPTANCE-REPORT.md）
```

真实 DSH 环境 E2E（需要 `DEEPSEEK_API_KEY`，自动从 `~/.dsh/.credentials.yaml` 读取）：

```powershell
py -3 scripts/e2e_dsh.py --keep-data
# 覆盖：插件安装/加载 → sidecar 启动 → turn 原文落盘 → 两轮会话记忆闭环（第二次回答 pnpm）
#       → 注入 token 记录 <=3000 → 卸载后无残留工具/进程、数据保留
```

录屏 / 演示（隔离数据目录 + 面板，六幕：写入 → 召回 → 改口 → 跨天 → 规模压力 → 收尾）：

```powershell
py -3 scripts/demo_live.py            # 分步，Enter 推进；--auto 不停顿
py -3 scripts/demo_live.py --keep     # 复用上次 .tmp/demo 数据
# 完整录屏脚本（窗口布局、分镜表、常见翻车处理）：docs/DEMO.md
```

## 安装进 DSH profile

插件注册发生在 DSH 服务启动时，**安装后需重启 profile**。推荐用官方 plugin 机制：

```powershell
# 在普通 PowerShell 终端（不是 DSH agent 沙箱）：
powershell -NoProfile -File scripts/install-plugin.ps1 -Profile web
# 卸载（保留数据）：
powershell -NoProfile -File scripts/uninstall-plugin.ps1 -Profile web
```

手工等价步骤：在 profile 的 `package.json` 加依赖 `"dsh-graph-memory": "link:<本目录>"`，
把 `dsh-graph-memory` 追加到 `dsh.profile.bundles`，重启 DSH。
启用后模型会看到 `memory_pin / memory_update / memory_forget / memory_recall / memory_stats` 五个工具，
每个模型 step 自动注入：

```
<graph_memory_instructions>固定常量段（≤128 tokens）</graph_memory_instructions>
<memory_context data-not-instructions="true">[#id | w=2.3 | 10-28 更新] 描述 …</memory_context>
<day_context data-not-instructions="true">当天上下文 / 补召回 / 历史摘要</day_context>
```

总量硬顶默认 3000 tokens，与使用时长、会话轮数、图谱规模无关；上限可在面板 `http://127.0.0.1:9629/`
的「注入上限」里调到 3000–2,000,000（写 `budget.json`，sidecar 与宿主注入器都跟随），
面板同时显示该长度下**每句话的输入成本**（按 `pricing.*` 里的官方单价折算）。

## 面板主题

面板默认使用**米白色（暖白）主题**：米白背景 `#f6f1e6`、暖白卡片 `#fffdf8`、深棕文字
与焦糖/橄榄/琥珀点缀色；图谱节点、权重曲线、预算分布条、状态徽章均已配套换色，深色终端风格不再使用。
效果预览：[docs/panel-cream.png](docs/panel-cream.png)。启动 `./start.sh` 后访问 `http://127.0.0.1:9629/` 即可查看。
## 配置

默认配置在 [config/default.json](config/default.json)（Sidecar）与 `src/config.ts` 的 `DEFAULT_CONFIG`（宿主侧）。
profile 的 `cordis.patch.yml` 可以按 id 覆盖：

```yaml
- id: graph-memory
  config:
    paths:
      userDataHome: 'D:/data/graph-memory/user'
      workspaceDataHome: 'D:/data/graph-memory/workspace'
    sidecar:
      pythonPath: 'py -3'       # 本机 Python（默认按 py -3 / python3 / python 依次探测）
      transport: auto           # stdio 优先；沙箱下自动降级 loopback TCP
      panelPort: 9629
    graph:
      backend: embedded         # embedded | lightrag
      lightragUrl: 'http://127.0.0.1:9621'
    pricing:                    # 仅用于面板成本估算（元/百万 token）
      model: deepseek-flash
      inputCacheMissPerM: 2     # 高峰价，空闲半价
      inputCacheHitPerM: 0.04
    llm:
      mode: host                # host=复用 DSH 当前会话模型；offline/scripted=测试；openai=自定义兼容端点
    panel:
      enabled: true
```

水位线上界建议实测校准（M5）：

```powershell
py -3 scripts/calibrate.py --quick     # 每档 50 次召回，输出 docs/calibration.json + 建议 high/low/hard
py -3 scripts/calibrate.py --apply     # 写入两个 scope 的 maintenance.json（优先级高于 config）
```

## 目录

```
src/                 DSH 插件（TypeScript/Cordis）
  index.ts           生命周期：监听注册 / sidecar 管理 / turn-end ingest / 每日冲突扫描 / 卸载清理
  injector.ts        system-prompt/assemble 注入、query 捕获、300ms 旁路、activate/injection_record
  budgeter.ts        与 Python 逐字节一致的 token 计量、三容器装配、硬顶截断、动态 memory 档位
  toolBridge.ts      memory_pin / memory_update / memory_forget / memory_recall(as_of) / memory_stats
  sidecarClient.ts   懒启动/空闲退出/stdio 与 loopback TCP 双传输/反向 host-llm/失败重启
  config.ts types.ts
sidecar/             Python Sidecar（标准库，无第三方运行时依赖）
  server.py          JSON-RPC 服务端（stdio/tcp）、面板线程、空闲退出、信号处理
  service.py         业务编排：retrieve/ingest/activate/update/compress/prune/day_pack/journal/stats
  graph.py           embedded LightRAG-compatible 图谱（唯一真相源 / 属性 / 向量索引 / 名字倒排候选）
  graph_lightrag.py  真实 LightRAG HTTP 适配器（/insert /query mode=mix /graph/entity/* /merge）
  noise.py           四道闸门（工具摘要/凭据/短句/去重）· config/noiseWords.yaml
  extract.py         LLM 抽取 + offline 确定性规则；supersede.py 改口判定与六步改写
  weight.py          7 天半衰期惰性衰减 / 激活 / score / 保护期
  daykeeper.py       天缓冲 / 全量压缩 / 补召回 / 单份历史滚动摘要（R8）
  prune.py           规模门控 + 双水位迟滞 + compress→prune 两阶段状态机（R4/E1-E6）
  retrieval.py       双路召回合并 / workspace×1.2 / pinned×1.5 / score 排序
  journal.py         SQLite 流水（7 天回滚、死信、注入统计）
  llm_bridge.py      host/offline/scripted/openai 四种 LLM 模式与降级
  panel.py           127.0.0.1:9629 面板 HTTP 服务（静态资源 + /api/*）
  budget.py          可调注入预算（3000–2M）：容器等比缩放、budget.json 持久化、每条消息成本
panel/               原生 JS 只读面板（预算仪表盘/图谱/搜索/权重曲线/Journal/剪枝/死信）
config/              default.json + noiseWords.yaml
tests/               node（TS 纯函数）/ python（模块单测）/ acceptance（A-F 黑盒验收）
scripts/             smoke_sidecar / e2e_dsh / calibrate / emit-tokens / emit-injection / install / uninstall
docs/                CONTRACT（内部契约）/ ARCHITECTURE / INSTALL / VERIFICATION / REDLINES / 验收标准与报告
```

## 已验证的关键行为

独立验收（`py -3 tests/acceptance/run_acceptance.py`，真实 sidecar 子进程 + 伪时钟）**17/17 PASS，0 失败/0 跳过，43s**：

- **记忆闭环（A）**：真实 DSH headless 两轮会话，第二轮回答 “pnpm”，注入记录 `memory=21 day=21 total=142 ≤3000`；离线 sidecar 版见验收 A。
- **改口（C）**：Day1 “我喜欢咖啡” → Day2 “现在不喜欢了” → 描述改为 `用户不喜欢咖啡`、`valid_from=Day2`、`w=max` 不清零、`protect_until +7d`、向量库同步（旧值检索不命中）、Journal before→after 可回滚；Day3 召回更新后节点。
- **恒定预算（D1/D2/D3）**：200 轮稳态注入波动 **0.148%**；30 天×50 轮稳态波动 **0.00%** 且 `history_summary.json` 恰好 1 份；图 100→20001 节点注入 **880→880（0%）**；全部 ≤3000。
- **压缩先于删除（B/R4）**：compress 轮零删除；同轮 prune 只报告；隔轮才归档。
- **补召回（D4）**：压缩触发（summary 346≤360 token）后当天细节补召回 **10/10 = 100%**。
- **门控（E1-E6）**：1000 节点（<high）跑 90 天删除=0 且权重 0.0001 仍可召回；超 high 单调剪到 low 停（201→184→169→155）；迟滞下 10 天触发 0 次；单轮处理量 = ceil(8%)；pinned 90% 第 3 轮 stuck 告警恰 1 条且不再重试；user 超界时 workspace 完全不动。
- **噪声（F1-F4）**：纯应答全丢、5 条放行信号全部入库误杀 0、随机 100 条短句误杀 0、3 类凭据全部拦截。
- **真实 DSH E2E（13/13）**：插件加载 → 沙箱下 sidecar 自动 loopback TCP 降级 → turn 原文落盘 → 记忆闭环 → 注入常量 → 卸载后无插件日志/无残留 sidecar 进程/数据保留。
- **旁路（R7）**：召回失败/超时零注入，对话继续。
- **卸载**：撤销工具/提示词/监听、停止 sidecar、保留数据文件。

验收细节与逐条证据见 [docs/ACCEPTANCE-REPORT.md](docs/ACCEPTANCE-REPORT.md) 与 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 校准产物

`py -3 scripts/calibrate.py` 在本机实测（1k/5k/10k/20k/50k 节点 × 100 次召回）P95 分别约
4.4 / 23.8 / 44.2 / 34.7 / 33.7 ms，均 <150ms 预算 → 建议 `workspace high=35000 / low=28000 / hard=87500`、
`user high=5000 / low=4000 / hard=12500`，写入 [docs/calibration.json](docs/calibration.json)；
`--apply` 会写入两个 scope 的 `maintenance.json`（优先级高于 config）。运行期仍按每日 P95 自适应。

## 已知限制

- embedded 图谱是为「无 LightRAG 服务时仍完整可用」准备的兼容实现；生产大图谱建议配置 `graph.backend: lightrag` 复用 LightRAG 的抽取/去重/双层检索与向量库。LightRAG HTTP 适配器要求其图存储保留自定义节点属性（默认 NetworkX/JSON 存储满足；Neo4j 固定 schema 会丢属性，需换存储）。
- 抽取/摘要/改口判定的质量取决于所用模型；`llm.mode=host` 复用 DSH 当前会话模型（零额外配置），也可覆盖为便宜快模型。
- embedded 检索为确定性哈希稀疏向量 + 名字倒排的近似双层检索，不是神经网络 embedding；语义泛化能力弱于 LightRAG 的向量检索。
- 面板图谱视图最多渲染 140 节点、无缩放/平移；真机视觉未做浏览器截图验证（沙箱内 headless 浏览器被拒）。
- 工具注册发生在 DSH 启动时，安装/升级插件后必须重启 profile 才生效。

## License

MIT
