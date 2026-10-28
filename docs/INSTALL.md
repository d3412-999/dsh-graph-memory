# 安装与运维

## 1. 前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| DSH | `>= 0.2.1-rc.2`（`dsh.engines.dsh`） | 插件通过 Cordis 加载 |
| Node | `>= 20` | DSH 自带，无需单独装 |
| Python | `3.10+`，仅标准库 | 默认探测顺序 `py -3` / `python3` / `python`，可用 `sidecar.pythonPath` 覆盖 |
| LLM | 默认复用 DSH 当前会话模型 | 无需额外 key/配置；也可配 `llm.mode=openai` 用独立便宜模型 |
| LightRAG（可选） | 外部 HTTP 服务 | 配 `graph.backend: lightrag` 后复用其抽取/去重/双层检索 |

本仓库的可选 devDependencies 仅 `typescript` + `@types/node`（构建用），Sidecar 无第三方 Python 依赖。

## 1.5 一键启动（独立试用，不接 DSH）

```bash
./start.sh          # 自动构建（必要时）并启动 sidecar + 面板，常驻模式；Ctrl+C 停止
./stop.sh           # 另开终端优雅停止
```

常用参数：`--no-build` / `--port 9629` / `--user-home <dir>` / `--workspace-home <dir>` / `--install-profile web`。
面板 `http://127.0.0.1:9629/`；调试控制 `py -3 scripts/sidecar_ctl.py status|ping|log|call <method>`。
Windows 请用 Git Bash 或 WSL 运行 `*.sh`；PowerShell 用户可直接用 `npm run start`。
## 2. 构建

```powershell
cd <本仓库>
npm install          # 如遇 npm 缓存目录沙箱限制：$env:npm_config_cache = "$PWD\.npm-cache"
npm run build        # tsc: src/*.ts -> lib/*.js（DSH 加载 lib/index.js）
```

## 3. 安装到 DSH profile

**推荐（普通 PowerShell 终端，非 DSH agent 沙箱）**：

```powershell
powershell -NoProfile -File scripts/install-plugin.ps1 -Profile web
# 可选：-PythonPath "C:\Python312\python.exe"  -SkipBuild
```

脚本等价于：

1. `dsh plugin --profile web add link:<本仓库>`（写入 profile 的 dependencies 并 pnpm 安装）；
2. 把 `dsh-graph-memory` 选入 `dsh.profile.bundles`（`dsh plugin add` 对带 `dsh.bundle.patch` 的包默认启用）；
3. 重启该 profile（工具注册与 bundle 行在 DSH 启动时生效）。

手工安装（无 plugin CLI 时）：

```jsonc
// $DSH_HOME/profiles/web/package.json
{
  "dependencies": { "dsh-graph-memory": "link:C:/path/to/deepseek-memory" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "dsh-graph-memory"] } }
}
```

并在 `$DSH_HOME/profiles/web/node_modules/dsh-graph-memory` 建一个指向仓库的 junction/symlink，重启。

## 4. 配置（profile 的 cordis.patch.yml，按 id 覆盖）

```yaml
- id: graph-memory
  config:
    paths:
      userDataHome: 'C:/Users/me/.dsh/graph-memory'
      workspaceDataHome: 'C:/work/proj/.dsh/graph-memory'
    sidecar:
      pythonPath: 'py -3'
      transport: auto        # stdio 优先；受限沙箱自动降级 127.0.0.1 TCP
      idleExitSeconds: 300
      panelPort: 9629
      panelHost: 127.0.0.1
    graph:
      backend: embedded      # embedded | lightrag
      lightragUrl: 'http://127.0.0.1:9621'
      lightragApiKey: null
    llm:
      mode: host             # host | offline | scripted | openai
    panel:
      enabled: true
```

完整字段与默认值见 [config/default.json](../config/default.json)（Sidecar）与 `src/config.ts`（宿主侧）。

### LLM 模式

| mode | 用途 |
|---|---|
| `host`（默认） | 反向 JSON-RPC 调 DSH 当前会话模型（零额外配置）；失败自动降级 offline 规则 |
| `offline` | 确定性规则（抽取/摘要/矛盾判定），验收测试与无模型环境用 |
| `scripted` | 读 `GRAPH_MEMORY_LLM_SCRIPT` 指向的 JSON 响应脚本，测试用 |
| `openai` | 独立 OpenAI-compatible 端点（`baseUrl` + `apiKeyEnv`），用便宜快模型做抽取/摘要 |

### LightRAG 后端

```yaml
- id: graph-memory
  config:
    graph:
      backend: lightrag
      lightragUrl: 'http://127.0.0.1:9621'
```

要求 LightRAG 服务开启 `/insert`、`/query`、`/graph/*` REST API；其图存储需保留自定义节点属性
（NetworkX/JSON/文件存储满足；Neo4j 固定 schema 会丢弃 properties，请换存储）。

## 5. 水位线校准（建议首次运行）

```powershell
py -3 scripts/calibrate.py            # 实测 1k/5k/10k/20k/50k 的召回 P95，输出 docs/calibration.json
py -3 scripts/calibrate.py --apply    # 写入两个 scope 的 maintenance.json（calibration 优先于 config）
```

规则：`high = 满足 P95<150ms 的最大档 × 0.7`，`low = high×0.8`，`hard = high×2.5`，下限 2000。
运行期还会按每日 P95 自适应（连续 3 天超预算且未到 high → high×0.8；长期富余且逼近 high → ×1.2，
夹逼 `[2000, 校准值×3]`）。

## 6. 面板

DSH 启动后访问 `http://127.0.0.1:9629/`（仅回环监听）。页面每 5 秒轮询：

- 注入预算仪表盘（最近一次实测 token 与 system/memory/day/detail/history 分布，上限 3000）；
- 图谱可视化（节点大小∝权重、颜色按状态、pinned 金色、点击详情）；
- 记忆列表 + 语义搜索 + pin/unpin/forget/update/权重调整（POST `/api/action`，仅回环可写）；
- 权重衰减曲线（前端按 7 天半衰期计算未来 30 天）、Journal 变更历史（含 7 天可回滚状态）、
  剪枝日志、死信与告警、运行状态。

面板 API 一览见 `panel/README.md`。只读部署请不要反代暴露（无鉴权，仅依赖 loopback 校验）。

## 7. 卸载

```powershell
powershell -NoProfile -File scripts/uninstall-plugin.ps1 -Profile web
# 默认保留数据：~/.dsh/graph-memory 与 {workspace}/.dsh/graph-memory
# 需要连数据一起删：-RemoveData
```

卸载行为（E2E 已验证）：撤销工具注册/系统提示词段/事件监听、停止 Sidecar、无残留进程；数据文件保留。
重启 profile 后插件不再加载。

## 8. 故障排查

| 现象 | 可能原因 / 处理 |
|---|---|
| 日志 `sidecar candidate failed spawn EPERM`，随后 `started (loopback tcp fallback)` | 宿主沙箱禁止 piped stdio 子进程；`transport:auto` 已自动降级，属正常 |
| 日志 `python ... ENOENT` | Python 不在 PATH；配 `sidecar.pythonPath` 为绝对路径（如 `C:/Python312/python.exe`） |
| 插件加载但工具不可用 | 工具在 DSH 启动时注册；重启 profile |
| `memory_stats` 返回错误 | sidecar 未启动/启动失败；查看 `<workspaceDataHome>/sidecar.log` 与 DSH 日志 |
| 注入 token 为 0 | 正常：无记忆且无当天上下文时不注入（零 token 浪费）；或 300ms 旁路触发（日志 `retrieve bypassed`） |
| 面板打不开 | `panel.enabled=false`、端口被占用（自动顺延，查看 sidecar.log 的 panel ready 行）、或访问的不是回环地址 |
| 改口不生效 | 确认否定/改口语境（`不/不再/换成/改用/…`）且候选相似度命中；`memory_update` 工具可直接覆盖式改写 |
| 剪枝长期不触发 | 规模低于 high 时**设计上零删除**（时间不是删除理由）；用 `memory_stats` 看 count 与 watermarks，或 `scripts/calibrate.py` 调整 high |
