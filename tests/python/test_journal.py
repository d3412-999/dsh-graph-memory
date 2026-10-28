"""journal.py 单测：CONTRACT §2.7 表结构/回滚约束/死信/注入统计/R6 向量同步。

关键断言：
- changes.before/after 规范为 {description, properties}；
- rollback 仅 7 天内、未回滚、kind 合法；恢复 description+properties 并调用 graph.edit_entity
  （R6 向量同步），成功后写回滚记录；
- 与真实 GraphStore 集成：回滚后 description 恢复且向量索引已重建（旧说法可再次命中）。
"""
from __future__ import annotations

import shutil
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import base  # noqa: E402
from journal import SQLiteJournal  # noqa: E402

# graph.py 属于 Lead 的交付；若尚未可用则跳过集成用例（不掩盖失败：Lead 会收到报告）。
try:  # noqa: E402
    from graph import GraphStore
except Exception as _graph_exc:  # noqa: BLE001
    GraphStore = None  # type: ignore[assignment]
    _GRAPH_IMPORT_ERROR = repr(_graph_exc)
else:
    _GRAPH_IMPORT_ERROR = ""

DAY = 86400.0
T0 = 1_700_000_000.0

# 测试夹具目录：系统 temp 在 sandbox 下不可写，改用仓库 .tmp/（契约 §6 允许，已 gitignore）
_TMP_ROOT = os.path.join(ROOT, ".tmp", "tests-py")


def make_temp_dir(tag: str) -> str:
    """测试夹具目录：仓库 .tmp/ 下的唯一目录（不用 tempfile.mkdtemp —— sandbox 下其子项不可写）。"""
    os.makedirs(_TMP_ROOT, exist_ok=True)
    path = os.path.join(_TMP_ROOT, f"{tag}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(path, exist_ok=False)
    return path


class FakeGraph:
    """记录 edit_entity 调用的最小 GraphStore 替身。"""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None

    def edit_entity(self, entity_id, description=None, properties=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"id": entity_id, "description": description, "properties": properties})
        node = self.nodes.setdefault(
            entity_id,
            {
                "id": entity_id,
                "name": "fake",
                "type": "Concept",
                "description": "",
                "scope": "user",
                "properties": {},
            },
        )
        if description is not None:
            node["description"] = description
        if properties:
            node["properties"].update(properties)
        return dict(node)


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = make_temp_dir("gm-journal")
        self.clock = [T0]
        self.journal = SQLiteJournal(
            os.path.join(self.tmpdir, "journal.db"),
            clock=lambda: self.clock[0],
        )
        self.graph = FakeGraph()

    def tearDown(self) -> None:
        self.journal.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds


class ChangeTests(JournalTestCase):
    def test_log_change_and_list_entries(self) -> None:
        change_id = self.journal.log_change(
            "user",
            "ent_aaa",
            "supersede",
            "旧说法",
            "新说法",
            confidence=0.82,
            source_turn_id="turn_1",
        )
        self.assertIsInstance(change_id, int)
        entries = self.journal.list_entries("user")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], change_id)
        self.assertEqual(entry["scope"], "user")
        self.assertEqual(entry["target_id"], "ent_aaa")
        self.assertEqual(entry["kind"], "supersede")
        self.assertEqual(entry["before"], {"description": "旧说法", "properties": {}})
        self.assertEqual(entry["after"], {"description": "新说法", "properties": {}})
        self.assertAlmostEqual(entry["confidence"], 0.82)
        self.assertEqual(entry["source_turn_id"], "turn_1")
        self.assertFalse(entry["rolled_back"])

    def test_list_entries_filters_and_orders(self) -> None:
        self.journal.log_change("user", "ent_a", "update", "a1", "a2")
        self.journal.log_change("user", "ent_b", "update", "b1", "b2")
        self.journal.log_change("workspace", "ent_a", "update", "w1", "w2")
        self.assertEqual(len(self.journal.list_entries("user")), 2)
        self.assertEqual(len(self.journal.list_entries("workspace")), 1)
        only_a = self.journal.list_entries("user", target_id="ent_a")
        self.assertEqual(len(only_a), 1)
        self.assertEqual(only_a[0]["target_id"], "ent_a")
        # 最新在前
        self.assertEqual(self.journal.list_entries("user")[0]["target_id"], "ent_b")
        self.assertEqual(len(self.journal.list_entries("user", limit=1)), 1)

    def test_properties_only_snapshot_normalized(self) -> None:
        self.journal.log_change("user", "ent_a", "weight", {"w": 1.0, "pinned": False}, {"w": 2.0})
        entry = self.journal.list_entries("user")[0]
        self.assertIsNone(entry["before"]["description"])
        self.assertEqual(entry["before"]["properties"], {"w": 1.0, "pinned": False})
        self.assertEqual(entry["after"]["properties"], {"w": 2.0})

    def test_get_change(self) -> None:
        change_id = self.journal.log_change("user", "ent_a", "pin", "x", "y")
        entry = self.journal.get_change(change_id)
        assert entry is not None
        self.assertEqual(entry["id"], change_id)
        self.assertIsNone(self.journal.get_change(change_id + 999))


class RollbackTests(JournalTestCase):
    def _log(self, kind: str = "supersede") -> tuple[int, dict]:
        before = {"description": "用户偏好 pnpm", "properties": {"w": 1.0, "valid_from": "2026-01-01"}}
        after = {"description": "用户偏好 npm", "properties": {"w": 2.0}}
        change_id = self.journal.log_change(
            "user", "ent_aaa", kind, before, after, confidence=0.8, source_turn_id="turn_9"
        )
        return change_id, before

    def test_rollback_restores_before_and_syncs_graph(self) -> None:
        change_id, before = self._log()
        self.advance(DAY)
        result = self.journal.rollback(change_id, self.graph, now=self.clock[0])
        # R6：必须通过 graph.edit_entity 恢复（其内部重算向量）
        self.assertEqual(len(self.graph.calls), 1)
        call = self.graph.calls[0]
        self.assertEqual(call["id"], "ent_aaa")
        self.assertEqual(call["description"], before["description"])
        self.assertEqual(call["properties"], before["properties"])
        self.assertEqual(result["restored"]["id"], "ent_aaa")
        self.assertEqual(result["restored"]["before"], before)
        self.assertEqual(result["kind"], "supersede")
        # 平铺别名（service.journal_rollback 包装 {"restored": rollback(...)} 时仍可读到 id/before/after）
        self.assertEqual(result["id"], "ent_aaa")
        self.assertEqual(result["before"], before)
        entry = self.journal.get_change(change_id)
        assert entry is not None
        self.assertTrue(entry["rolled_back"])
        # 写回滚记录（kind=rollback）
        entries = self.journal.list_entries("user", target_id="ent_aaa")
        self.assertEqual(entries[0]["kind"], "rollback")
        self.assertEqual(entries[0]["after"]["description"], before["description"])

    def test_rollback_twice_rejected(self) -> None:
        change_id, _ = self._log()
        self.journal.rollback(change_id, self.graph)
        with self.assertRaises(base.GraphMemoryError) as ctx:
            self.journal.rollback(change_id, self.graph)
        self.assertEqual(ctx.exception.code, "already_rolled_back")

    def test_rollback_expired_after_seven_days(self) -> None:
        change_id, _ = self._log()
        self.advance(7 * DAY + 1)
        with self.assertRaises(base.GraphMemoryError) as ctx:
            self.journal.rollback(change_id, self.graph, now=self.clock[0])
        self.assertEqual(ctx.exception.code, "rollback_expired")
        self.assertEqual(self.graph.calls, [])
        entry = self.journal.get_change(change_id)
        assert entry is not None
        self.assertFalse(entry["rolled_back"])

    def test_rollback_allowed_exactly_at_seven_days(self) -> None:
        change_id, _ = self._log()
        self.advance(7 * DAY)  # 边界内（> 才算过期）
        result = self.journal.rollback(change_id, self.graph, now=self.clock[0])
        self.assertEqual(result["restored"]["id"], "ent_aaa")

    def test_rollback_unknown_change(self) -> None:
        with self.assertRaises(base.GraphMemoryError) as ctx:
            self.journal.rollback(424242, self.graph)
        self.assertEqual(ctx.exception.code, "change_not_found")

    def test_rollback_kind_not_allowed(self) -> None:
        change_id, _ = self._log(kind="ingest")
        with self.assertRaises(base.GraphMemoryError) as ctx:
            self.journal.rollback(change_id, self.graph)
        self.assertEqual(ctx.exception.code, "rollback_not_allowed")

    def test_rollback_graph_failure_wrapped(self) -> None:
        change_id, _ = self._log()
        self.graph.fail_with = RuntimeError("backend down")
        with self.assertRaises(base.GraphMemoryError) as ctx:
            self.journal.rollback(change_id, self.graph)
        self.assertEqual(ctx.exception.code, "rollback_failed")
        entry = self.journal.get_change(change_id)
        assert entry is not None
        self.assertFalse(entry["rolled_back"])


class ActivationAndInjectionTests(JournalTestCase):
    def test_log_activation_and_recent_ids(self) -> None:
        self.journal.log_activation("user", "ent_a", "injected", 1.0, 2.0)
        self.advance(10)
        self.journal.log_activation("user", "ent_b", "injected", 1.0, 1.5)
        self.advance(10)
        ids = self.journal.recently_activated_ids("user", T0 - 1)
        self.assertEqual(ids, {"ent_a", "ent_b"})
        self.assertEqual(self.journal.recently_activated_ids("user", T0 + 25), set())
        rows = self.journal.activations("user", node_id="ent_a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "injected")

    def test_log_injection_and_stats(self) -> None:
        self.journal.log_injection("session_1", 1, {"memory": 100, "day": 50}, ["ent_a", "ent_b"])
        self.advance(5)
        self.journal.log_injection("session_1", 2, {"memory": 200}, ["ent_c"])
        stats = self.journal.injection_stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["last_tokens"], {"memory": 200})
        self.assertEqual(stats["last_injected_ids"], ["ent_c"])
        self.assertEqual(stats["total_memory_tokens"], 300)

    def test_injection_stats_empty(self) -> None:
        stats = self.journal.injection_stats("user")
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["last_injection"])
        self.assertEqual(stats["last_tokens"], {})


class AlarmAndDeadLetterTests(JournalTestCase):
    def test_alarms(self) -> None:
        self.journal.log_alarm("user", "credential_alarm", {"pattern": "sk-***"})
        self.journal.log_alarm("workspace", "prune_stuck", "连续 3 轮剪不到 low")
        recent = self.journal.recent_alarms("user")
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["kind"], "credential_alarm")
        self.assertEqual(recent[0]["detail"], {"pattern": "sk-***"})
        workspace_alarm = self.journal.recent_alarms("workspace")[0]
        self.assertEqual(workspace_alarm["detail"], "连续 3 轮剪不到 low")

    def test_dead_letter_lifecycle(self) -> None:
        dead_id = self.journal.enqueue_dead_letter(
            "user", {"turn_text": "记住我用 pnpm", "turn_id": "turn_5"}, "LLM timeout"
        )
        rows = self.journal.dead_letters()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], dead_id)
        self.assertEqual(rows[0]["payload"]["turn_id"], "turn_5")
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertFalse(rows[0]["replayed"])
        fetched = self.journal.get_dead_letter(dead_id)
        assert fetched is not None
        self.assertEqual(fetched["last_error"], "LLM timeout")
        self.assertTrue(self.journal.mark_dead_letter_replayed(dead_id))
        self.assertEqual(self.journal.dead_letters(), [])
        self.assertEqual(len(self.journal.dead_letters(include_replayed=True)), 1)
        self.assertTrue(self.journal.dead_letters("user", include_replayed=True)[0]["replayed"])
        self.assertEqual(self.journal.dead_letters("workspace"), [])

    def test_mark_unknown_dead_letter(self) -> None:
        self.assertFalse(self.journal.mark_dead_letter_replayed(999))
        self.assertIsNone(self.journal.get_dead_letter(999))


class IngestPruneDayEventTests(JournalTestCase):
    def test_log_ingest_and_prune_history(self) -> None:
        self.journal.log_ingest("user", "turn_1", "accepted", 2, 1)
        self.journal.log_ingest("user", "turn_2", "failed", 0, 0, reason="llm down")
        self.journal.log_prune_report("user", "compress", {"compressed": ["ent_a"], "merged": []})
        self.advance(3)
        self.journal.log_prune_report("user", "prune", {"archived": [], "deleted": []})
        history = self.journal.prune_history("user")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["kind"], "prune")
        self.assertEqual(history[0]["result"], {"archived": [], "deleted": []})
        self.assertEqual(history[1]["result"]["compressed"], ["ent_a"])

    def test_day_events_window(self) -> None:
        self.journal.log_day_event("user", "compress", {"summary_tokens": 120})
        self.advance(2 * DAY)
        self.journal.log_day_event("user", "rollover", {"date": "2026-10-29"})
        events = self.journal.day_events("user", days=7)
        self.assertEqual(len(events), 2)
        self.advance(10 * DAY)
        self.assertEqual(len(self.journal.day_events("user", days=7)), 0)

    def test_stats(self) -> None:
        self.journal.log_change("user", "ent_a", "update", "a", "b")
        self.journal.log_activation("user", "ent_a", "injected", 1.0, 2.0)
        self.journal.log_ingest("user", "turn_1", "accepted", 1, 0)
        self.journal.log_alarm("user", "warning", "x")
        self.journal.enqueue_dead_letter("user", {"a": 1}, "err")
        self.journal.log_injection("s", 1, {"memory": 10}, ["ent_a"])
        stats = self.journal.stats("user")
        self.assertEqual(stats["changes"], 1)
        self.assertEqual(stats["activations"], 1)
        self.assertEqual(stats["ingests"], 1)
        self.assertEqual(stats["alarms"], 1)
        self.assertEqual(stats["dead_letter"], 1)
        self.assertEqual(stats["dead_letter_pending"], 1)
        self.assertEqual(stats["injections"], 1)
        self.assertGreater(stats["db_size"], 0)


@unittest.skipIf(GraphStore is None, f"graph.py unavailable: {_GRAPH_IMPORT_ERROR}")
class RealGraphRollbackTests(unittest.TestCase):
    """与真实 GraphStore 集成：rollback 恢复 description/properties 且向量已重算（R6）。"""

    def setUp(self) -> None:
        self.tmpdir = make_temp_dir("gm-journal-graph")
        self.clock = [T0]
        self.graph = GraphStore(
            os.path.join(self.tmpdir, "graph-home"),
            "user",
            clock=lambda: self.clock[0],
        )
        self.journal = SQLiteJournal(
            os.path.join(self.tmpdir, "journal.db"),
            clock=lambda: self.clock[0],
        )

    def tearDown(self) -> None:
        self.journal.close()
        self.graph.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rollback_restores_entity_and_rebuilds_vector(self) -> None:
        entity_id = self.graph.upsert_entity(
            "包管理器", "Preference", "使用 pnpm 管理依赖", "user", properties={"w": 1.0}
        )
        node_before = self.graph.get_entity(entity_id)
        assert node_before is not None
        vector_before = dict(self.graph.entity_vectors[entity_id])
        before_state = {
            "description": node_before["description"],
            "properties": {
                "w": 1.0,
                "valid_from": node_before["properties"]["valid_from"],
            },
        }
        change_id = self.journal.log_change(
            "user", entity_id, "supersede", before_state, {"description": "使用 npm 管理依赖"}, confidence=0.9
        )
        # 模拟 supersede 已应用的最终状态
        self.graph.edit_entity(entity_id, description="使用 npm 管理依赖", properties={"w": 2.0})
        # R6：edit_entity 必须同步重算向量条目（精确断言向量内容已变）
        self.assertNotEqual(self.graph.entity_vectors[entity_id], vector_before)
        self.assertEqual(
            self.graph.entity_vectors[entity_id],
            base.embed_sparse(f"{node_before['name']}\n使用 npm 管理依赖"),
        )

        result = self.journal.rollback(change_id, self.graph, now=self.clock[0])
        self.assertEqual(result["restored"]["id"], entity_id)
        restored = self.graph.get_entity(entity_id)
        assert restored is not None
        self.assertEqual(restored["description"], "使用 pnpm 管理依赖")
        self.assertEqual(float(restored["properties"]["w"]), 1.0)
        # 回滚同样触发向量重算：恢复后的向量必须等于旧描述的确定性 embedding
        self.assertEqual(
            self.graph.entity_vectors[entity_id],
            base.embed_sparse(f"{node_before['name']}\n使用 pnpm 管理依赖"),
        )
        hits = [eid for eid, _score in self.graph.search_entities("使用 pnpm 管理依赖", "user", top_k=5)]
        self.assertIn(entity_id, hits)


if __name__ == "__main__":
    unittest.main()
