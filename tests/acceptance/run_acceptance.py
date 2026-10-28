"""独立验收入口：跑 tests/acceptance/test_cases.py 全部黑盒用例，输出 JSON + Markdown 报告。

用法：
    py -3 tests/acceptance/run_acceptance.py                    # 全量
    py -3 tests/acceptance/run_acceptance.py -k D1              # 只跑匹配的用例
    py -3 tests/acceptance/run_acceptance.py --no-report        # 只跑不写报告
    py -3 tests/acceptance/run_acceptance.py --json-out .tmp/acc.json --report docs/ACCEPTANCE-REPORT.md

退出码：0=全部通过；1=有失败/错误；2=无失败但有跳过（未验证）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import evidence  # noqa: E402
import test_cases  # noqa: E402

DEFAULT_JSON_OUT = os.path.join(REPO_ROOT, "docs", "acceptance.json")
DEFAULT_REPORT = os.path.join(REPO_ROOT, "docs", "ACCEPTANCE-REPORT.md")
DEFAULT_ENV_CHECKS = os.path.join(REPO_ROOT, "docs", "env-checks.json")

# 集成/验收期间由独立验证者发现并已修复的缺陷（写入报告留痕）
INTEGRATION_FIXES = [
    {
        "id": "FIX-1",
        "title": "config/default.json 带 UTF-8 BOM → 默认配置整体失效",
        "detail": (
            "config/default.json 被重写时引入 BOM，sidecar/config.py 的 load_defaults() 以 encoding='utf-8' 读取，"
            "JSONDecodeError 被静默吞掉返回 {}，所有默认配置（injection/prune/llm/day 等）丢失。"
            "修复：default.json 重写为无 BOM + load_defaults/parse_override 改 utf-8-sig 双保险；"
            "回归断言：test_tokens.DefaultConfigTest（无 BOM + totalHardTokens=3000 等默认值）。"
        ),
        "status": "fixed",
    },
    {
        "id": "FIX-2",
        "title": "graph.py 候选选择缺陷：图谱 20000 节点时高相似节点被漏召回（D3 注入量漂移 15.9%）",
        "detail": (
            "graph.py `_candidate_ids` 只取 8 个最小倒排桶做候选，20k 节点时桶被 bulk 噪声占满，"
            "100 个高相似固定记忆节点一个都进不了候选，retrieve 注入 token 差 15.9%（D3 硬指标 17 不达标）。"
            "修复：名字词法倒排（ASCII 词 + CJK 2-gram）∪ 哈希维度倒排（遍历全部 query 维度，cap 调优），"
            "并补 graph.py 缺失的 `import re`（名字倒排 NameError）。回归：test_D3_graph_scale_constant_budget。"
        ),
        "status": "fixed",
    },
    {
        "id": "FIX-3",
        "title": "server.py 未知方法返回 -32603（RpcError 关键字参数用错，-32601 永远出不来）",
        "detail": (
            "dispatch 里 `RpcError(..., code=-32601)`，但 base.RpcError 形参是 rpc_code，"
            "TypeError 被通用 except 捕获成 -32603 internal error，违反 CONTRACT §3 错误码约定。"
            "修复：改为 rpc_code=-32601。回归：test_protocol.test_unknown_method_32601。"
        ),
        "status": "fixed",
    },
    {
        "id": "FIX-4",
        "title": "noise.py 凭据正则漏 Bearer token（凭据可能进图谱/日志）",
        "detail": (
            "CONTRACT §2.3 要求凭据正则覆盖 `Bearer\\s+\\S+`；实测 `Authorization: Bearer abcdef123456.token` "
            "因命中数字放行信号而 passed（未丢弃）。修复：新增大小写不敏感 Bearer 模式（含数字≥8 字符或无数字≥20 字符），"
            "同时保证普通英文 `bearer token is required` 不误伤。回归：test_F4_credential_dropped_and_alarm。"
        ),
        "status": "fixed",
    },
    {
        "id": "FIX-5",
        "title": "ingest 同名实体 upsert 绕过改口检测（C 场景旧说法仍可检索）",
        "detail": (
            "Day2 「我现在不喜欢喝咖啡了，改成喝茶」抽出的候选名与 Day1 同名，service.ingest 走"
            "「已有同名实体 → 追加 description」短路，未进入 supersede 判定：description 变旧+新拼接、"
            "无 Journal supersede、旧值仍被检索（违反 R6）。验收探针发现后由实现方修复；"
            "现回归：test_C_supersede_daily（description/valid_from=Day2/w=max/protect_until+7d/Journal/rollback/R6 全链路）。"
        ),
        "status": "fixed",
    },
]

# --------------------------------------------------------------------------- #
# 27 条验收标准（docs/ACCEPTANCE-CRITERIA.md）与用例映射
# --------------------------------------------------------------------------- #
CRITERIA = [
    ("1", "插件启用后无需干预，自主完成写入/召回/改口/压缩/遗忘", ["test_A_memory_loop"]),
    ("2", "turn 结束后异步触发写入，不阻塞对话响应", ["test_A_memory_loop"]),
    ("3", "新记忆初始权重 1.0；pin/“记住”权重 2.0 且 pinned=true", []),
    ("4", "写入四道闸门按序执行（工具摘要/凭据丢弃告警/短句丢弃/>0.95 去重）",
     ["test_F1_pure_responses_dropped", "test_F2_signal_pass_no_false_kill",
      "test_F3_short_sentences_no_false_kill", "test_F4_credential_dropped_and_alarm"]),
    ("5", "实体类型沿用 LightRAG 默认分类并额外支持 Preference/Decision", []),
    ("6", "衰减读时惰性求值 w(t)=min(10,w·2^(-Δt/7d))，不依赖定时任务", []),
    ("7", "激活仅对 injected_ids；w=min(10,w+0.5·(1+boost))", ["test_A_memory_loop"]),
    ("8", "排序 score=sim×(0.3+0.7×min(1,w/5))，pinned×1.5，workspace×1.2", ["test_A_memory_loop"]),
    ("8b", "F1 纯应答不进图（硬指标）", ["test_F1_pure_responses_dropped"]),
    ("8c", "F2 放行信号全部入库、误杀=0（硬指标）", ["test_F2_signal_pass_no_false_kill"]),
    ("8d", "F3 随机 100 条 8~30 字短句误杀=0", ["test_F3_short_sentences_no_false_kill"]),
    ("9", "删除三阶段：先压缩→隔轮才归档→冷区超期才真删；禁止同轮压完即删（R4）",
     ["test_B_compress_before_prune"]),
    ("10", "卸载时撤销注册/监听、停 sidecar、保留数据文件", []),
    ("11", "写入时同 scope 近邻 top-5，sim∈[0.62,0.95) 才送 LLM 判矛盾", ["test_C_supersede_daily"]),
    ("12", "矛盾置信度 ≥0.7 触发改写；含否定词阈值 0.6", ["test_C_supersede_daily"]),
    ("13", "改写六步：edit→valid_from/updated→w=max→同步向量→Journal→protect_until+7d（R6）",
     ["test_C_supersede_daily"]),
    ("14", "改口最新为准、静默替换；不同 scope 各自保留", ["test_C_supersede_daily"]),
    ("15", "每日首次会话扫近 30 天矛盾对兜底，产出 conflict_report", []),
    ("16", "改口 Journal 记录 before/after/date/confidence/source_turn_id，7 天内可回滚",
     ["test_C_supersede_daily"]),
    ("17", "注入 token 总量硬顶 3000，不随使用时长/轮数/图规模增长（硬指标）",
     ["test_D1_200_turns_constant_budget", "test_D2_30days_constant_budget_single_history",
      "test_D3_graph_scale_constant_budget"]),
    ("18", "memory_context 动态档位 1024/1536/1792/2048（硬指标）", []),
    ("19", "day_context 恒定 ≤1024（512/312/200），当天超界给全量压缩摘要（硬指标）",
     ["test_D2_30days_constant_budget_single_history", "test_D4_compressed_detail_recall"]),
    ("20", "全量压缩 ≤360 token、保留最近 2 条原文、原文不删、限流 30min/20 轮",
     ["test_D4_compressed_detail_recall"]),
    ("21", "历史滚动摘要永远只有一份 ≤200 token，绝不逐日堆积（R8）",
     ["test_D2_30days_constant_budget_single_history"]),
    ("22", "注入前本地计量；超 3000 按优先级截断并告警；面板显示实测 token（硬指标）",
     ["test_D1_200_turns_constant_budget", "test_D3_graph_scale_constant_budget"]),
    ("23", "召回为空不注入任何标签；召回失败/超时 300ms 旁路放行（R7）", []),
    ("24", "图谱规模 < high 完全不剪枝（时间不是删除理由），可召回、激活回升（E1）",
     ["test_E1_90days_below_high_zero_delete"]),
    ("25", "图谱规模 > high 开启剪枝并剪到 <= low 停；> hard 强制剪+告警",
     ["test_E2_over_high_prune_to_low"]),
    ("26", "双水位迟滞/单轮 ≤8%/连续 3 轮剪不到 low 停止+告警/三档按 scope 独立计量",
     ["test_E3_band_fluctuation_triggers", "test_E4_single_round_max_fraction",
      "test_E5_pinned_stuck_alarm", "test_E6_scope_isolation"]),
    ("27", "压缩/剪枝免疫 pinned、Preference、活跃 workspace、近 7 天改过、新建 3 天；dry_run 只出报告",
     ["test_B_compress_before_prune"]),
]

HARD_INDICATORS = [
    ("8b", "纯应答不进图", ["test_F1_pure_responses_dropped"]),
    ("8c", "放行信号全部入库、误杀=0", ["test_F2_signal_pass_no_false_kill"]),
    ("17", "注入总量硬顶 3000 且与时长/轮数/规模无关",
     ["test_D1_200_turns_constant_budget", "test_D2_30days_constant_budget_single_history",
      "test_D3_graph_scale_constant_budget"]),
    ("18", "memory_context 动态档位", []),  # TS budgeter node 测试覆盖（见 test_tokens 交叉校验）
    ("19", "day_context 恒定预算", ["test_D2_30days_constant_budget_single_history",
                                "test_D4_compressed_detail_recall"]),
    ("22", "超预算优先级截断 + 告警", ["test_D1_200_turns_constant_budget",
                                   "test_D3_graph_scale_constant_budget"]),
    ("E1", "<high 时 90 天零删除", ["test_E1_90days_below_high_zero_delete"]),
]

TEST_TO_CRITERIA: dict[str, list[str]] = {}
for _cid, _summary, _tests in CRITERIA:
    for _test in _tests:
        TEST_TO_CRITERIA.setdefault(_test, [])
        if _cid not in TEST_TO_CRITERIA[_test]:
            TEST_TO_CRITERIA[_test].append(_cid)


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict] = {}
        self._started_at: dict[str, float] = {}

    def _key(self, test: unittest.TestCase) -> str:
        return getattr(test, "_testMethodName", str(test))

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._started_at[self._key(test)] = time.time()

    def _finish(self, test: unittest.TestCase, status: str, message: str = "") -> None:
        key = self._key(test)
        duration = time.time() - self._started_at.get(key, time.time())
        self.records.setdefault(key, {})
        self.records[key].update({"status": status, "message": message, "duration_s": round(duration, 2)})
        self.records[key]["evidence"] = evidence.get(key)

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._finish(test, "pass")

    def addFailure(self, test: unittest.TestCase, err) -> None:  # noqa: ANN001
        super().addFailure(test, err)
        message = "".join(traceback.format_exception_only(err[0], err[1])).strip()
        self._finish(test, "fail", message)

    def addError(self, test: unittest.TestCase, err) -> None:  # noqa: ANN001
        super().addError(test, err)
        message = "".join(traceback.format_exception_only(err[0], err[1])).strip()
        if isinstance(err[1], unittest.SkipTest):
            message = str(err[1])
        self._finish(test, "error", message)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._finish(test, "skip", reason)


def flatten_suite(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten_suite(test)
        else:
            yield test


def build_suite(pattern: str = "") -> unittest.TestSuite:
    loader = unittest.TestLoader()
    module_suite = loader.loadTestsFromModule(test_cases)
    if not pattern:
        return module_suite
    selected = [test for test in flatten_suite(module_suite) if pattern.lower() in test.id().lower()]
    return unittest.TestSuite(selected)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_payload(
    cases: list[dict],
    duration: float,
    command: str,
    exit_code: int,
    env_checks: dict | None = None,
) -> dict:
    by_test = {case["test"]: case for case in cases}
    def tests_status(tests: list[str]) -> str:
        statuses = [by_test[test]["status"] for test in tests if test in by_test]
        if not statuses:
            return "other_suite"  # 无独立黑盒用例 → 由产品单测/TS 测试/E2E 覆盖（见环境命令结果）
        if any(status in ("fail", "error") for status in statuses):
            return "fail"
        if all(status == "skip" for status in statuses):
            return "skip"
        if any(status == "skip" for status in statuses):
            return "partial"
        return "pass"

    criteria = [
        {"id": cid, "summary": summary, "tests": tests, "status": tests_status(tests)}
        for cid, summary, tests in CRITERIA
    ]
    hard = [
        {"id": hid, "summary": summary, "tests": tests, "status": tests_status(tests)}
        for hid, summary, tests in HARD_INDICATORS
    ]
    summary = {
        "tests": len(cases),
        "passed": sum(1 for case in cases if case["status"] == "pass"),
        "failed": sum(1 for case in cases if case["status"] == "fail"),
        "errors": sum(1 for case in cases if case["status"] == "error"),
        "skipped": sum(1 for case in cases if case["status"] == "skip"),
        "duration_s": round(duration, 2),
    }
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": command,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "repo_root": REPO_ROOT,
        "exit_code": exit_code,
        "summary": summary,
        "cases": cases,
        "criteria": criteria,
        "hard_indicators": hard,
        "test_to_criteria": TEST_TO_CRITERIA,
        "integration_fixes": INTEGRATION_FIXES,
        "env_checks": env_checks or {},
        "notes": [
            "D1/D2/D3 注入 token 由 tests/acceptance/injection_model.py（CONTRACT §2.2 独立 Python 镜像）"
            "对真实 sidecar 的 retrieve/day_pack 数据装配计量；TS budgeter 落地后由 tests/python/test_tokens.py "
            "做跨语言一致性交叉校验。",
            "时间模拟使用 sidecar debug_set_now 伪时钟；不依赖真实 sleep。",
            "E 系列剪枝门控用 --config 深合并覆盖 prune.watermarks.* 为小规模阈值（契约 calibrate 覆盖语义），"
            "D3 使用真实 20000 节点规模。",
            "所有 fixture 位于仓库 .tmp/ 下的独立目录（DSH Windows 沙箱限制；等价于系统临时目录隔离），"
            "不写真实 ~/.dsh，不联网。",
            "未映射测试：test_F4_credential_dropped_and_alarm 仅对应验收 4。",
            "验收 3/5/6/15 由产品单测覆盖；18/23 由 TS node 测试覆盖（见下方环境命令结果）；"
            "10 由真实 DSH headless E2E 覆盖；23 的 300ms 旁路属 TS injector 层。",
        ],
    }


def _status_icon(status: str) -> str:
    return {
        "pass": "PASS",
        "fail": "FAIL",
        "error": "ERROR",
        "skip": "SKIP",
        "not_run": "未覆盖",
        "other_suite": "其它套件覆盖",
        "partial": "PARTIAL",
    }.get(status, status)


def render_report(payload: dict) -> str:
    lines: list[str] = []
    summary = payload["summary"]
    lines.append("# dsh-graph-memory 独立验收报告")
    lines.append("")
    lines.append(f"- 生成时间：{payload['generated_at']}")
    lines.append(f"- 运行环境：Python {payload['python']} / {payload['platform']}")
    lines.append(f"- 仓库：`{payload['repo_root']}`")
    lines.append("")
    lines.append("## 运行命令")
    lines.append("")
    lines.append("```powershell")
    lines.append("# 全量独立验收（真实 sidecar 子进程 + JSON-RPC 黑盒）")
    lines.append("py -3 tests/acceptance/run_acceptance.py")
    lines.append("")
    lines.append("# 单用例复现（示例）")
    lines.append("py -3 tests/acceptance/run_acceptance.py -k test_D3")
    lines.append("")
    lines.append("# 协议/令牌交叉校验（单独）")
    lines.append("py -3 -m unittest tests.python.test_protocol tests.python.test_tokens -v")
    lines.append("```")
    lines.append("")
    lines.append("## 结果汇总")
    lines.append("")
    lines.append("| 总数 | 通过 | 失败 | 错误 | 跳过 | 耗时 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(
        f"| {summary['tests']} | {summary['passed']} | {summary['failed']} | {summary['errors']} | "
        f"{summary['skipped']} | {summary['duration_s']}s |"
    )
    lines.append("")
    icon = "✅" if payload["exit_code"] == 0 else ("⚠️" if payload["exit_code"] == 2 else "❌")
    lines.append(f"退出码：`{payload['exit_code']}`（0=全部通过，1=有失败，2=有跳过） {icon}")
    lines.append("")
    lines.append("## 用例明细")
    lines.append("")
    lines.append("| 用例 | 状态 | 耗时 | 验收编号 | 失败原因/关键证据 |")
    lines.append("|---|---|---|---|---|")
    for case in payload["cases"]:
        criteria = ", ".join(case.get("criteria") or []) or "-"
        detail = case.get("message") or ""
        if not detail:
            evidence_items = list((case.get("evidence") or {}).items())[:2]
            detail = "; ".join(f"{key}={_short(value)}" for key, value in evidence_items)
        lines.append(
            f"| `{case['test']}` | {_status_icon(case['status'])} | {case.get('duration_s', 0)}s | "
            f"{criteria} | {_inline(detail)} |"
        )
    lines.append("")
    lines.append("## 27 条验收标准映射")
    lines.append("")
    lines.append("| 编号 | 验收语义 | 对应用例 | 结果 |")
    lines.append("|---|---|---|---|")
    for criterion in payload["criteria"]:
        tests = ", ".join(f"`{test}`" for test in criterion["tests"]) or "（由单测/TS 测试覆盖）"
        lines.append(f"| {criterion['id']} | {criterion['summary']} | {tests} | {_status_icon(criterion['status'])} |")
    lines.append("")
    lines.append("## 硬指标")
    lines.append("")
    lines.append("| 硬指标 | 含义 | 对应用例 | 结果 |")
    lines.append("|---|---|---|---|")
    for indicator in payload["hard_indicators"]:
        tests = ", ".join(f"`{test}`" for test in indicator["tests"]) or "（TS node 测试/交叉校验覆盖）"
        lines.append(f"| {indicator['id']} | {indicator['summary']} | {tests} | {_status_icon(indicator['status'])} |")
    lines.append("")

    lines.append("")
    lines.append("## 其它环境/交叉校验命令结果")
    lines.append("")
    env_checks = payload.get("env_checks") or {}
    checks = env_checks.get("checks") or []
    if checks:
        lines.append("| 命令 | 结果 | 退出码 | 说明 |")
        lines.append("|---|---|---|---|")
        for check in checks:
            lines.append(
                f"| `{check.get('command')}` | {_inline(str(check.get('summary') or ''))} | "
                f"{check.get('exit_code', '-')} | {_inline(str(check.get('note') or ''))} |"
            )
        lines.append("")
    if env_checks.get("cross_checks"):
        lines.append("交叉一致性校验：")
        lines.append("")
        for cross in env_checks["cross_checks"]:
            lines.append(f"- {cross}")
        lines.append("")
    if env_checks.get("criteria_coverage_notes"):
        lines.append("其它测试套件对未直接映射验收条目的覆盖说明：")
        lines.append("")
        for note in env_checks["criteria_coverage_notes"]:
            lines.append(f"- {note}")
        lines.append("")

    fixes = payload.get("integration_fixes") or []
    if fixes:
        lines.append("## 集成/验收期间发现并已修复的缺陷")
        lines.append("")
        for fix in fixes:
            lines.append(f"### {fix['id']} [{fix.get('status', 'fixed')}] {fix['title']}")
            lines.append("")
            lines.append(fix["detail"])
            lines.append("")

    failures = [case for case in payload["cases"] if case["status"] in ("fail", "error")]
    if failures:
        lines.append("## 失败用例与原因")
        lines.append("")
        for case in failures:
            lines.append(f"### `{case['test']}`（{_status_icon(case['status'])}）")
            lines.append("")
            lines.append("```")
            lines.append(case.get("message") or "（无错误信息）")
            lines.append("```")
            lines.append("")
            lines.append(f"复现：`py -3 tests/acceptance/run_acceptance.py -k {case['test']}`")
            lines.append("")

    skips = [case for case in payload["cases"] if case["status"] == "skip"]
    if skips:
        lines.append("## 跳过用例")
        lines.append("")
        for case in skips:
            lines.append(f"- `{case['test']}`：{case.get('message') or '未实现'}")
        lines.append("")

    lines.append("## 证据（断言实测值）")
    lines.append("")
    for case in payload["cases"]:
        lines.append(f"### {case['test']} — {_status_icon(case['status'])}")
        lines.append("")
        evidence_data = case.get("evidence") or {}
        if evidence_data:
            lines.append("```json")
            lines.append(json.dumps(evidence_data, ensure_ascii=False, indent=1, default=str))
            lines.append("```")
        else:
            lines.append("（无证据记录）")
        lines.append("")

    lines.append("## 方法与边界说明")
    lines.append("")
    for note in payload.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _short(value, limit: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _inline(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-graph-memory 独立验收测试")
    parser.add_argument("-k", "--filter", default="", help="只运行 id 包含该子串的用例")
    parser.add_argument("--json-out", default=DEFAULT_JSON_OUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--env-checks", default=DEFAULT_ENV_CHECKS, help="其它环境/交叉校验命令结果 JSON（可选）")
    parser.add_argument("--no-report", action="store_true", help="只跑用例，不写 JSON/Markdown")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    evidence.reset()
    suite = build_suite(args.filter)
    count = sum(1 for _ in flatten_suite(suite))
    if count == 0:
        print(f"[acceptance] 没有匹配 {args.filter!r} 的用例")
        return 1
    print(f"[acceptance] 运行 {count} 个独立验收用例（真实 sidecar 子进程 / JSON-RPC 黑盒）...")
    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
        resultclass=RecordingResult,
        stream=sys.stdout,
    )
    started = time.time()
    result: RecordingResult = runner.run(suite)  # type: ignore[assignment]
    duration = time.time() - started

    cases = []
    for key, record in result.records.items():
        cases.append(
            {
                "test": key,
                "case": key[len("test_"):] if key.startswith("test_") else key,
                "status": record["status"],
                "duration_s": record.get("duration_s", 0.0),
                "message": record.get("message", ""),
                "criteria": TEST_TO_CRITERIA.get(key, []),
                "evidence": record.get("evidence", {}),
            }
        )
    cases.sort(key=lambda case: case["test"])
    failures = sum(1 for case in cases if case["status"] in ("fail", "error"))
    skipped = sum(1 for case in cases if case["status"] == "skip")
    exit_code = 0 if failures == 0 and skipped == 0 else (1 if failures else 2)
    command = "py -3 " + os.path.relpath(os.path.abspath(__file__), REPO_ROOT) + (
        f" -k {args.filter}" if args.filter else ""
    )
    env_checks: dict = {}
    if args.env_checks and os.path.isfile(args.env_checks):
        try:
            with open(args.env_checks, "r", encoding="utf-8") as fh:
                env_checks = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[acceptance] 警告：env-checks 读取失败 {args.env_checks}: {exc}")
            env_checks = {}
    payload = build_payload(cases, duration, command, exit_code, env_checks=env_checks)
    payload["filter"] = args.filter

    if not args.no_report:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(render_report(payload))
        print(f"[acceptance] JSON  -> {args.json_out}")
        print(f"[acceptance] 报告  -> {args.report}")

    summary = payload["summary"]
    print(
        f"[acceptance] 完成：{summary['passed']} 通过 / {summary['failed']} 失败 / "
        f"{summary['errors']} 错误 / {summary['skipped']} 跳过，耗时 {summary['duration_s']}s（退出码 {exit_code}）"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
