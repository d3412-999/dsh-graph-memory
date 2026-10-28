# -*- coding: utf-8 -*-
"""改口（supersede）单测（CONTRACT §2.4 / §4 / R6）。

C 用例：Day1「用户喜欢用 pnpm 管理依赖」→ Day3「用户改用 npm 管理依赖」，
严格六步改写；断言 valid_from=Day3、updated_at=Day3、w=max(旧,新) 不清零、
protect_until=Day3+7d、向量已同步（旧说法检索不到旧值）、Journal before→after。
另覆盖：LLM 裁决解析/降级、否定词、should_supersede 阈值、矛盾扫描只报告不改图。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIDECAR = os.path.join(ROOT, "sidecar")
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)

from base import (  # noqa: E402
    SECONDS_PER_WEEK,
    Config,
    GraphMemoryError,
    embed_sparse,
    sparse_cosine,
    today_str,
)
from graph import GraphStore  # noqa: E402
from llm_bridge import LlmBridge  # noqa: E402
import supersede  # noqa: E402

DAY = 86400.0
DAY1 = 1730000000.0
DAY3 = DAY1 + 3 * DAY


def _make_home(prefix: str) -> str:
    """沙箱下不能用 tempfile.mkdtemp（子进程在新建目录内建子目录会被拒绝）。"""
    base = os.path.join(ROOT, ".tmp")
    os.makedirs(base, exist_ok=True)
    home = os.path.join(base, f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    os.makedirs(home, exist_ok=True)
    return home


class FakeJournal:
    def __init__(self, timeline: list | None = None) -> None:
        self.changes: list[dict] = []
        self.alarms: list[tuple] = []
        self.timeline = timeline

    def log_change(
        self,
        scope: str,
        target_id: str,
        kind: str,
        before: dict | None = None,
        after: dict | None = None,
        confidence: float = 0.0,
        source_turn_id: str = "",
    ) -> int:
        self.changes.append(
            {
                "scope": scope,
                "target_id": target_id,
                "kind": kind,
                "before": before,
                "after": after,
                "confidence": confidence,
                "source_turn_id": source_turn_id,
            }
        )
        if self.timeline is not None:
            self.timeline.append(("journal_change", kind))
        return len(self.changes)

    def log_alarm(self, scope: str, kind: str, detail: dict) -> None:
        self.alarms.append((scope, kind, detail))


class RecordingGraph(GraphStore):
    """记录 edit_entity / update_properties 调用顺序，验证严格六步。"""

    def __init__(self, *args, timeline: list | None = None, **kwargs) -> None:
        self.timeline = timeline
        super().__init__(*args, **kwargs)

    def edit_entity(self, entity_id, description=None, properties=None):  # noqa: ANN001
        if self.timeline is not None:
            self.timeline.append(("edit_entity", description is not None))
        return super().edit_entity(entity_id, description, properties)

    def update_properties(self, entity_id, patch):  # noqa: ANN001
        if self.timeline is not None:
            self.timeline.append(("update_properties", tuple(sorted(patch))))
        return super().update_properties(entity_id, patch)


class FakeService:
    def __init__(self, graph, journal, now_value, scope: str = "user") -> None:
        self.graphs = {scope: graph}
        self.journals = {scope: journal}
        self.scope = scope
        self._now = now_value if callable(now_value) else (lambda: now_value)
        self.config = Config({})

    def now(self) -> float:
        return float(self._now())


class _AsyncLlm:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, prompt, *, system=None, max_tokens=1024, purpose=""):
        self.calls.append({"prompt": prompt, "purpose": purpose, "system": system})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class GraphTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.home = _make_home("supersede-test")
        self.now = DAY1
        self.graph = GraphStore(self.home, "user", clock=lambda: self.now)
        self.journal = FakeJournal()
        self.service = FakeService(self.graph, self.journal, lambda: self.now)
        self.addCleanup(shutil.rmtree, self.home, True)

    def make_node(self, name: str, description: str, **properties) -> str:
        props = {"last_activated_at": self.now, **properties}
        node_id = self.graph.upsert_entity(name, "Preference", description, "user", properties=props)
        self.graph.save()
        return node_id


 # --------------------------------------------------------------------------- #
# 否定词 / 阈值
# --------------------------------------------------------------------------- #
class DetectNegationTests(unittest.TestCase):
    def test_negation_phrases(self) -> None:
        for text in ["现在不喜欢了", "不再用 yarn", "我改成了 npm", "取消这个方案", "换成 SQLite"]:
            with self.subTest(text=text):
                self.assertTrue(supersede.detect_negation(text))

    def test_neutral_phrases(self) -> None:
        for text in ["我喜欢咖啡", "继续用 pnpm 管理依赖", "项目使用 SQLite 存储"]:
            with self.subTest(text=text):
                self.assertFalse(supersede.detect_negation(text), msg=text)

    def test_confidence_threshold_helper(self) -> None:
        self.assertEqual(supersede.confidence_threshold_for("现在不喜欢了"), 0.6)
        self.assertEqual(supersede.confidence_threshold_for("我喜欢咖啡"), 0.7)

    def test_should_supersede_thresholds(self) -> None:
        verdict = {"is_same_attribute": True, "is_contradiction": True, "confidence": 0.65}
        self.assertTrue(supersede.should_supersede(verdict, "现在不喜欢了"))
        self.assertFalse(supersede.should_supersede(verdict, "我喜欢咖啡"))
        self.assertTrue(
            supersede.should_supersede(
                {"is_same_attribute": True, "is_contradiction": True, "confidence": 0.8},
                "我喜欢咖啡",
            )
        )
        self.assertFalse(
            supersede.should_supersede(
                {"is_same_attribute": False, "is_contradiction": True, "confidence": 0.95},
                "现在不喜欢了",
            )
        )


class ParseVerdictTests(unittest.TestCase):
    def test_fenced_json(self) -> None:
        raw = '```json\n{"is_same_attribute": true, "is_contradiction": true, "confidence": 0.82}\n```'
        verdict = supersede.parse_verdict(raw)
        self.assertEqual(verdict, {"is_same_attribute": True, "is_contradiction": True, "confidence": 0.82})

    def test_text_fallback(self) -> None:
        raw = "is_same_attribute: true, is_contradiction: true, confidence: 80%"
        verdict = supersede.parse_verdict(raw)
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])
        self.assertAlmostEqual(verdict["confidence"], 0.8)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(supersede.parse_verdict("I cannot answer"))
        self.assertIsNone(supersede.parse_verdict(None))

    def test_percent_and_out_of_range_clamped(self) -> None:
        verdict = supersede.parse_verdict({"is_same_attribute": "yes", "is_contradiction": "no", "confidence": 95})
        self.assertTrue(verdict["is_same_attribute"])
        self.assertFalse(verdict["is_contradiction"])
        self.assertAlmostEqual(verdict["confidence"], 0.95)

    def test_judge_prompt_labels(self) -> None:
        prompt = supersede.build_judge_prompt("用户喜欢咖啡", "现在不喜欢了", "上一轮上下文")
        self.assertIn("旧说法：用户喜欢咖啡", prompt)
        self.assertIn("新说法：现在不喜欢了", prompt)
        self.assertIn("is_same_attribute", prompt)


class JudgeSupersedeTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_valid_verdict(self) -> None:
        llm = _AsyncLlm('{"is_same_attribute": true, "is_contradiction": true, "confidence": 0.9}')
        verdict = await supersede.judge_supersede("用户喜欢咖啡", "现在不喜欢了", "", llm)
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])
        self.assertAlmostEqual(verdict["confidence"], 0.9)
        self.assertEqual(llm.calls[0]["purpose"], "judge")

    async def test_llm_invalid_falls_back_offline(self) -> None:
        llm = _AsyncLlm("not json")
        verdict = await supersede.judge_supersede("用户喜欢咖啡", "现在不喜欢了", "", llm)
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])
        self.assertGreaterEqual(verdict["confidence"], 0.6)

    async def test_llm_exception_falls_back_offline(self) -> None:
        llm = _AsyncLlm(RuntimeError("host llm down"))
        verdict = await supersede.judge_supersede("用户喜欢咖啡", "现在不喜欢了", "", llm)
        self.assertTrue(verdict["is_contradiction"])
        self.assertGreaterEqual(verdict["confidence"], 0.6)

    async def test_offline_bridge_judge_path(self) -> None:
        bridge = LlmBridge(Config({"llm": {"mode": "offline", "timeoutMs": 1000}}))
        verdict = await supersede.judge_supersede("用户喜欢咖啡", "现在不喜欢了", "", bridge)
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])
        self.assertGreaterEqual(verdict["confidence"], 0.6)

    async def test_empty_new_value(self) -> None:
        verdict = await supersede.judge_supersede("用户喜欢咖啡", "", "", None)
        self.assertFalse(verdict["is_contradiction"])


class ApplySupersedeThreeDayTests(GraphTestBase):
    NEW_VALUE = "用户改用 npm 管理依赖"

    async def _prepare_day1(self) -> tuple[str, str, dict, float]:
        node_id = self.make_node(
            "包管理器",
            "用户喜欢用 pnpm 管理依赖",
            w=2.5,
        )
        self.graph.search_entities("pnpm 管理依赖", "user", top_k=5)  # 触发向量索引构建
        old_desc = self.graph.get_entity(node_id)["description"]
        old_vec = dict(self.graph.entity_vectors[node_id])
        return node_id, old_desc, old_vec, 2.5

    async def test_three_day_supersede_full_contract(self) -> None:
        node_id, old_desc, old_vec, old_w = await self._prepare_day1()
        old_embed_version = int(self.graph.get_entity(node_id)["properties"]["embed_version"])
        node = self.graph.get_entity(node_id)

        self.now = DAY3
        result = await supersede.apply_supersede(
            self.service,
            node,
            self.NEW_VALUE,
            confidence=0.8,
            source_turn_id="turn_day3",
            scope="user",
        )

        # 返回结构
        self.assertEqual(result["node_id"], node_id)
        self.assertIn("before", result)
        self.assertIn("after", result)
        self.assertEqual(result["before"]["description"], old_desc)
        self.assertEqual(result["after"]["description"], self.NEW_VALUE)

        # 图状态：六步结果
        after = self.graph.get_entity(node_id)
        props = after["properties"]
        self.assertEqual(after["description"], self.NEW_VALUE)
        self.assertEqual(props["valid_from"], today_str(DAY3))
        self.assertAlmostEqual(float(props["updated_at"]), DAY3, places=3)
        self.assertAlmostEqual(float(props["protect_until"]), DAY3 + SECONDS_PER_WEEK, places=3)
        self.assertAlmostEqual(float(props["w"]), max(old_w, 0.0), places=6)
        self.assertGreater(float(props["w"]), 0.0, "改口后 w 绝不清零")
        self.assertEqual(props["status"], "active")
        self.assertGreater(int(props["embed_version"]), old_embed_version)

        # Journal before → after
        self.assertEqual(len(self.journal.changes), 1)
        change = self.journal.changes[0]
        self.assertEqual(change["scope"], "user")
        self.assertEqual(change["target_id"], node_id)
        self.assertEqual(change["kind"], "supersede")
        self.assertEqual(change["before"]["description"], old_desc)
        self.assertAlmostEqual(float(change["before"]["properties"]["w"]), old_w, places=6)
        self.assertEqual(change["after"]["description"], self.NEW_VALUE)
        self.assertAlmostEqual(float(change["confidence"]), 0.8)
        self.assertEqual(change["source_turn_id"], "turn_day3")

        # R6：向量已同步，旧说法不再命中旧值
        new_vec = self.graph.entity_vectors[node_id]
        self.assertNotEqual(new_vec, old_vec)
        old_query = embed_sparse("pnpm 管理依赖")
        self.assertLess(
            sparse_cosine(old_query, new_vec),
            sparse_cosine(old_query, old_vec),
            "旧说法与新向量的相似度必须下降",
        )
        for other_id, _sim in self.graph.search_entities("pnpm 管理依赖", "user", top_k=5):
            other = self.graph.get_entity(other_id)
            self.assertNotIn("pnpm", other["description"], "旧说法检索不得命中旧值")
        new_hits = [eid for eid, _ in self.graph.search_entities("npm 管理依赖", "user", top_k=5)]
        self.assertIn(node_id, new_hits, "新说法必须能检索到该节点")

    async def test_low_weight_not_cleared(self) -> None:
        node_id = self.make_node("端口", "默认端口 3000", w=0.4, last_activated_at=DAY1 - 30 * DAY)
        self.now = DAY3
        await supersede.apply_supersede(
            self.service,
            self.graph.get_entity(node_id),
            "默认端口 8080",
            confidence=1.0,
            source_turn_id="turn_x",
            scope="user",
        )
        props = self.graph.get_entity(node_id)["properties"]
        self.assertAlmostEqual(float(props["w"]), 0.4, places=6)
        self.assertGreater(float(props["w"]), 0.0)

    async def test_w_takes_max_with_decayed_value(self) -> None:
        node_id = self.make_node("主题", "用户喜欢深色主题", w=3.0, last_activated_at=DAY1)
        node = self.graph.get_entity(node_id)
        self.now = DAY3
        await supersede.apply_supersede(
            self.service,
            node,
            "用户喜欢浅色主题",
            confidence=0.9,
            source_turn_id="turn_y",
            scope="user",
        )
        props = self.graph.get_entity(node_id)["properties"]
        self.assertAlmostEqual(float(props["w"]), 3.0, places=6)

    async def test_empty_new_value_rejected(self) -> None:
        node_id = self.make_node("x", "用户喜欢咖啡")
        with self.assertRaises(GraphMemoryError):
            await supersede.apply_supersede(
                self.service,
                self.graph.get_entity(node_id),
                "   ",
                confidence=1.0,
                source_turn_id="t",
                scope="user",
            )

    async def test_journal_change_signature_fallback_dict(self) -> None:
        """兼容 journal.log_change(dict) 单参实现（极窄兜底）。"""

        class DictJournal:
            def __init__(self) -> None:
                self.entries: list[dict] = []

            def log_change(self, entry: dict) -> None:
                self.entries.append(entry)

        journal = DictJournal()
        service = FakeService(self.graph, journal, lambda: self.now)
        node_id = self.make_node("包管理器", "用户喜欢用 pnpm")
        await supersede.apply_supersede(
            service,
            self.graph.get_entity(node_id),
            "用户改用 npm",
            confidence=0.8,
            source_turn_id="turn_z",
            scope="user",
        )
        self.assertEqual(len(journal.entries), 1)
        self.assertEqual(journal.entries[0]["kind"], "supersede")


class StrictStepOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_step_strict_order(self) -> None:
        home = _make_home("supersede-order")
        self.addCleanup(shutil.rmtree, home, True)
        timeline: list[tuple] = []
        graph = RecordingGraph(home, "user", clock=lambda: DAY1, timeline=timeline)
        journal = FakeJournal(timeline=timeline)
        service = FakeService(graph, journal, lambda: DAY1)
        node_id = graph.upsert_entity("包管理器", "Preference", "用户喜欢用 pnpm", "user", properties={"w": 2.0})
        graph.save()
        timeline.clear()

        await supersede.apply_supersede(
            service,
            graph.get_entity(node_id),
            "用户改用 npm",
            confidence=0.8,
            source_turn_id="turn_order",
            scope="user",
        )

        expected = [
            ("edit_entity", True),  # 1) description := 新说法
            ("update_properties", ("updated_at", "valid_from")),  # 2) 日期语义
            ("update_properties", ("w",)),  # 3) w = max(旧,新)
            ("edit_entity", False),  # 4) 向量同步（显式 reindex）
            ("journal_change", "supersede"),  # 5) Journal before→after
            ("update_properties", ("protect_until",)),  # 6) now + 7d
        ]
        self.assertEqual(timeline, expected)

    async def test_missing_journal_fails_without_mutation(self) -> None:
        home = _make_home("supersede-nojournal")
        self.addCleanup(shutil.rmtree, home, True)
        graph = GraphStore(home, "user", clock=lambda: DAY1)
        node_id = graph.upsert_entity("包管理器", "Preference", "用户喜欢用 pnpm", "user")
        service = FakeService(graph, FakeJournal(), lambda: DAY1)
        service.journals = {}
        with self.assertRaises(GraphMemoryError):
            await supersede.apply_supersede(
                service,
                graph.get_entity(node_id),
                "用户改用 npm",
                confidence=0.8,
                source_turn_id="turn_q",
                scope="user",
            )
        self.assertEqual(graph.get_entity(node_id)["description"], "用户喜欢用 pnpm")

    async def test_clock_attribute_service_fallback(self) -> None:
        """service 只有 clock 可调用、没有 now() 时也能工作（防御性兼容）。"""

        class ClockOnlyService:
            def __init__(self, graph, journal, now_value) -> None:
                self.graph = graph
                self.journal = journal
                self.clock = lambda: now_value
                self.config = Config({})

        home = _make_home("supersede-clockonly")
        self.addCleanup(shutil.rmtree, home, True)
        graph = GraphStore(home, "user", clock=lambda: DAY1)
        node_id = graph.upsert_entity("包管理器", "Preference", "用户喜欢用 pnpm", "user")
        service = ClockOnlyService(graph, FakeJournal(), DAY1)
        result = await supersede.apply_supersede(
            service,
            graph.get_entity(node_id),
            "用户改用 npm",
            confidence=0.8,
            source_turn_id="turn_c",
            scope="user",
        )
        self.assertEqual(result["node_id"], node_id)


class SweepConflictsTests(GraphTestBase):
    async def test_detects_conflicting_pair_and_only_reports(self) -> None:
        a = self.graph.upsert_entity("咖啡", "Preference", "用户喜欢喝咖啡，每天两杯", "user")
        b = self.graph.upsert_entity("咖啡2", "Preference", "用户不喜欢喝咖啡，从来不喝", "user")
        self.graph.save()
        report = await supersede.sweep_conflicts(self.service, "user", days=30)
        self.assertEqual(report["scope"], "user")
        self.assertGreaterEqual(report["checked"], 1)
        self.assertGreaterEqual(report["conflicts"], 1)
        self.assertTrue(report["pairs"])
        pair = report["pairs"][0]
        for key in ("a", "b", "sim", "verdict"):
            self.assertIn(key, pair)
        self.assertIn({pair["a"], pair["b"]}, [{a, b}])
        self.assertTrue(pair["verdict"]["is_contradiction"])
        # 只报告，不改图
        self.assertEqual(self.graph.get_entity(a)["description"], "用户喜欢喝咖啡，每天两杯")
        self.assertEqual(self.graph.get_entity(b)["description"], "用户不喜欢喝咖啡，从来不喝")

    async def test_unrelated_nodes_no_conflict(self) -> None:
        self.graph.upsert_entity("缓存", "Concept", "缓存采用 LRU 策略", "user")
        self.graph.upsert_entity("日志", "Concept", "日志按大小轮转归档", "user")
        self.graph.save()
        report = await supersede.sweep_conflicts(self.service, "user", days=30)
        self.assertEqual(report["conflicts"], 0)
        self.assertEqual(report["pairs"], [])

    async def test_empty_graph(self) -> None:
        report = await supersede.sweep_conflicts(self.service, "user", days=30)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["conflicts"], 0)
        self.assertEqual(report["pairs"], [])


class RewriteDescriptionTests(unittest.IsolatedAsyncioTestCase):
    """改口改写：offline 降级不得把提示词原文写成新描述（提示词泄漏回归）。"""

    async def test_offline_rewrite_returns_empty_instead_of_prompt(self) -> None:
        bridge = LlmBridge(Config({"llm": {"mode": "offline", "timeoutMs": 1000}}))
        prompt = (
            "你是记忆改写器。旧记忆描述与用户最新一句话如下。请输出一句不超过 60 字的中文新描述。\n"
            "<<<OLD>>>用户使用pnpm\n<<<NEW>>>我们不用 pnpm 了，包管理器换成 yarn。"
        )
        out = await bridge.complete(prompt, max_tokens=120, purpose="rewrite")
        self.assertEqual(out, "", "offline rewrite 必须返回空串，交由 service 规则兜底")

    async def test_prompt_echo_is_rejected_and_replacement_rewritten(self) -> None:
        from service import MemoryService  # noqa: PLC0415

        class EchoLlm:
            async def complete(self, prompt: str, **_kwargs: Any) -> str:
                return prompt  # 模拟模型原样回显提示词

        service = MemoryService.__new__(MemoryService)
        service.llm = EchoLlm()
        node = {"name": "包管理器", "description": "用户使用pnpm"}
        text = await service._compose_rewritten_description(node, "我们不用 pnpm 了，包管理器换成 yarn。")
        self.assertEqual(text, "包管理器已改用yarn")
        self.assertNotIn("记忆改写器", text)
        self.assertNotIn("<<<OLD>>>", text)

    async def test_negation_rewrite_keeps_core_object(self) -> None:
        from service import MemoryService  # noqa: PLC0415

        class EmptyLlm:
            async def complete(self, prompt: str, **_kwargs: Any) -> str:
                return ""

        service = MemoryService.__new__(MemoryService)
        service.llm = EmptyLlm()
        node = {"name": "深色主题", "description": "用户喜欢深色主题"}
        text = await service._compose_rewritten_description(node, "我现在不喜欢深色主题了，改用浅色。")
        self.assertEqual(text, "用户不喜欢深色主题")


if __name__ == "__main__":
    unittest.main()
