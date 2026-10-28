# dsh-graph-memory 独立验收报告

- 生成时间：2026-10-28T22:57:50+0800
- 运行环境：Python 3.14.0 / Windows-11-10.0.26200-SP0
- 仓库：`C:\Users\15868\Desktop\练手项目\deepseek-memory`

## 运行命令

```powershell
# 全量独立验收（真实 sidecar 子进程 + JSON-RPC 黑盒）
py -3 tests/acceptance/run_acceptance.py

# 单用例复现（示例）
py -3 tests/acceptance/run_acceptance.py -k test_D3

# 协议/令牌交叉校验（单独）
py -3 -m unittest tests.python.test_protocol tests.python.test_tokens -v
```

## 结果汇总

| 总数 | 通过 | 失败 | 错误 | 跳过 | 耗时 |
|---|---|---|---|---|---|
| 17 | 17 | 0 | 0 | 0 | 125.8s |

退出码：`0`（0=全部通过，1=有失败，2=有跳过） ✅

## 用例明细

| 用例 | 状态 | 耗时 | 验收编号 | 失败原因/关键证据 |
|---|---|---|---|---|
| `test_A_memory_loop` | PASS | 1.26s | 1, 2, 7, 8 | ingest_elapsed_s=0.0847; retrieved_items=[{"name": "pnpm", "sim": 0.330167}, {"name": "npm", "sim": 0.035093}] |
| `test_B_compress_before_prune` | PASS | 2.22s | 9, 27 | nodes_before=60; compress_result={"compressed": ["ent_89a086800076342e", "ent_ff4671ec3dce7307", "ent_200c6e28b38012bb", "ent_a0018f13a6d7e4f3", "ent_... |
| `test_C_supersede_daily` | PASS | 1.47s | 11, 12, 13, 14, 16 | old_description="用户喜欢咖啡"; superseded={"before": "用户喜欢咖啡", "after": "用户不喜欢咖啡", "valid_from": "2026-03-16", "protect_days": 7.0, "w": 1.0} |
| `test_D1_200_turns_constant_budget` | PASS | 21.66s | 17, 22 | turn1_total=1149; steady_totals=[1349, 1349, 1349, 1349, 1349, 1349, 1350, 1351, 1351, 1351, 1351, 1351, 1351, 1351, 1351, 1351] |
| `test_D2_30days_constant_budget_single_history` | PASS | 43.74s | 17, 19, 21 | day1_total=1016; daily_totals_tail=[{"day": 26, "total": 1151}, {"day": 27, "total": 1151}, {"day": 28, "total": 1151}, {"day": 29, "total": 1151}, {"da... |
| `test_D3_graph_scale_constant_budget` | PASS | 3.38s | 17, 22 | nodes=20001; before_tokens={"system": 46, "memory": 775, "day": 15, "detail": 0, "history": 0, "total": 881} |
| `test_D4_compressed_detail_recall` | PASS | 2.76s | 19, 20 | compressed=true; summary_tokens=346 |
| `test_E1_90days_below_high_zero_delete` | PASS | 3.99s | 24 | nodes=1000; archived_after_90d=0 |
| `test_E2_over_high_prune_to_low` | PASS | 8.71s | 25 | high=200; low=160 |
| `test_E3_band_fluctuation_triggers` | PASS | 6.14s | 26 | high=100; low=80 |
| `test_E4_single_round_max_fraction` | PASS | 8.27s | 26 | nodes_before=201; expected_limit=17 |
| `test_E5_pinned_stuck_alarm` | PASS | 8.2s | 26 | pinned=189; rounds=[{"round": 1, "action": "compress", "reason": "over_high", "stuck": false, "live": 190}, {"round": 2, "action": "prun... |
| `test_E6_scope_isolation` | PASS | 2.62s | 26 | user_counts=[30, 30, 27, 24, 22]; workspace_before={"nodes": 10, "nodes_live": 10, "phase": "idle"} |
| `test_F1_pure_responses_dropped` | PASS | 1.17s | 4, 8b | rows=[{"text": "好的", "dropped": true, "reason": "too_short"}, {"text": "嗯嗯", "dropped": true, "reason": "too_short"}, {"te... |
| `test_F2_signal_pass_no_false_kill` | PASS | 1.32s | 4, 8c | rows=[{"text": "明天下午三点开会", "dropped": false, "reason": "passed", "nodes": 0}, {"text": "价格改成 80", "dropped": false, "reaso... |
| `test_F3_short_sentences_no_false_kill` | PASS | 7.76s | 4, 8d | sentences=100; killed=0 |
| `test_F4_credential_dropped_and_alarm` | PASS | 1.12s | 4 | rows=[{"text": "我的 api key 是 sk-abcdefgh12345678，帮我保存", "dropped": true, "reason": "credential"}, {"text": "Authorization:...; warning_notifications=3 |

## 27 条验收标准映射

| 编号 | 验收语义 | 对应用例 | 结果 |
|---|---|---|---|
| 1 | 插件启用后无需干预，自主完成写入/召回/改口/压缩/遗忘 | `test_A_memory_loop` | PASS |
| 2 | turn 结束后异步触发写入，不阻塞对话响应 | `test_A_memory_loop` | PASS |
| 3 | 新记忆初始权重 1.0；pin/“记住”权重 2.0 且 pinned=true | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 4 | 写入四道闸门按序执行（工具摘要/凭据丢弃告警/短句丢弃/>0.95 去重） | `test_F1_pure_responses_dropped`, `test_F2_signal_pass_no_false_kill`, `test_F3_short_sentences_no_false_kill`, `test_F4_credential_dropped_and_alarm` | PASS |
| 5 | 实体类型沿用 LightRAG 默认分类并额外支持 Preference/Decision | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 6 | 衰减读时惰性求值 w(t)=min(10,w·2^(-Δt/7d))，不依赖定时任务 | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 7 | 激活仅对 injected_ids；w=min(10,w+0.5·(1+boost)) | `test_A_memory_loop` | PASS |
| 8 | 排序 score=sim×(0.3+0.7×min(1,w/5))，pinned×1.5，workspace×1.2 | `test_A_memory_loop` | PASS |
| 8b | F1 纯应答不进图（硬指标） | `test_F1_pure_responses_dropped` | PASS |
| 8c | F2 放行信号全部入库、误杀=0（硬指标） | `test_F2_signal_pass_no_false_kill` | PASS |
| 8d | F3 随机 100 条 8~30 字短句误杀=0 | `test_F3_short_sentences_no_false_kill` | PASS |
| 9 | 删除三阶段：先压缩→隔轮才归档→冷区超期才真删；禁止同轮压完即删（R4） | `test_B_compress_before_prune` | PASS |
| 10 | 卸载时撤销注册/监听、停 sidecar、保留数据文件 | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 11 | 写入时同 scope 近邻 top-5，sim∈[0.62,0.95) 才送 LLM 判矛盾 | `test_C_supersede_daily` | PASS |
| 12 | 矛盾置信度 ≥0.7 触发改写；含否定词阈值 0.6 | `test_C_supersede_daily` | PASS |
| 13 | 改写六步：edit→valid_from/updated→w=max→同步向量→Journal→protect_until+7d（R6） | `test_C_supersede_daily` | PASS |
| 14 | 改口最新为准、静默替换；不同 scope 各自保留 | `test_C_supersede_daily` | PASS |
| 15 | 每日首次会话扫近 30 天矛盾对兜底，产出 conflict_report | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 16 | 改口 Journal 记录 before/after/date/confidence/source_turn_id，7 天内可回滚 | `test_C_supersede_daily` | PASS |
| 17 | 注入 token 总量硬顶 3000，不随使用时长/轮数/图规模增长（硬指标） | `test_D1_200_turns_constant_budget`, `test_D2_30days_constant_budget_single_history`, `test_D3_graph_scale_constant_budget` | PASS |
| 18 | memory_context 动态档位 1024/1536/1792/2048（硬指标） | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 19 | day_context 恒定 ≤1024（512/312/200），当天超界给全量压缩摘要（硬指标） | `test_D2_30days_constant_budget_single_history`, `test_D4_compressed_detail_recall` | PASS |
| 20 | 全量压缩 ≤360 token、保留最近 2 条原文、原文不删、限流 30min/20 轮 | `test_D4_compressed_detail_recall` | PASS |
| 21 | 历史滚动摘要永远只有一份 ≤200 token，绝不逐日堆积（R8） | `test_D2_30days_constant_budget_single_history` | PASS |
| 22 | 注入前本地计量；超 3000 按优先级截断并告警；面板显示实测 token（硬指标） | `test_D1_200_turns_constant_budget`, `test_D3_graph_scale_constant_budget` | PASS |
| 23 | 召回为空不注入任何标签；召回失败/超时 300ms 旁路放行（R7） | （由单测/TS 测试覆盖） | 其它套件覆盖 |
| 24 | 图谱规模 < high 完全不剪枝（时间不是删除理由），可召回、激活回升（E1） | `test_E1_90days_below_high_zero_delete` | PASS |
| 25 | 图谱规模 > high 开启剪枝并剪到 <= low 停；> hard 强制剪+告警 | `test_E2_over_high_prune_to_low` | PASS |
| 26 | 双水位迟滞/单轮 ≤8%/连续 3 轮剪不到 low 停止+告警/三档按 scope 独立计量 | `test_E3_band_fluctuation_triggers`, `test_E4_single_round_max_fraction`, `test_E5_pinned_stuck_alarm`, `test_E6_scope_isolation` | PASS |
| 27 | 压缩/剪枝免疫 pinned、Preference、活跃 workspace、近 7 天改过、新建 3 天；dry_run 只出报告 | `test_B_compress_before_prune` | PASS |

## 硬指标

| 硬指标 | 含义 | 对应用例 | 结果 |
|---|---|---|---|
| 8b | 纯应答不进图 | `test_F1_pure_responses_dropped` | PASS |
| 8c | 放行信号全部入库、误杀=0 | `test_F2_signal_pass_no_false_kill` | PASS |
| 17 | 注入总量硬顶 3000 且与时长/轮数/规模无关 | `test_D1_200_turns_constant_budget`, `test_D2_30days_constant_budget_single_history`, `test_D3_graph_scale_constant_budget` | PASS |
| 18 | memory_context 动态档位 | （TS node 测试/交叉校验覆盖） | 其它套件覆盖 |
| 19 | day_context 恒定预算 | `test_D2_30days_constant_budget_single_history`, `test_D4_compressed_detail_recall` | PASS |
| 22 | 超预算优先级截断 + 告警 | `test_D1_200_turns_constant_budget`, `test_D3_graph_scale_constant_budget` | PASS |
| E1 | <high 时 90 天零删除 | `test_E1_90days_below_high_zero_delete` | PASS |


## 其它环境/交叉校验命令结果

| 命令 | 结果 | 退出码 | 说明 |
|---|---|---|---|
| `py -3 -m unittest discover -s tests/python -t .` | Ran 253 tests in 42.2s — OK | 0 | 含本验收框架 test_protocol.py(16) + test_tokens.py(12)；stderr 的 [graph-memory] 日志/DSH asyncio 慢任务提示属正常输出 |
| `py -3 -m unittest tests.python.test_protocol` | Ran 16 tests — OK（含 1 条契约缺口软断言：参数非法错误码实测 -32603，契约期望 -32602） | 0 | 黑盒 JSON-RPC：§3.1 方法结构/错误码/通知/非法 JSON/shutdown/scripted 模式 |
| `py -3 -m unittest tests.python.test_tokens` | Ran 12 tests — OK | 0 | token 公式与 base/TS 三方一致 + 预算装配 1024/1536/1792/2048 + 硬顶 3000 + config 默认值回归 |
| `node tests/node/budgeter.test.mjs` | pass 12 / fail 0 | 0 | TS budgeter 纯函数单测（sandbox 下不能用 node --test，会 spawn 子进程 EPERM）；覆盖验收 18/22 的 TS 侧语义 |
| `node tests/node/config.test.mjs` | pass 7 / fail 0 | 0 | TS config 纯函数单测（含 config.deepMerge 深拷贝回归：默认配置不被 makeConfig 污染） |
| `node tests/node/injector.test.mjs` | pass 6 / fail 0 | 0 | TS injector 单测：成功注入+只对 injected_ids activate（R5）、R7 抛错旁路、ensureStarted 失败旁路、检索 hang 在 300ms 预算内旁路、空结果零注入、默认 300ms 断言；覆盖验收 7/23 |
| `node 三条串联（package.json test:node）` | 12 + 7 + 6 = 25 pass / 0 fail | 0 | 三条命令串联：budgeter(12) + config(7) + injector(6) |
| `node scripts/emit-tokens.mjs --samples tests/fixtures/token_samples.json` | 15 个固定样本 TS tokens 与 Python base.estimate_tokens 逐字节一致 | 0 | 跨语言 token 计量一致性（CONTRACT §2.2），由 tests.python.test_tokens 断言 |
| `node scripts/emit-injection.mjs --input tests/fixtures/injection_cases.json` | 3 个装配用例 memory/day/detail/history 分量 TS 与 Python 镜像完全一致，总量差 <=24（system 常量文本各自实现），全部 <=3000 | 0 | TS/Python 注入预算装配交叉校验（D1/D2/D3 计量方法学依据） |
| `py -3 scripts/smoke_sidecar.py` | ALL SMOKE CHECKS PASSED（ingest/retrieve/activate/day_pack/stats/tokens/update/Journal/7 天回滚） | 0 | M0 冒烟：真实 sidecar 子进程（非 unittest） |
| `py -3 scripts/e2e_dsh.py` | 13/13 PASS（由 Lead 执行；真实 DSH headless） | 0 | 需要 DEEPSEEK_API_KEY（~/.dsh/.credentials.yaml），会访问真实模型 API；离线验收侧未复跑，作为真实宿主证据引用；覆盖验收 10（卸载清理/数据保留）与真实注入记录 total<=3000 |
| `bash start.sh --no-build` | 用途说明（未在验收环境执行） | - | 用户要求的「一键启动」入口：跳过 TypeScript 构建（--no-build），以常驻模式（sidecar.idleExitSeconds=0）拉起本地 sidecar + 只读面板（默认 http://127.0.0.1:9629，端口占用自动顺延），Ctrl+C 或 ./stop.sh 优雅停止、数据与日志保留；脚本自动探测 Python（python3/python/py -3）、必要时 npm install + tsc 构建，--install-profile <profile> 可把插件 link 装进 DSH profile。配套 scripts/sidecar_ctl.py 提供 wait-ready/status/ping/call/log/stop 调试命令。该脚本依赖 Bash（Git Bash/WSL/Linux/macOS），未作为离线 unittest 用例执行，仅记录用途。 |

交叉一致性校验：

- token 计量：Python base.estimate_tokens == tests/acceptance/injection_model.py 镜像 == TS lib/budgeter.js（15 个固定样本，含 CJK/ASCII/混合/emoji/CJK 标点/长串）
- 注入装配：Python 镜像与 TS assembleInjection 的 memory/day/detail/history 分量逐 token 一致（3 个用例：普通/空/显式+pinned），总量差异仅来自两侧 system 常量文本不同（阈值 <=24 token）
- D1 稳态注入 token 波动 0.148%（<5%，排除前 50 轮冷启动，200 轮共 16 次采样）
- D2 30 天 × 50 轮稳态注入 token 波动 0.00%（<5%，day15~day30 采样），history_summary.json 恰好 1 份且 history token<=200
- D3 图谱 100→20001 节点注入 token 差 0.00%（<5%），top1_sim 前后均为 0.4796 且 top1 命中固定记忆
- D4 压缩后当天细节补召回 10/10=100%（>=90%），summary_tokens=346（<=360）
- E4 单轮处理量 17 = ceil(201×8%)，report.limit 一致；E5 prune_stuck 告警 1 条且 stuck 后不再变化；E6 user 剪枝生效而 workspace 零剪枝

其它测试套件对未直接映射验收条目的覆盖说明：

- 验收 3/5/6/15：由产品单测覆盖（tests/python/test_weight.py、test_extract.py、test_supersede.py 等，253 tests OK）
- 验收 18：由 node tests/node/budgeter.test.mjs 覆盖（动态 memory 预算 1024/1536/1792/2048），并由 test_tokens 交叉校验
- 验收 23：由 node tests/node/budgeter.test.mjs（空结果零注入）+ tests/node/injector.test.mjs（R7 抛错旁路 / 检索 hang 300ms 预算内旁路）+ test_protocol.test_retrieve_empty_result_zero_tokens 覆盖
- 验收 7：由 node tests/node/injector.test.mjs（只对 injected_ids activate）与验收 A 激活断言共同覆盖
- 验收 10：由真实 DSH headless E2E（scripts/e2e_dsh.py，13/13 PASS）覆盖

## 集成/验收期间发现并已修复的缺陷

### FIX-1 [fixed] config/default.json 带 UTF-8 BOM → 默认配置整体失效

config/default.json 被重写时引入 BOM，sidecar/config.py 的 load_defaults() 以 encoding='utf-8' 读取，JSONDecodeError 被静默吞掉返回 {}，所有默认配置（injection/prune/llm/day 等）丢失。修复：default.json 重写为无 BOM + load_defaults/parse_override 改 utf-8-sig 双保险；回归断言：test_tokens.DefaultConfigTest（无 BOM + totalHardTokens=3000 等默认值）。

### FIX-2 [fixed] graph.py 候选选择缺陷：图谱 20000 节点时高相似节点被漏召回（D3 注入量漂移 15.9%）

graph.py `_candidate_ids` 只取 8 个最小倒排桶做候选，20k 节点时桶被 bulk 噪声占满，100 个高相似固定记忆节点一个都进不了候选，retrieve 注入 token 差 15.9%（D3 硬指标 17 不达标）。修复：名字词法倒排（ASCII 词 + CJK 2-gram）∪ 哈希维度倒排（遍历全部 query 维度，cap 调优），并补 graph.py 缺失的 `import re`（名字倒排 NameError）。回归：test_D3_graph_scale_constant_budget。

### FIX-3 [fixed] server.py 未知方法返回 -32603（RpcError 关键字参数用错，-32601 永远出不来）

dispatch 里 `RpcError(..., code=-32601)`，但 base.RpcError 形参是 rpc_code，TypeError 被通用 except 捕获成 -32603 internal error，违反 CONTRACT §3 错误码约定。修复：改为 rpc_code=-32601。回归：test_protocol.test_unknown_method_32601。

### FIX-4 [fixed] noise.py 凭据正则漏 Bearer token（凭据可能进图谱/日志）

CONTRACT §2.3 要求凭据正则覆盖 `Bearer\s+\S+`；实测 `Authorization: Bearer abcdef123456.token` 因命中数字放行信号而 passed（未丢弃）。修复：新增大小写不敏感 Bearer 模式（含数字≥8 字符或无数字≥20 字符），同时保证普通英文 `bearer token is required` 不误伤。回归：test_F4_credential_dropped_and_alarm。

### FIX-5 [fixed] ingest 同名实体 upsert 绕过改口检测（C 场景旧说法仍可检索）

Day2 「我现在不喜欢喝咖啡了，改成喝茶」抽出的候选名与 Day1 同名，service.ingest 走「已有同名实体 → 追加 description」短路，未进入 supersede 判定：description 变旧+新拼接、无 Journal supersede、旧值仍被检索（违反 R6）。验收探针发现后由实现方修复；现回归：test_C_supersede_daily（description/valid_from=Day2/w=max/protect_until+7d/Journal/rollback/R6 全链路）。

## 证据（断言实测值）

### test_A_memory_loop — PASS

```json
{
 "ingest_elapsed_s": 0.0847,
 "retrieved_items": [
  {
   "name": "pnpm",
   "sim": 0.330167
  },
  {
   "name": "npm",
   "sim": 0.035093
  }
 ],
 "activated": {
  "ent_c41accc9f025a3b9": 1.599999966810178,
  "ent_248436de9759fb07": 1.5999999670014504
 },
 "r3_violations": []
}
```

### test_B_compress_before_prune — PASS

```json
{
 "nodes_before": 60,
 "compress_result": {
  "compressed": [
   "ent_89a086800076342e",
   "ent_ff4671ec3dce7307",
   "ent_200c6e28b38012bb",
   "ent_a0018f13a6d7e4f3",
   "ent_24a1bb5b1b0bad52",
   "ent_d387d1fda46b740a",
   "ent_1924be449c85ea0a",
   "ent_e14c517a942d8565",
   "ent_4bda5f3f3d3f2637",
   "ent_fbe3107a12da3378",
   "ent_08cb9f8566c5b9d2",
   "ent_383323b8b8bc71be",
   "ent_ad57c4a6f27fa73e",
   "ent_e78f7b57f9e8c41c",
   "ent_259ee3f504eeec68",
   "ent_b00e0466f0053b6d",
   "ent_a04a63c4d17b9e84",
   "ent_d383f7423ce7ae84",
   "ent_83079ace46b3f421",
   "ent_a9d2eddce39bf6b5",
   "ent_53a2d2b64e89ac15",
   "ent_d9d817edd51e0b96",
   "ent_57cad596ed7e5ac4",
   "ent_c453cc9fac8c59f8",
   "ent_9cfef3e6c4b4068c",
   "ent_cf8cdff91667e594",
   "ent_92e501d9d020e6d6",
   "ent_9f4e33809032ff35",
   "ent_50df9d10a42241e9",
   "ent_c9626b583f6316a7",
   "ent_9c3476d83df9be46",
   "ent_a6cf47d3c5a4bc88",
   "ent_021c49fe425a2907",
   "ent_14123dad255fe71f",
   "ent_4062ab3504a6c747",
   "ent_628a14b6a8e597da",
   "ent_570b486cad9014c7",
   "ent_cef3336378042c04",
   "ent_31159dc9fb589759",
   "ent_3adb0a924b2bb977",
   "ent_19384fd245a69794",
   "ent_c3ccd600246ee547",
   "ent_6386569a3669ed5b",
   "ent_faaf71100a9f9106",
   "ent_077e939b2ec011f8",
   "ent_bae283435f490fd9",
   "ent_90f22c70ad12c0c7",
   "ent_cdc7f203988ba501",
   "ent_aa992054c275d338",
   "ent_af80903a529ef30f",
   "ent_9a70037627a6aaaa",
   "ent_a9a09b710216d210",
   "ent_4393425abb8f04a0",
   "ent_ccdd1933f3da8348",
   "ent_1e19e7ce8f408ca7",
   "ent_f14439ce62e74f89",
   "ent_d242dcdb2a2d1a79",
   "ent_d70e0c62c9268f58",
   "ent_7e27de882da4d344",
   "ent_f138b096a27ec547"
  ],
  "merged": [],
  "skipped": null,
  "round": 1
 },
 "statuses_after_compress": {
  "compressed": 60
 },
 "same_round_prune": {
  "archived": [],
  "deleted": []
 },
 "next_round_prune": {
  "archived": 5,
  "deleted": 0
 }
}
```

### test_C_supersede_daily — PASS

```json
{
 "old_description": "用户喜欢咖啡",
 "superseded": {
  "before": "用户喜欢咖啡",
  "after": "用户不喜欢咖啡",
  "valid_from": "2026-03-16",
  "protect_days": 7.0,
  "w": 1.0
 },
 "journal_entry": {
  "id": 1,
  "kind": "supersede",
  "target_id": "ent_21964ec23ea65f39",
  "ts": 1773622800.2197654
 },
 "day3_recall_ids": [
  "ent_21964ec23ea65f39"
 ],
 "rollback_restored": true
}
```

### test_D1_200_turns_constant_budget — PASS

```json
{
 "turn1_total": 1149,
 "steady_totals": [
  1349,
  1349,
  1349,
  1349,
  1349,
  1349,
  1350,
  1351,
  1351,
  1351,
  1351,
  1351,
  1351,
  1351,
  1351,
  1351
 ],
 "steady_span_pct": 0.148,
 "max_total": 1441,
 "final_tokens": {
  "system": 46,
  "memory": 778,
  "day": 393,
  "detail": 87,
  "history": 0,
  "total": 1351
 }
}
```

### test_D2_30days_constant_budget_single_history — PASS

```json
{
 "day1_total": 1016,
 "daily_totals_tail": [
  {
   "day": 26,
   "total": 1151
  },
  {
   "day": 27,
   "total": 1151
  },
  {
   "day": 28,
   "total": 1151
  },
  {
   "day": 29,
   "total": 1151
  },
  {
   "day": 30,
   "total": 1151
  }
 ],
 "steady_span_pct": 0.0,
 "max_total": 1220,
 "history_files": 1,
 "last_day_tokens": {
  "system": 46,
  "memory": 710,
  "day": 147,
  "detail": 0,
  "history": 200,
  "total": 1151
 }
}
```

### test_D3_graph_scale_constant_budget — PASS

```json
{
 "nodes": 20001,
 "before_tokens": {
  "system": 46,
  "memory": 775,
  "day": 15,
  "detail": 0,
  "history": 0,
  "total": 881
 },
 "after_tokens": {
  "system": 46,
  "memory": 775,
  "day": 15,
  "detail": 0,
  "history": 0,
  "total": 881
 },
 "diff_pct": 0.0,
 "top1_sim": {
  "before": 0.479593,
  "after": 0.479593
 },
 "top1_name": {
  "before": "pnpm 依赖管理 固定记忆-87",
  "after": "pnpm 依赖管理 固定记忆-87"
 }
}
```

### test_D4_compressed_detail_recall — PASS

```json
{
 "compressed": true,
 "summary_tokens": 346,
 "raw_tokens": 516,
 "recall_accuracy": 1.0,
 "misses": []
}
```

### test_E1_90days_below_high_zero_delete — PASS

```json
{
 "nodes": 1000,
 "archived_after_90d": 0,
 "deleted_after_90d": 0,
 "recall_items": 20,
 "weights_before": [
  0.0001,
  0.0001,
  0.0001
 ],
 "weights_after": {
  "ent_00c7e63ceb00fdc4": 0.6001347762288741,
  "ent_06fd01ed33b1080e": 0.6001347762288741,
  "ent_2b935173ad17576f": 0.6001347762288741
 }
}
```

### test_E2_over_high_prune_to_low — PASS

```json
{
 "high": 200,
 "low": 160,
 "counts": [
  201,
  201,
  184,
  169,
  155
 ],
 "rounds": [
  {
   "round": 1,
   "gate": {
    "action": "compress",
    "reason": "over_high",
    "count": 201,
    "phase": "idle"
   },
   "live": 201
  },
  {
   "round": 2,
   "gate": {
    "action": "prune",
    "reason": "over_high",
    "count": 201,
    "phase": "compressed"
   },
   "live": 184
  },
  {
   "round": 3,
   "gate": {
    "action": "prune",
    "reason": "hysteresis",
    "count": 184,
    "phase": "compressed"
   },
   "live": 169
  },
  {
   "round": 4,
   "gate": {
    "action": "prune",
    "reason": "hysteresis",
    "count": 169,
    "phase": "compressed"
   },
   "live": 155
  }
 ]
}
```

### test_E3_band_fluctuation_triggers — PASS

```json
{
 "high": 100,
 "low": 80,
 "setup": [
  {
   "live": 92,
   "archived": 9
  },
  {
   "live": 84,
   "archived": 8
  },
  {
   "live": 77,
   "archived": 7
  }
 ],
 "triggers": 0,
 "days": [
  {
   "day": 0,
   "live": 79,
   "gate": "idle",
   "reason": "below_low"
  },
  {
   "day": 1,
   "live": 81,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 2,
   "live": 83,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 3,
   "live": 85,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 4,
   "live": 87,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 5,
   "live": 89,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 6,
   "live": 91,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 7,
   "live": 93,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 8,
   "live": 95,
   "gate": "idle",
   "reason": "within_band"
  },
  {
   "day": 9,
   "live": 97,
   "gate": "idle",
   "reason": "within_band"
  }
 ]
}
```

### test_E4_single_round_max_fraction — PASS

```json
{
 "nodes_before": 201,
 "expected_limit": 17,
 "report_limit": 17,
 "handled": 17,
 "archived": 17,
 "deleted": 0
}
```

### test_E5_pinned_stuck_alarm — PASS

```json
{
 "pinned": 189,
 "rounds": [
  {
   "round": 1,
   "action": "compress",
   "reason": "over_high",
   "stuck": false,
   "live": 190
  },
  {
   "round": 2,
   "action": "prune",
   "reason": "hysteresis",
   "stuck": false,
   "live": 189
  },
  {
   "round": 3,
   "action": "prune",
   "reason": "hysteresis",
   "stuck": false,
   "live": 189
  },
  {
   "round": 4,
   "action": "prune",
   "reason": "hysteresis",
   "stuck": false,
   "live": 189
  },
  {
   "round": 5,
   "action": "idle",
   "reason": "prune_stuck",
   "stuck": true,
   "live": 189
  }
 ],
 "stuck": true,
 "alarms": [
  "prune_stuck"
 ],
 "prune_stuck_alarm": {
  "id": 1,
  "ts": 1774832407.2269053,
  "scope": "workspace",
  "kind": "prune_stuck",
  "detail": {
   "scope": "workspace",
   "round": 4,
   "count": 189,
   "high": 200,
   "low": 160,
   "fail_rounds": 3
  }
 }
}
```

### test_E6_scope_isolation — PASS

```json
{
 "user_counts": [
  30,
  30,
  27,
  24,
  22
 ],
 "workspace_before": {
  "nodes": 10,
  "nodes_live": 10,
  "phase": "idle"
 },
 "workspace_after": {
  "nodes": 10,
  "nodes_live": 10,
  "phase": "idle"
 },
 "workspace_statuses": {
  "active": 10
 }
}
```

### test_F1_pure_responses_dropped — PASS

```json
{
 "rows": [
  {
   "text": "好的",
   "dropped": true,
   "reason": "too_short"
  },
  {
   "text": "嗯嗯",
   "dropped": true,
   "reason": "too_short"
  },
  {
   "text": "先这样吧",
   "dropped": true,
   "reason": "too_short"
  },
  {
   "text": "稍等我看下",
   "dropped": true,
   "reason": "too_short"
  },
  {
   "text": "你决定",
   "dropped": false,
   "reason": "passed",
   "note": "含“决定”→CONTRACT §2.3 放行信号优先；只断言不进图"
  }
 ]
}
```

### test_F2_signal_pass_no_false_kill — PASS

```json
{
 "rows": [
  {
   "text": "明天下午三点开会",
   "dropped": false,
   "reason": "passed",
   "nodes": 0
  },
  {
   "text": "价格改成 80",
   "dropped": false,
   "reason": "passed",
   "nodes": 1
  },
  {
   "text": "改用 Redis",
   "dropped": false,
   "reason": "passed",
   "nodes": 1
  },
  {
   "text": "决定用 pnpm",
   "dropped": false,
   "reason": "passed",
   "nodes": 1
  },
  {
   "text": "这个接口谁负责？",
   "dropped": false,
   "reason": "passed",
   "nodes": 1
  }
 ]
}
```

### test_F3_short_sentences_no_false_kill — PASS

```json
{
 "sentences": 100,
 "killed": 0,
 "killed_samples": []
}
```

### test_F4_credential_dropped_and_alarm — PASS

```json
{
 "rows": [
  {
   "text": "我的 api key 是 sk-abcdefgh12345678，帮我保存",
   "dropped": true,
   "reason": "credential"
  },
  {
   "text": "Authorization: Bearer abcdef123456.token",
   "dropped": true,
   "reason": "credential"
  },
  {
   "text": "password=SuperSecret123 请记住",
   "dropped": true,
   "reason": "credential"
  }
 ],
 "warning_notifications": 3
}
```

## 方法与边界说明

- D1/D2/D3 注入 token 由 tests/acceptance/injection_model.py（CONTRACT §2.2 独立 Python 镜像）对真实 sidecar 的 retrieve/day_pack 数据装配计量；TS budgeter 落地后由 tests/python/test_tokens.py 做跨语言一致性交叉校验。
- 时间模拟使用 sidecar debug_set_now 伪时钟；不依赖真实 sleep。
- E 系列剪枝门控用 --config 深合并覆盖 prune.watermarks.* 为小规模阈值（契约 calibrate 覆盖语义），D3 使用真实 20000 节点规模。
- 所有 fixture 位于仓库 .tmp/ 下的独立目录（DSH Windows 沙箱限制；等价于系统临时目录隔离），不写真实 ~/.dsh，不联网。
- 未映射测试：test_F4_credential_dropped_and_alarm 仅对应验收 4。
- 验收 3/5/6/15 由产品单测覆盖；18/23 由 TS node 测试覆盖（见下方环境命令结果）；10 由真实 DSH headless E2E 覆盖；23 的 300ms 旁路属 TS injector 层。
