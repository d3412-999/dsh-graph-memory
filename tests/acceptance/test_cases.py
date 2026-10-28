"""dsh-graph-memory 独立验收用例（黑盒：只经真实 sidecar 子进程的 JSON-RPC）。

映射用户 27 条验收标准（见 docs/ACCEPTANCE-CRITERIA.md）：
    A  -> 1、2、7、8（另有 R3 元数据断言）
    B  -> 9、27（R4）
    C  -> 11~16（R6）
    D1 -> 17、22
    D2 -> 17、19、21（R8）
    D3 -> 17、22
    D4 -> 19、20
    E1 -> 24（硬指标 E1）
    E2 -> 25
    E3/E4/E5/E6 -> 26
    F1 -> 8b、4      F2 -> 8c、4      F3 -> 8d、4      F4 -> 4（凭据告警）

时间模拟全部用 sidecar 绑定的 `debug_set_now` 伪时钟（不真实 sleep）；
E 系列用 `--config` 覆盖 `prune.watermarks.*` 为小值（契约允许 calibrate 覆盖），
避免为验收造 2 万节点（D3 除外，D3 必须覆盖 100→20000 规模）。
"""
from __future__ import annotations

import math
import os
import re
import unittest

import evidence
import injection_model
from rpc_client import (
    MethodMissing,
    RpcError,
    SidecarFixture,
    generate_short_sentences,
    spawn_or_skip,
)

FIXED_MEMORY_PREFIX = "pnpm 依赖管理 固定记忆"
QUERY = "pnpm 依赖管理 配置 版本"

# B 用的 30 个互不相同的主题前缀（每主题 2 个节点；避免 compress 时全部 merge 成 1 条）
DISTINCT_PREFIXES = [
    "苹果电脑配置", "量子计算研究", "咖啡口味偏好", "咖啡机采购", "数据库选型",
    "缓存策略调整", "日志采集方案", "权限模型设计", "灰度发布流程", "压测报告结论",
    "构建脚本优化", "接口超时排查", "部署流水线", "监控告警规则", "备份恢复演练",
    "代码评审规范", "分支管理策略", "版本发布节奏", "依赖管理工具", "容器镜像仓库",
    "前端构建缓存", "后端服务拆分", "消息队列选型", "定时任务调度", "文件存储方案",
    "搜索索引设计", "推荐算法实验", "用户画像标签", "支付回调幂等", "订单状态机",
]

METADATA_PATTERNS = (
    re.compile(r"\bw\s*=", re.IGNORECASE),
    re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\bpin(ned)?\b", re.IGNORECASE),
    re.compile(r"\bvalid_from\b", re.IGNORECASE),
    re.compile(r"\bprotect_until\b", re.IGNORECASE),
)


def pct_diff(a: float, b: float) -> float:
    base = max(abs(float(a)), abs(float(b)), 1.0)
    return abs(float(a) - float(b)) / base * 100.0


def ceil_fraction(count: int, fraction: float = 0.08) -> int:
    return int(math.ceil(int(count) * float(fraction))) if count > 0 else 0


class AcceptanceBase(unittest.TestCase):
    """公共夹具：每个用例一个隔离 sidecar；证据写入 evidence 模块。"""

    maxDiff = None

    def setUp(self) -> None:
        evidence.set_current(self._testMethodName)

    # -- 夹具 ---------------------------------------------------------
    def spawn(
        self,
        *,
        config_overrides: dict | None = None,
        llm_mode: str = "offline",
        llm_script: dict | None = None,
    ) -> SidecarFixture:
        try:
            return spawn_or_skip(
                self,
                config_overrides=config_overrides,
                llm_mode=llm_mode,
                llm_script=llm_script,
                name=self._testMethodName,
            )
        except MethodMissing as exc:  # pragma: no cover - 兜底
            raise unittest.SkipTest(str(exc))

    def record(self, key: str, value) -> None:
        evidence.record(key, value)

    # -- 断言辅助 -----------------------------------------------------
    def rpc_required(self) -> None:
        """sidecar 关键方法存在性探测（未实现 → skip 并说明）。"""
        fixture = getattr(self, "_probe_fixture", None)
        if fixture is None:
            return
        for method in ("retrieve", "ingest", "day_pack", "stats"):
            if not fixture.client.has_method(method):
                raise unittest.SkipTest(f"sidecar 方法未实现: {method}")

    def nodes(self, fixture: SidecarFixture, scope: str = "workspace") -> list[dict]:
        return fixture.list_nodes(scope=scope)

    def statuses(self, fixture: SidecarFixture, scope: str = "workspace") -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes(fixture, scope):
            status = str((node.get("properties") or {}).get("status"))
            counts[status] = counts.get(status, 0) + 1
        return counts

    def live_nodes(self, fixture: SidecarFixture, scope: str = "workspace") -> int:
        return int(fixture.stats()["scopes"][scope]["nodes_live"])

    def precompress(
        self,
        fixture: SidecarFixture,
        count: int,
        *,
        prefix: str,
        scope: str = "workspace",
    ) -> list[dict]:
        """造 count 个「已压缩、低权重」节点（黑盒：debug_seed + debug_entity_set）。"""
        fixture.debug_seed_entities(count, prefix=prefix, scope=scope)
        nodes = self.nodes(fixture, scope=scope)
        self.assertGreaterEqual(len(nodes), count, "debug_seed_entities 造数不足")
        for node in nodes:
            fixture.debug_entity_set(node["id"], {"w": 0.1, "status": "compressed"})
        return nodes


# --------------------------------------------------------------------------- #
# A：记忆闭环（1、2、7、8 + R3）
# --------------------------------------------------------------------------- #
class TestA_MemoryLoop(AcceptanceBase):
    def test_A_memory_loop(self) -> None:
        fx = self.spawn()
        # 2：turn 结束异步写入，2s 内返回 accepted
        import time

        t0 = time.time()
        result = fx.ingest("我们项目决定用 pnpm 管理依赖，不要用 npm。", turn_id="a-1")
        ingest_elapsed = time.time() - t0
        self.assertTrue(result.get("accepted"), result)
        self.assertLess(ingest_elapsed, 2.0, "ingest 必须 2s 内返回（异步语义）")
        self.assertTrue(result.get("node_ids"), f"ingest 未写入实体: {result}")

        # 1/8：新会话问“上次说过用什么包管理器”，能召回
        retrieved = fx.retrieve("上次我说过用什么包管理器管理依赖？")
        items = retrieved.get("items") or []
        texts = [str(item.get("text") or "") for item in items]
        self.assertTrue(items, "retrieve 未命中任何记忆")
        self.assertTrue(any("pnpm" in text for text in texts), f"未命中 pnpm 记忆: {texts}")
        top = items[0]
        for key in ("id", "text", "w", "valid_from", "updated", "sim", "pinned", "scope"):
            self.assertIn(key, top, f"retrieve item 缺少契约字段 {key}: {top}")
        sims = [float(item.get("sim") or 0.0) for item in items]
        self.assertEqual(sims, sorted(sims, reverse=True), "items 必须按 sim/score 降序")

        # 7/R5：只激活 injected_ids；激活后权重上升
        injected = retrieved.get("injected_ids") or [top["id"]]
        activated = fx.activate(injected)
        updated = {entry["id"]: float(entry["w"]) for entry in activated.get("updated") or []}
        self.assertTrue(set(injected) <= set(updated), f"injected_ids 未全部激活: {updated}")
        for entity_id in injected:
            node = next((item for item in items if item["id"] == entity_id), None)
            self.assertIsNotNone(node)
            self.assertGreaterEqual(float(updated[entity_id]) + 1e-9, float(node.get("w") or 0.0))

        # R3：description 禁止出现元数据（w=/日期/pin）
        violations = []
        for node in self.nodes(fx):
            description = str(node.get("description") or "")
            for pattern in METADATA_PATTERNS:
                if pattern.search(description):
                    violations.append({"id": node.get("id"), "description": description, "pattern": pattern.pattern})
        self.assertEqual(violations, [], f"R3 违规：description 含元数据 {violations}")

        self.record("ingest_elapsed_s", round(ingest_elapsed, 4))
        self.record("retrieved_items", [{"name": item.get("name"), "sim": item.get("sim")} for item in items[:5]])
        self.record("activated", updated)
        self.record("r3_violations", violations)


# --------------------------------------------------------------------------- #
# B：压缩先于删除（9、27 / R4）
# --------------------------------------------------------------------------- #
class TestB_CompressBeforePrune(AcceptanceBase):
    def test_B_compress_before_prune(self) -> None:
        fx = self.spawn()
        for prefix in DISTINCT_PREFIXES:
            fx.debug_seed_entities(2, prefix=prefix)
        before = fx.stats()["scopes"]["workspace"]
        self.assertGreaterEqual(before["nodes"], 30)
        fx.advance(86400 * 15)  # w ≈ 0.22 < 0.6，且 age>3 天（可压缩/可归档窗口）

        round1 = fx.round_tick()["round"]
        compressed = fx.compress()
        after_compress = fx.stats()["scopes"]["workspace"]
        statuses = self.statuses(fx)
        self.assertTrue(compressed.get("compressed"), f"compress 未压缩任何节点: {compressed}")
        self.assertGreaterEqual(len(compressed.get("compressed") or []), 1)

        # R4：compress 轮不得归档/删除
        self.assertEqual(statuses.get("archived", 0), 0, f"compress 轮出现 archived 节点: {statuses}")
        self.assertEqual(int(after_compress["nodes_live"]), int(after_compress["nodes"]) - statuses.get("archived", 0))

        # R4：同一 round 的 prune 只出报告，不得归档
        same_round = fx.prune(dry_run=False)
        self.assertEqual(same_round.get("archived"), [], f"同轮 prune 不得归档: {same_round}")
        self.assertEqual(same_round.get("deleted"), [], f"同轮 prune 不得删除: {same_round}")

        # 隔一轮才允许归档
        round2 = fx.round_tick()["round"]
        self.assertGreater(round2, round1)
        next_round = fx.prune(dry_run=False)
        total_archived = len(next_round.get("archived") or []) + len(next_round.get("deleted") or [])
        self.assertGreater(total_archived, 0, f"隔轮 prune 未归档任何压缩节点: {next_round}")

        self.record("nodes_before", int(before["nodes"]))
        self.record("compress_result", {k: compressed.get(k) for k in ("compressed", "merged", "skipped", "round")})
        self.record("statuses_after_compress", statuses)
        self.record("same_round_prune", {"archived": same_round.get("archived"), "deleted": same_round.get("deleted")})
        self.record("next_round_prune", {"archived": len(next_round.get("archived") or []), "deleted": len(next_round.get("deleted") or [])})


# --------------------------------------------------------------------------- #
# C：改口逐日（11~16 / R6）
# --------------------------------------------------------------------------- #
class TestC_SupersedeDaily(AcceptanceBase):
    DAY1_TEXT = "我喜欢喝咖啡，尤其是美式咖啡。"
    DAY2_TEXT = "我现在不喜欢喝咖啡了，改成喝茶。"
    DAY3_TEXT = "今天下午想喝点什么，有什么推荐吗？"

    def test_C_supersede_daily(self) -> None:
        fx = self.spawn()
        day1 = fx.today()
        first = fx.ingest(self.DAY1_TEXT, turn_id="c-day1")
        self.assertFalse((first.get("dropped") or {}).get("dropped"))
        nodes_day1 = self.nodes(fx)
        self.assertTrue(nodes_day1, "Day1 未写入偏好节点")
        target_id = None
        old_description = ""
        for node in nodes_day1:
            if "喜欢" in str(node.get("description") or ""):
                target_id = node["id"]
                old_description = str(node.get("description") or "")
        self.assertIsNotNone(target_id, f"Day1 未找到偏好节点: {nodes_day1}")

        # Day2：改口
        fx.advance(86400)
        day2 = fx.today()
        self.assertNotEqual(day1, day2)
        second = fx.ingest(self.DAY2_TEXT, turn_id="c-day2")
        superseded = second.get("superseded") or []
        self.assertTrue(superseded, f"Day2 未触发改口 supersede: {second}")
        change = superseded[0]
        self.assertEqual(change.get("node_id"), target_id, f"改口目标错误: {change}")
        before = str((change.get("before") or {}).get("description") or "")
        after = str((change.get("after") or {}).get("description") or "")
        self.assertIn("喜欢", before)
        self.assertNotEqual(before, after, "改口后 description 未变化")

        # 13：valid_from=Day2、w=max 不清零、protect_until=now+7d；description 已是新说法
        fx.advance(1)  # 让 debug_state 时间与改口时间区分开
        state = fx.debug_state()
        node = next((n for n in self.nodes(fx) if n["id"] == target_id), None)
        self.assertIsNotNone(node, "改口后节点消失")
        props = node.get("properties") or {}
        self.assertEqual(node.get("description"), after, "节点 description 未同步为 after")
        self.assertEqual(str(props.get("valid_from")), day2, f"valid_from 必须=Day2: {props.get('valid_from')}")
        self.assertGreaterEqual(float(props.get("w") or 0.0), 1.0, "w 必须 max(w_old,w_new) 不清零")
        protect_until = props.get("protect_until")
        self.assertIsNotNone(protect_until, "改口后必须设置 protect_until")
        now = float(state["now"])
        self.assertGreater(float(protect_until), now + 6.0 * 86400, f"protect_until 必须约 now+7d: {protect_until} vs {now}")
        self.assertLess(float(protect_until), now + 8.0 * 86400)

        # 16：Journal before/after/date/confidence/source_turn_id，7 天内可回滚
        entries = fx.journal_list(scope="workspace").get("entries") or []
        supersede_entries = [entry for entry in entries if entry.get("kind") == "supersede"]
        self.assertTrue(supersede_entries, f"Journal 无 supersede 记录: {entries}")
        journal_entry = supersede_entries[-1]
        for key in ("before", "after", "ts"):
            self.assertIn(key, journal_entry, f"Journal 缺少字段 {key}: {journal_entry}")
        self.assertEqual(journal_entry.get("target_id"), target_id)

        # R6：向量同步 —— 旧说法检索不再返回旧值
        old_retrieval = fx.retrieve("喜欢喝咖啡")
        old_items = old_retrieval.get("items") or []
        self.assertTrue(old_items, "旧说法检索不应完全为空（应命中更新后节点）")
        for item in old_items:
            self.assertNotEqual(str(item.get("text") or ""), old_description, "R6 违规：旧说法仍检索到旧 description")
        hit = next((item for item in old_items if item.get("id") == target_id), None)
        self.assertIsNotNone(hit, "更新后的节点未被检索到")
        self.assertEqual(str(hit.get("text")), after)

        # Day3：问“点喝的”，应召回更新后的偏好（新说法）
        fx.advance(86400)
        third = fx.ingest(self.DAY3_TEXT, turn_id="c-day3")
        self.assertFalse((third.get("dropped") or {}).get("dropped"))
        recalled = fx.retrieve("想喝点什么")
        recalled_ids = [item.get("id") for item in recalled.get("items") or []]
        self.assertIn(target_id, recalled_ids, f"Day3 未召回更新后的偏好: {recalled_ids}")

        # 16：7 天内可回滚（回滚后恢复 before）
        change_id = int(journal_entry["id"])
        rolled = fx.journal_rollback(change_id)
        self.assertTrue(rolled.get("restored"), f"journal_rollback 失败: {rolled}")
        restored_node = next((n for n in self.nodes(fx) if n["id"] == target_id), None)
        self.assertIsNotNone(restored_node)
        self.assertEqual(str(restored_node.get("description")), before, "回滚后 description 未恢复 before")

        self.record("old_description", old_description)
        self.record("superseded", {"before": before, "after": after, "valid_from": props.get("valid_from"),
                                   "protect_days": round((float(protect_until) - now) / 86400.0, 3), "w": props.get("w")})
        self.record("journal_entry", {k: journal_entry.get(k) for k in ("id", "kind", "target_id", "ts")})
        self.record("day3_recall_ids", recalled_ids)
        self.record("rollback_restored", True)


# --------------------------------------------------------------------------- #
# D1：200 轮注入 token 恒定（17、22）
# --------------------------------------------------------------------------- #
class TestD1_TurnsConstantBudget(AcceptanceBase):
    TURNS = 200

    def test_D1_200_turns_constant_budget(self) -> None:
        fx = self.spawn()
        fx.debug_seed_entities(200, prefix=FIXED_MEMORY_PREFIX)
        measurements: list[dict] = []
        for turn in range(1, self.TURNS + 1):
            fx.ingest(f"第 {turn} 轮：继续用 pnpm 管理依赖，配置版本 {turn}，构建脚本保持稳定。", turn_id=f"d1-{turn}")
            fx.advance(60)
            if turn % 10 == 0:
                measured = fx.measure_injection(QUERY)
                measurements.append({"turn": turn, "total": measured["tokens"]["total"], "tokens": measured["tokens"]})
        totals = [item["total"] for item in measurements]
        self.assertTrue(totals)
        self.assertLessEqual(max(totals), injection_model.TOTAL_HARD, f"注入总量超过 3000: {max(totals)}")

        # 冷启动排除：只看第 50 轮之后的稳态（前 20~50 轮 day_context 尚未达稳态）
        steady = [item["total"] for item in measurements if item["turn"] >= 50]
        self.assertGreaterEqual(len(steady), 10)
        span_pct = pct_diff(min(steady), max(steady))
        self.assertLess(span_pct, 5.0, f"D1 稳态注入 token 波动 {span_pct:.2f}% >= 5%: {steady}")
        self.record("turn1_total", totals[0])
        self.record("steady_totals", steady)
        self.record("steady_span_pct", round(span_pct, 3))
        self.record("max_total", max(totals))
        self.record("final_tokens", measurements[-1]["tokens"])


# --------------------------------------------------------------------------- #
# D2：30 天 × 50 轮恒定 + 历史摘要单份（17、19、21 / R8）
# --------------------------------------------------------------------------- #
class TestD2_LongTermConstantBudget(AcceptanceBase):
    DAYS = 30
    TURNS_PER_DAY = 50

    def test_D2_30days_constant_budget_single_history(self) -> None:
        fx = self.spawn()
        fx.debug_seed_entities(200, prefix=FIXED_MEMORY_PREFIX)
        daily_totals: list[dict] = []
        for day in range(1, self.DAYS + 1):
            for turn in range(self.TURNS_PER_DAY):
                if turn == 0:
                    text = f"今天把 pnpm 依赖管理配置更新到第 {day} 版，构建脚本版本是 v{day}。"
                elif turn == 1:
                    text = f"服务 v{day} 的启动端口是 {9000 + day}。"
                else:
                    text = "嗯嗯"
                fx.ingest(text, turn_id=f"d2-{day}-{turn}")
                fx.advance(60)
            measured = fx.measure_injection(QUERY)
            workspace_stats = fx.stats()["scopes"]["workspace"]
            daily_totals.append(
                {
                    "day": day,
                    "total": measured["tokens"]["total"],
                    "tokens": measured["tokens"],
                    "history_tokens": int(workspace_stats.get("history_tokens") or 0),
                }
            )
            if day < self.DAYS:
                fx.advance(86400)

        totals = [item["total"] for item in daily_totals]
        self.assertLessEqual(max(totals), injection_model.TOTAL_HARD, f"D2 注入总量超过 3000: {max(totals)}")
        steady = [item["total"] for item in daily_totals if item["day"] >= 15]
        span_pct = pct_diff(min(steady), max(steady))
        self.assertLess(span_pct, 5.0, f"D2 稳态注入 token 波动 {span_pct:.2f}% >= 5%: {steady}")

        # 19：day_context 恒定预算（day<=512 / total<=1024）
        last = daily_totals[-1]["tokens"]
        self.assertLessEqual(int(last["day"]), injection_model.DAY_TEXT_MAX)
        day_total = int(last["day"]) + int(last["detail"]) + int(last["history"])
        self.assertLessEqual(day_total, injection_model.DAY_MAX, f"day_context 超 1024: {last}")
        self.assertLessEqual(int(last["history"]), injection_model.HISTORY_MAX)

        # 21/R8：历史摘要永远只有一份，且 ≤200 token
        days_dir = os.path.join(fx.workspace_home, "days")
        history_files = []
        for root, _dirs, files in os.walk(days_dir):
            for name in files:
                if name == "history_summary.json":
                    history_files.append(os.path.join(root, name))
        self.assertEqual(len(history_files), 1, f"R8 违规：history_summary.json 数量={len(history_files)}: {history_files}")
        self.assertLessEqual(int(daily_totals[-1]["history_tokens"]), injection_model.HISTORY_MAX)

        self.record("day1_total", daily_totals[0]["total"])
        self.record("daily_totals_tail", [{"day": item["day"], "total": item["total"]} for item in daily_totals[-5:]])
        self.record("steady_span_pct", round(span_pct, 3))
        self.record("max_total", max(totals))
        self.record("history_files", len(history_files))
        self.record("last_day_tokens", last)


# --------------------------------------------------------------------------- #
# D3：图谱 100 → 20000 节点注入量恒定（17、22）
# --------------------------------------------------------------------------- #
class TestD3_GraphScaleConstantBudget(AcceptanceBase):
    def test_D3_graph_scale_constant_budget(self) -> None:
        fx = self.spawn()
        fx.debug_seed_entities(100, prefix=FIXED_MEMORY_PREFIX)
        fx.ingest("我们用 pnpm 管理依赖，配置版本 1。", turn_id="d3-0")
        before = fx.measure_injection(QUERY)
        before_retrieve = fx.retrieve(QUERY)
        before_top = (before_retrieve.get("items") or [{}])[0]

        fx.debug_seed_entities(19900, prefix="bulk data")
        nodes = int(fx.stats()["scopes"]["workspace"]["nodes"])
        self.assertGreaterEqual(nodes, 20000, f"未造出 20000 节点: {nodes}")
        after = fx.measure_injection(QUERY)
        after_retrieve = fx.retrieve(QUERY)
        after_top = (after_retrieve.get("items") or [{}])[0]

        for label, measured in (("before", before), ("after", after)):
            total = int(measured["tokens"]["total"])
            self.assertLessEqual(total, injection_model.TOTAL_HARD, f"D3 {label} 注入总量超 3000: {total}")
        diff_pct = pct_diff(before["tokens"]["total"], after["tokens"]["total"])
        self.assertLess(diff_pct, 5.0, f"D3 图 100→20000 注入 token 差 {diff_pct:.2f}% >= 5%")

        # 规模不应稀释召回：固定记忆的 top1 相似度必须保持
        top1_before = float(before_retrieve.get("top1_sim") or 0.0)
        top1_after = float(after_retrieve.get("top1_sim") or 0.0)
        self.assertGreaterEqual(
            top1_after,
            top1_before * 0.9,
            f"D3 规模扩大后召回质量下降：top1_sim {top1_before:.3f} -> {top1_after:.3f}",
        )
        self.assertTrue(
            str(before_top.get("name") or "").startswith(FIXED_MEMORY_PREFIX),
            f"before top1 不是固定记忆: {before_top.get('name')}",
        )
        self.assertTrue(
            str(after_top.get("name") or "").startswith(FIXED_MEMORY_PREFIX),
            f"after top1 变成噪声节点: {after_top.get('name')} (top1_sim={top1_after:.3f})",
        )

        self.record("nodes", nodes)
        self.record("before_tokens", before["tokens"])
        self.record("after_tokens", after["tokens"])
        self.record("diff_pct", round(diff_pct, 3))
        self.record("top1_sim", {"before": top1_before, "after": top1_after})
        self.record("top1_name", {"before": before_top.get("name"), "after": after_top.get("name")})


# --------------------------------------------------------------------------- #
# D4：压缩后当天细节补召回 ≥90%（19、20）
# --------------------------------------------------------------------------- #
class TestD4_CompressedDetailRecall(AcceptanceBase):
    FACTS = 25
    QUERIES = 10
    FACT_TEMPLATE = "服务 alpha{i} 的监听端口是 {port}，负责人编号是 {owner}。"

    def test_D4_compressed_detail_recall(self) -> None:
        fx = self.spawn()
        for i in range(self.FACTS):
            fx.ingest(self.FACT_TEMPLATE.format(i=i, port=9000 + i, owner=100 + i), turn_id=f"d4-{i}")
            fx.advance(120)
        fx.advance(1800)  # 触发限流窗口外压缩
        fx.day_pack("服务 alpha3 的监听端口")  # 触发 maybe_compress
        daykeeper = fx.debug_state()["daykeepers"]["workspace"]
        compressed = bool(daykeeper.get("compressed"))
        summary_tokens = int(daykeeper.get("summary_tokens") or 0)
        self.assertTrue(compressed, f"超过 512 token 未触发全量压缩: {daykeeper}")
        self.assertGreater(summary_tokens, 0, "压缩后 summary_tokens 必须 >0")
        self.assertLessEqual(summary_tokens, 360, f"摘要超过 360 token: {summary_tokens}")

        hits = 0
        misses: list[dict] = []
        for i in range(self.QUERIES):
            query = self.FACT_TEMPLATE.format(i=i, port=9000 + i, owner=100 + i)
            packed = fx.day_pack(query)
            needle = str(9000 + i)
            found = needle in str(packed.get("detail_text") or "") or needle in str(packed.get("day_text") or "")
            if found:
                hits += 1
            else:
                misses.append({"i": i, "needle": needle, "detail": str(packed.get("detail_text") or "")[:80]})
            self.assertLessEqual(int(packed["tokens"]["day"]), injection_model.DAY_TEXT_MAX)
            day_total = sum(int(packed["tokens"].get(key) or 0) for key in ("day", "detail", "history"))
            self.assertLessEqual(day_total, injection_model.DAY_MAX)
        accuracy = hits / float(self.QUERIES)
        self.assertGreaterEqual(accuracy, 0.9, f"补召回准确率 {accuracy:.2f} < 0.9: {misses}")

        self.record("compressed", compressed)
        self.record("summary_tokens", summary_tokens)
        self.record("raw_tokens", daykeeper.get("raw_tokens"))
        self.record("recall_accuracy", round(accuracy, 3))
        self.record("misses", misses)


# --------------------------------------------------------------------------- #
# E1：规模 < high 时 90 天零删除且可召回/激活回升（24 / 硬指标 E1）
# --------------------------------------------------------------------------- #
class TestE1_BelowHigh90Days(AcceptanceBase):
    NODES = 1000
    DAYS = 90

    def test_E1_90days_below_high_zero_delete(self) -> None:
        fx = self.spawn()
        fx.debug_seed_entities(self.NODES, prefix="e1 项目记忆")
        initial = fx.stats()["scopes"]["workspace"]
        self.assertEqual(int(initial["nodes_live"]), self.NODES)
        self.assertLess(self.NODES, int(initial["watermarks"]["high"]), "E1 前提：节点数必须 < high")

        # 90 天（每天一次维护轮）→ 时间不是删除理由
        archived = 0
        deleted = 0
        for _day in range(self.DAYS):
            fx.advance(86400)
            fx.maintenance()
        final_prune = fx.prune(dry_run=False)
        archived += len(final_prune.get("archived") or [])
        deleted += len(final_prune.get("deleted") or [])
        after = fx.stats()["scopes"]["workspace"]
        self.assertEqual(archived, 0, f"E1 违规：<high 时 90 天出现归档 {archived}")
        self.assertEqual(deleted, 0, f"E1 违规：<high 时 90 天出现删除 {deleted}")
        self.assertEqual(int(after["nodes_live"]), self.NODES)
        self.assertEqual(int(after["nodes"]), self.NODES)

        # 权重衰减后仍可召回
        retrieved = fx.retrieve("e1 项目记忆")
        items = retrieved.get("items") or []
        self.assertTrue(items, "E1：90 天后不可召回")
        weights_before = [float(item.get("w") or 0.0) for item in items]
        self.assertTrue(all(weight < 0.01 for weight in weights_before), f"E1：90 天后权重应衰减到近 0: {weights_before[:3]}")
        injected = retrieved.get("injected_ids") or [item["id"] for item in items]

        # 激活后权重回升
        activated = fx.activate(injected, reason="injected")
        updated = {entry["id"]: float(entry["w"]) for entry in activated.get("updated") or []}
        self.assertTrue(updated, "E1：激活失败")
        for entity_id, weight_after in updated.items():
            self.assertGreaterEqual(weight_after, 0.5, f"E1：激活后权重未回升 {entity_id}={weight_after}")
        self.record("nodes", self.NODES)
        self.record("archived_after_90d", archived)
        self.record("deleted_after_90d", deleted)
        self.record("recall_items", len(items))
        self.record("weights_before", [round(w, 5) for w in weights_before[:3]])
        self.record("weights_after", dict(list(updated.items())[:3]))


# --------------------------------------------------------------------------- #
# E2：> high 开始剪，单调下降到 <= low（25）
# --------------------------------------------------------------------------- #
class TestE2_OverHighPruneToLow(AcceptanceBase):
    HIGH = 200
    LOW = 160

    def test_E2_over_high_prune_to_low(self) -> None:
        overrides = {"prune": {"watermarks": {"workspace": {"high": self.HIGH, "low": self.LOW, "hard": 1000}}}}
        fx = self.spawn(config_overrides=overrides)
        self.precompress(fx, self.HIGH + 1, prefix="e2 node")
        fx.advance(86400 * 15)
        counts = [self.live_nodes(fx)]
        self.assertGreater(counts[0], self.HIGH)

        rounds: list[dict] = []
        for _round in range(10):
            result = fx.maintenance()
            live = self.live_nodes(fx)
            counts.append(live)
            rounds.append(
                {
                    "round": result.get("round"),
                    "gate": {key: (result.get("gate") or {}).get(key) for key in ("action", "reason", "count", "phase")},
                    "live": live,
                }
            )
            if live <= self.LOW:
                break
        self.assertLessEqual(counts[-1], self.LOW, f"E2 未剪到 <=low={self.LOW}: {counts}")
        self.assertTrue(all(counts[i] >= counts[i + 1] for i in range(len(counts) - 1)), f"E2 节点数非单调下降: {counts}")
        prune_actions = [
            action
            for entry in rounds
            for action in ([] if not entry.get("gate") else [entry])
            if entry["gate"].get("action") in ("prune", "force")
        ]
        self.assertTrue(prune_actions, f"E2 未触发 prune 动作: {rounds}")
        self.assertGreater(int(fx.stats()["scopes"]["workspace"]["nodes"]), int(fx.stats()["scopes"]["workspace"]["nodes_live"]),
                           "E2 剪枝后应存在 archived 节点（真实归档，不是删除）")

        self.record("high", self.HIGH)
        self.record("low", self.LOW)
        self.record("counts", counts)
        self.record("rounds", [{"round": item["round"], "gate": item["gate"], "live": item["live"]} for item in rounds])


# --------------------------------------------------------------------------- #
# E3：双水位迟滞，low~high 波动 10 天剪枝触发 <=1（26）
# --------------------------------------------------------------------------- #
class TestE3_Hysteresis(AcceptanceBase):
    HIGH = 100
    LOW = 80

    def test_E3_band_fluctuation_triggers(self) -> None:
        overrides = {"prune": {"watermarks": {"workspace": {"high": self.HIGH, "low": self.LOW, "hard": 1000}}}}
        fx = self.spawn(config_overrides=overrides)
        self.precompress(fx, self.HIGH + 1, prefix="e3 node")
        fx.advance(86400 * 15)
        fx.round_tick()
        fx.compress()

        setup = []
        for _round in range(8):
            fx.round_tick()
            result = fx.prune(dry_run=False)
            live = self.live_nodes(fx)
            setup.append({"live": live, "archived": len(result.get("archived") or [])})
            if live <= self.LOW:
                break
        self.assertLessEqual(setup[-1]["live"], self.LOW, f"E3 前置：未剪到 <=low: {setup}")
        self.assertLessEqual(len(setup), 3, f"E3 前置：剪到 low 不应超过 3 轮（8%/轮）: {setup}")

        triggers = 0
        days: list[dict] = []
        for day in range(10):
            fx.debug_seed_entities(1, prefix=f"e3 日{day}甲")
            fx.debug_seed_entities(1, prefix=f"e3 日{day}乙")
            result = fx.maintenance()
            prune_actions = [action for action in (result.get("actions") or []) if action.get("action") == "prune"]
            if prune_actions and (prune_actions[0].get("result") or {}).get("archived"):
                triggers += 1
            live = self.live_nodes(fx)
            days.append(
                {
                    "day": day,
                    "live": live,
                    "gate": (result.get("gate") or {}).get("action"),
                    "reason": (result.get("gate") or {}).get("reason"),
                }
            )
            self.assertLessEqual(live, self.HIGH, f"E3：第 {day} 天意外超过 high")
        self.assertLessEqual(triggers, 1, f"E3 违规：low~high 波动 10 天剪枝触发 {triggers} 次 >1")

        self.record("high", self.HIGH)
        self.record("low", self.LOW)
        self.record("setup", setup)
        self.record("triggers", triggers)
        self.record("days", days)


# --------------------------------------------------------------------------- #
# E4：单轮处理量 <= 8%（26）
# --------------------------------------------------------------------------- #
class TestE4_MaxFractionPerRound(AcceptanceBase):
    NODES = 201

    def test_E4_single_round_max_fraction(self) -> None:
        overrides = {"prune": {"watermarks": {"workspace": {"high": 100, "low": 20, "hard": 1000}}}}
        fx = self.spawn(config_overrides=overrides)
        self.precompress(fx, self.NODES, prefix="e4 node")
        fx.advance(86400 * 15)
        fx.round_tick()
        fx.compress()
        fx.round_tick()
        before = self.live_nodes(fx)
        result = fx.prune(dry_run=False)
        report = result.get("report") or {}
        handled = len(result.get("archived") or []) + len(result.get("deleted") or [])
        expected_limit = ceil_fraction(before, 0.08)
        self.assertEqual(int(report.get("count_before") or 0), before, f"report.count_before 不一致: {report}")
        self.assertEqual(int(report.get("limit") or 0), expected_limit, f"8% 上限计算不一致: limit={report.get('limit')} expected={expected_limit}")
        self.assertLessEqual(handled, expected_limit, f"E4 违规：单轮处理 {handled} > 8%({expected_limit})")

        self.record("nodes_before", before)
        self.record("expected_limit", expected_limit)
        self.record("report_limit", report.get("limit"))
        self.record("handled", handled)
        self.record("archived", len(result.get("archived") or []))
        self.record("deleted", len(result.get("deleted") or []))


# --------------------------------------------------------------------------- #
# E5：pinned 90% 连续 3 轮剪不到 low → 停止 + 告警（26）
# --------------------------------------------------------------------------- #
class TestE5_PinnedStuckAlarm(AcceptanceBase):
    HIGH = 200
    LOW = 160
    NODES = 210
    PINNED = 189

    def test_E5_pinned_stuck_alarm(self) -> None:
        overrides = {"prune": {"watermarks": {"workspace": {"high": self.HIGH, "low": self.LOW, "hard": 1000}}}}
        fx = self.spawn(config_overrides=overrides)
        fx.debug_seed_entities(self.NODES, prefix="e5 node")
        fx.advance(86400 * 15)
        nodes = self.nodes(fx)
        self.assertGreaterEqual(len(nodes), self.NODES)
        for node in nodes[: self.PINNED]:
            fx.debug_entity_set(node["id"], {"pinned": True})
        pinned_count = sum(1 for node in self.nodes(fx) if (node.get("properties") or {}).get("pinned"))
        self.assertEqual(pinned_count, self.PINNED, f"pinned 造数失败: {pinned_count}")

        rounds: list[dict] = []
        stuck = False
        for _round in range(8):
            result = fx.maintenance()
            gate = result.get("gate") or {}
            rounds.append(
                {
                    "round": result.get("round"),
                    "action": gate.get("action"),
                    "reason": gate.get("reason"),
                    "stuck": bool(gate.get("stuck")),
                    "live": self.live_nodes(fx),
                }
            )
            if gate.get("stuck"):
                stuck = True
                break
        self.assertTrue(stuck, f"E5：连续 3 轮剪不到 low 未进入 stuck 停止态: {rounds}")
        self.assertGreater(rounds[-1]["live"], self.LOW, "E5 前提：应剪不到 low")
        alarms = fx.panel_state().get("alarms") or []
        stuck_alarms = [alarm for alarm in alarms if alarm.get("kind") == "prune_stuck"]
        self.assertTrue(stuck_alarms, f"E5：未写 prune_stuck 告警: {alarms}")
        # 不无限重试：stuck 后再维护一轮不应继续大量归档
        live_after_stuck = self.live_nodes(fx)
        fx.maintenance()
        self.assertEqual(self.live_nodes(fx), live_after_stuck, "E5：stuck 后仍继续剪枝（应停止重试）")

        self.record("pinned", pinned_count)
        self.record("rounds", rounds)
        self.record("stuck", True)
        self.record("alarms", [alarm.get("kind") for alarm in alarms])
        self.record("prune_stuck_alarm", stuck_alarms[0] if stuck_alarms else None)


# --------------------------------------------------------------------------- #
# E6：三档按 scope 独立计量（user 超上界不影响 workspace）（26）
# --------------------------------------------------------------------------- #
class TestE6_ScopeIsolation(AcceptanceBase):
    def test_E6_scope_isolation(self) -> None:
        overrides = {
            "prune": {
                "watermarks": {
                    # user 30 节点 > high=25，三轮 8% 可到 <=low=22；workspace 保持默认高位
                    "user": {"high": 25, "low": 22, "hard": 50},
                    "workspace": {"high": 20000, "low": 16000, "hard": 50000},
                }
            }
        }
        fx = self.spawn(config_overrides=overrides)
        fx.debug_seed_entities(10, prefix="e6 工作区记忆")
        fx.debug_seed_entities(30, prefix="e6 用户记忆", scope="user")
        user_nodes = self.nodes(fx, scope="user")
        self.assertGreaterEqual(len(user_nodes), 30)
        for node in user_nodes:
            fx.debug_entity_set(node["id"], {"w": 0.1, "status": "compressed"})
        fx.advance(86400 * 15)

        workspace_before = fx.stats()["scopes"]["workspace"]
        user_counts = [int(fx.stats()["scopes"]["user"]["nodes_live"])]
        for _round in range(10):
            fx.maintenance(scope="user")
            user_counts.append(int(fx.stats()["scopes"]["user"]["nodes_live"]))
            if user_counts[-1] <= 22:
                break
        workspace_after = fx.stats()["scopes"]["workspace"]

        self.assertLess(user_counts[-1], user_counts[0], f"E6：user 超过 high 未剪枝: {user_counts}")
        self.assertLessEqual(user_counts[-1], 22, f"E6：user 未剪到 <=low: {user_counts}")
        self.assertEqual(int(workspace_after["nodes"]), int(workspace_before["nodes"]), "E6：workspace 节点数被误改")
        self.assertEqual(int(workspace_after["nodes_live"]), int(workspace_before["nodes_live"]))
        workspace_statuses = self.statuses(fx, scope="workspace")
        self.assertEqual(workspace_statuses.get("archived", 0), 0, f"E6：workspace 出现归档 {workspace_statuses}")
        self.assertEqual(workspace_after.get("phase"), "idle", f"E6：workspace 维护状态被触发 {workspace_after.get('phase')}")

        self.record("user_counts", user_counts)
        self.record("workspace_before", {k: workspace_before[k] for k in ("nodes", "nodes_live", "phase")})
        self.record("workspace_after", {k: workspace_after[k] for k in ("nodes", "nodes_live", "phase")})
        self.record("workspace_statuses", workspace_statuses)


# --------------------------------------------------------------------------- #
# F1/F2/F3/F4：噪声四道闸门（8b、8c、8d、4）
# --------------------------------------------------------------------------- #
class TestF1_PureResponses(AcceptanceBase):
    # 「你决定」含“决定”（CONTRACT §2.3 决策/意愿放行信号 → 允许不丢），但要求不进图
    PURE_RESPONSES = ("好的", "嗯嗯", "先这样吧", "稍等我看下")
    DELEGATION_RESPONSE = "你决定"

    def test_F1_pure_responses_dropped(self) -> None:
        fx = self.spawn()
        rows = []
        for text in self.PURE_RESPONSES:
            result = fx.ingest(text)
            dropped = result.get("dropped") or {}
            rows.append({"text": text, "dropped": bool(dropped.get("dropped")), "reason": dropped.get("reason")})
            self.assertTrue(dropped.get("dropped"), f"F1/8b 违规：纯应答未丢弃: {text} -> {result}")
            self.assertEqual(result.get("node_ids") or [], [], f"F1/8b 违规：纯应答进了图: {text}")
        delegation = fx.ingest(self.DELEGATION_RESPONSE)
        rows.append(
            {
                "text": self.DELEGATION_RESPONSE,
                "dropped": bool((delegation.get("dropped") or {}).get("dropped")),
                "reason": (delegation.get("dropped") or {}).get("reason"),
                "note": "含“决定”→CONTRACT §2.3 放行信号优先；只断言不进图",
            }
        )
        self.assertEqual(delegation.get("node_ids") or [], [], "F1/8b 违规：「你决定」进了图（不进图硬要求）")
        self.assertEqual(int(fx.stats()["scopes"]["workspace"]["nodes"]), 0, "F1/8b 违规：纯应答导致图谱非空")
        self.record("rows", rows)


class TestF2_SignalPass(AcceptanceBase):
    SIGNAL_TEXTS = (
        "明天下午三点开会",
        "价格改成 80",
        "改用 Redis",
        "决定用 pnpm",
        "这个接口谁负责？",
    )

    def test_F2_signal_pass_no_false_kill(self) -> None:
        fx = self.spawn()
        rows = []
        for text in self.SIGNAL_TEXTS:
            result = fx.ingest(text)
            dropped = result.get("dropped") or {}
            rows.append(
                {
                    "text": text,
                    "dropped": bool(dropped.get("dropped")),
                    "reason": dropped.get("reason"),
                    "nodes": len(result.get("node_ids") or []),
                }
            )
            self.assertFalse(dropped.get("dropped"), f"F2/8c 违规：放行信号被误杀: {text} -> {result}")
            self.assertTrue(result.get("accepted"), f"F2：未 accepted: {result}")
        killed = [row for row in rows if row["dropped"]]
        self.assertEqual(len(killed), 0, f"F2/8c 误杀数量应为 0: {killed}")
        self.record("rows", rows)


class TestF3_ShortSentences(AcceptanceBase):
    def test_F3_short_sentences_no_false_kill(self) -> None:
        fx = self.spawn()
        sentences = generate_short_sentences(100)
        self.assertEqual(len(sentences), 100)
        killed = []
        for sentence in sentences:
            result = fx.ingest(sentence)
            if (result.get("dropped") or {}).get("dropped"):
                killed.append({"text": sentence, "result": result.get("dropped")})
        self.assertEqual(len(killed), 0, f"F3/8d 违规：{len(killed)}/100 条短句被误杀（应为 0）: {killed[:5]}")
        self.record("sentences", len(sentences))
        self.record("killed", len(killed))
        self.record("killed_samples", killed[:5])


class TestF4_CredentialAlarm(AcceptanceBase):
    CREDENTIAL_TEXTS = (
        "我的 api key 是 sk-abcdefgh12345678，帮我保存",
        "Authorization: Bearer abcdef123456.token",
        "password=SuperSecret123 请记住",
    )

    def test_F4_credential_dropped_and_alarm(self) -> None:
        fx = self.spawn()
        rows = []
        for text in self.CREDENTIAL_TEXTS:
            result = fx.ingest(text)
            dropped = result.get("dropped") or {}
            rows.append({"text": text, "dropped": bool(dropped.get("dropped")), "reason": dropped.get("reason")})
            self.assertTrue(dropped.get("dropped"), f"F4/4 违规：凭据未丢弃: {text} -> {result}")
            self.assertEqual(result.get("node_ids") or [], [], f"F4/4 违规：凭据进了图: {text}")
        # 宿主告警通知（sidecar → host warning）
        warnings = [note for note in fx.client.notifications if note.get("method") == "warning"]
        self.assertTrue(warnings, f"F4/4 违规：凭据丢弃未发 warning 通知: {fx.client.notifications}")
        self.record("rows", rows)
        self.record("warning_notifications", len(warnings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
