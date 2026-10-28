# 验证手册（M0–M5）：每条都可直接运行

> 所有命令在仓库根目录执行。离线命令不需要模型/网络；标 🔑 的命令需要 `DEEPSEEK_API_KEY`
> （E2E 脚本会自动从 `~/.dsh/.credentials.yaml` 的 `refs` 读取，不会打印明文）。
> 统一使用 `py -3`（本机 Python 3.14）；若没有 `py` 启动器，把命令里的 `py -3` 换成 `python`。

## 0. 一键启动（Mem 服务 + 面板，不接 DSH 也可用）

```bash
./start.sh --no-build --port 19660
# 期望输出：· Python / · 数据 / · 启动 → "✓ 就绪：sidecar pid=..." + 面板 URL
# 浏览器打开面板 URL（/healthz 返回 {"ok": true}）
py -3 scripts/sidecar_ctl.py --workspace-home ./.dsh/graph-memory status   # running: true
py -3 -m unittest tests.python.test_protocol                               # 协议回归（sidecar 被改动时）
./stop.sh                                                                   # graceful shutdown，runtime 文件被清理
```

M0 也包含该路径：`bash -n start.sh && bash -n stop.sh` 语法检查 + 上面的启停循环。
（本机 DSH 受限沙箱无法执行 Git Bash 信号管道，真实验证需在普通终端运行；sidecar 单测/验收均在受限沙箱内可跑。）
## 0. 一次性准备

```powershell
npm install --no-audit --no-fund     # 只装 typescript + @types/node（构建用）
npm run build                        # src/*.ts -> lib/*.js
```

## M0 骨架与通道（Sidecar 启停、JSON-RPC 连通、配置读取）

```powershell
py -3 scripts/smoke_sidecar.py
```

期望：`ALL SMOKE CHECKS PASSED`，覆盖
`ping/pong → ingest（节点+chunk）→ retrieve（命中+injected_ids）→ activate（w 1.0→1.6）→ day_pack（预算内）→ stats → tokens → update（supersede 六步）→ journal_list（kind=supersede）→ journal_rollback（7 天内恢复）`。

单独验证配置读取（含 BOM 兼容）：

```powershell
py -3 -c "import sys;sys.path.insert(0,'sidecar');from config import load_defaults;d=load_defaults();print(d['injection']['totalHardTokens'], d['sidecar']['transport'])"
# 期望：3000 auto
```

## M1 记忆闭环（入图 + 召回注入 + 天缓冲）

离线（真实 sidecar 协议）：

```powershell
py -3 -c "
import json,sys; sys.path.insert(0,'tests/acceptance'); from rpc_client import SidecarClient
c=SidecarClient.prepare(llm_mode='offline'); c.start()
print(c.call('ingest',{'turn_text':'决定用 pnpm 管理依赖，版本 9.15','turn_id':'t1','scope':'workspace'})['node_ids'])
print(c.call('retrieve',{'query':'上次用什么包管理器','scope':['user','workspace']})['items'][0]['rendered'])
c.stop()"
```

期望：返回节点 id；retrieve 的 `rendered` 包含 `pnpm`。

真实 DSH（🔑，推荐）：

```powershell
py -3 scripts/e2e_dsh.py --keep-data
```

期望 13/13 PASS：插件加载日志、sidecar 启动（沙箱环境走 loopback TCP fallback）、
run1 原文落盘、run2 回答 `pnpm`、注入记录含 memory/day 且 total≤3000、
卸载后无插件日志/无残留 sidecar 进程/数据文件保留。

## M2 权重与剪枝（衰减、激活、总量门控、compress/prune 两阶段）

```powershell
py -3 -m unittest tests.python.test_weight tests.python.test_prune tests.python.test_retrieval -v
```

期望：`OK`（weight 29 + prune 18 + retrieval 31）。覆盖：
衰减公式、激活 boost、score 排序、压缩态复活、新建保护；
B（compress 轮零删除、隔轮才归档）、E1（900 天图规模低于 high 删除=0）、
E2（超 high 剪到 low 停）、E3（迟滞 10 天触发≤1）、E4（单轮≤8%）、
E5（pinned 90% 3 轮 stuck 告警并停止）、E6（scope 独立）、dry_run 只报告、冷区 30 天真删。

## M2.5 改口与冲突消解（两道防线、覆盖式改写、Journal 回滚）

```powershell
py -3 -m unittest tests.python.test_supersede tests.python.test_journal tests.python.test_service_rollback -v
```

期望：`OK`。覆盖 C 用例逐日断言（description/valid_from/w=max/protect_until+7d/
向量库同步：旧说法检索不到旧值/Journal before→after/7 天回滚）、
六步顺序、不同 scope 不冲突、每日 sweep 只报告不改图、set_weight/forget 回滚恢复旧值。

## M3 恒定预算改造（day_pack、全量压缩、补召回、计量截断）

```powershell
node tests/node/budgeter.test.mjs    # TS 预算器 12 项（R1/动态档位/优先级/fuzz 永不超 3000）
node tests/node/config.test.mjs      # 配置与 scope 选择 7 项
node tests/node/injector.test.mjs    # 注入器 6 项（成功注入/R5 只激活 injected_ids/R7 旁路与超时）
py -3 -m unittest tests.python.test_daykeeper tests.python.test_tokens -v
```

期望：全部 OK。覆盖 D2（30 天×50 轮首轮差值<5%、历史摘要单份、day_pack≤1024）、
全量压缩（≤360 token、保留最近 2 条、限流 30min/20 轮、原文不删）、补召回 D4（压缩后当天细节命中率≥90%）、
跨语言 token 计量一致性（Python ↔ `scripts/emit-tokens.mjs`）。

## M4 面板与工具

```powershell
# 起一个带面板的 sidecar（临时数据目录）
py -3 -c "
import json,subprocess,sys,time,os
p=subprocess.Popen([sys.executable,'sidecar/server.py','--user-home','.tmp/panel/user','--workspace-home','.tmp/panel/ws',
 '--workspace-root',os.getcwd(),'--config',json.dumps({'llm':{'mode':'offline'}}),'--panel-port','19629'],
 cwd=os.getcwd(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
time.sleep(2); p.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'ping','params':{}})+'\n'); p.stdin.flush()
print('panel:', p.stdout.readline().strip()[:120], p.stdout.readline().strip()[:160])
print('health:', __import__('urllib.request',fromlist=['urlopen']).urlopen('http://127.0.0.1:19629/healthz').read().decode())
p.stdin.write(json.dumps({'jsonrpc':'2.0','id':2,'method':'shutdown','params':{}})+'\n'); p.stdin.flush(); p.wait(timeout=5)"
```

期望：第二个 JSON 行出现 `panel ready` URL；`health:` 输出 `{"ok": true}`。浏览器打开该 URL 可见
预算仪表盘/图谱/记忆搜索/权重曲线/Journal/剪枝/死信。面板前端静态检查：

```powershell
node --check panel/app.js
```

工具注册检查（真实 DSH）：E2E 日志中插件加载；在 DSH 会话里让模型调用 `memory_stats`（或直接问“记住我用 pnpm”后验证下轮回答）。
安装后必须重启 profile（工具在 DSH 启动时注册）。

## M5 打磨与验收（性能、凭据过滤、注入隔离、卸载清理）

```powershell
# 凭据过滤 + 噪声闸门（F1/F2/F3 + 凭据误伤回归）
py -3 -m unittest tests.python.test_noise -v
# LightRAG 适配器（fake HTTP server，协议映射/属性/重嵌入/不可达降级）
py -3 -m unittest tests.python.test_graph_lightrag -v
# 水位线校准（每档 50/100 次召回测 P95，输出 docs/calibration.json）
py -3 scripts/calibrate.py --quick
# 全部 sidecar 单测
py -3 -m unittest discover -s tests/python -t . 
# 独立验收 A-F + 27 条 + 硬指标（输出 docs/ACCEPTANCE-REPORT.md / acceptance.json）
py -3 tests/acceptance/run_acceptance.py
# 真实 DSH E2E（含卸载清理检查）
py -3 scripts/e2e_dsh.py
```

期望：全部 PASS / OK。验收报告含每条用例的通过状态、关键断言值与 27 条编号映射。

## 一键回归

```powershell
npm run build; npm run test:node; npm run test:smoke; py -3 -m unittest discover -s tests/python -t .; py -3 tests/acceptance/run_acceptance.py
```

## 判定标准速查

| 指标 | 阈值 | 验证位置 |
|---|---|---|
| 注入总量 | ≤3000 tokens（实测），D1/D2/D3 差值 <5% | budgeter node 测试 / 验收 D1-D3 / E2E 注入记录 |
| memory 动态档位 | 1024/1536/1792/2048 | `dynamicMemoryBudget` 单测 |
| day_context | 512+312+200 ≤1024 | budgeter/daykeeper 单测、D2 |
| token 计量一致性 | Python 与 TS 固定样本完全一致 | `tests/python/test_tokens.py` + `scripts/emit-tokens.mjs` |
| 压缩后删除 | 第 N 轮 compress 零删除；第 N+1 轮起才归档 | B / prune 单测 |
| 规模门控 | count<high 90 天零删除 | E1 |
| 迟滞 | low~high 波动 10 天触发≤1 | E3 |
| 单轮处理量 | ≤ceil(8%×节点数) | E4 |
| 误杀 | F1/F2/F3 = 0 | noise 单测 / 验收 F |
| 改口向量同步 | 旧说法检索不到旧值 | C / test_supersede |
| 卸载 | 无残留工具/进程，数据保留 | E2E uninstall 段 |
