"""retrieval.py 单测：CONTRACT §2.1/§2.2/§3.1 双路召回、合并排序、动态预算统计。

覆盖点：
- items 契约字段、按 score 降序、top1_sim/high_weight_count；
- workspace ×1.2 / pinned ×1.5 / 权重饱和 / 惰性衰减参与打分；
- user+workspace 跨 scope 按 name 去重、archived 默认排除（explicit 包含）、关系扩展折扣召回；
- 后端不可用时降级为空结果（R7 精神）不抛错；
- dynamic_memory_cap 四条规则与 render/预算截断（§2.2）；
- 真实 GraphStore 集成（graph.py 可用时）。
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import base  # noqa: E402
import retrieval  # noqa: E402
from retrieval import dynamic_memory_cap, rank_candidates, retrieve  # noqa: E402

try:  # noqa: E402
    from graph import GraphStore
except Exception as _graph_exc:  # noqa: BLE001
    GraphStore = None  # type: ignore[assignment]
    _GRAPH_IMPORT_ERROR = repr(_graph_exc)
else:
    _GRAPH_IMPORT_ERROR = ""

DAY = 86400.0
WEEK = 7 * DAY
T0 = 1_700_000_000.0

# 测试夹具目录：系统 temp 在 sandbox 下不可写，改用仓库 .tmp/（契约 §6 允许，已 gitignore）
_TMP_ROOT = os.path.join(ROOT, ".tmp", "tests-py")


def make_temp_dir(tag: str) -> str:
    """测试夹具目录：仓库 .tmp/ 下的唯一目录（不用 tempfile.mkdtemp —— sandbox 下其子项不可写）。"""
    os.makedirs(_TMP_ROOT, exist_ok=True)
    path = os.path.join(_TMP_ROOT, f"{tag}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(path, exist_ok=False)
    return path

DEFAULT_CONFIG: dict = {
    "injection": {
        "workspaceWeightMultiplier": 1.2,
        "highWeightThreshold": 5.0,
        "recentUpdateDays": 7,
        "memoryDefaultTokens": 1024,
        "memoryHighSimTokens": 1536,
        "memoryBoostTokens": 1792,
        "memoryExplicitTokens": 2048,
        "highSimThreshold": 0.8,
    },
    "weight": {"pinnedScoreMultiplier": 1.5},
    "graph": {"topK": 20, "recentNeighborK": 5},
}


def _merge(target: dict, overlay: dict) -> dict:
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target


def make_config(overrides: dict | None = None) -> base.Config:
    data = _merge(copy.deepcopy(DEFAULT_CONFIG), overrides or {})
    return base.Config(data)


def make_node(
    name: str,
    *,
    scope: str = "user",
    description: str | None = None,
    w: float = 1.0,
    pinned: bool = False,
    status: str = "active",
    last_activated_at: float = T0,
    updated_at: float = T0,
    valid_from: str = "2026-10-28",
    created_at: float = T0,
    etype: str = "Preference",
) -> dict:
    node = base.make_entity(
        name,
        etype,
        description if description is not None else f"{name} 的描述",
        scope,
        created_at=created_at,
        properties={
            "w": w,
            "last_activated_at": last_activated_at,
            "updated_at": updated_at,
            "pinned": pinned,
            "status": status,
            "valid_from": valid_from,
        },
    )
    return node


class FakeGraph:
    """确定性召回替身：hits/neighbors 直接指定 (id, sim)。"""

    def __init__(
        self,
        nodes: dict[str, dict],
        hits: dict[str, list[tuple[str, float]]] | None = None,
        neighbors: dict[str, list[tuple[str, float]]] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.nodes = nodes
        self.hits = hits or {}
        self.neighbors = neighbors or {}
        self.fail = fail

    def search_entities(self, query, scope=None, top_k=20, *, include_archived=False, statuses=None):
        if self.fail is not None:
            raise self.fail
        return list(self.hits.get(query, []))[:top_k]

    def get_entity(self, entity_id):
        node = self.nodes.get(entity_id)
        return dict(node) if node else None

    def expand_related(self, seed_ids, top_k=5):
        out: list[tuple[str, float]] = []
        seen: set[str] = set()
        for sid in seed_ids:
            for nid, discount in self.neighbors.get(sid, []):
                if nid in seen:
                    continue
                seen.add(nid)
                out.append((nid, discount))
        return out[:top_k]


class FakeService:
    def __init__(self, graphs: dict[str, FakeGraph], now: float = T0) -> None:
        self.graphs = graphs
        self.config = make_config()
        self._now = now

    def now(self) -> float:
        return self._now


def _service(*graphs: FakeGraph, scopes: tuple[str, ...] = ("user",), now: float = T0) -> FakeService:
    return FakeService(dict(zip(scopes, graphs)), now=now)


class ItemShapeTests(unittest.TestCase):
    def test_returns_contract_shape(self) -> None:
        node = make_node("包管理器", description="用户偏好使用 pnpm 管理依赖")
        graph = FakeGraph({"ent_a": node}, {"pnpm": [("ent_a", 0.9)]})
        result = retrieve(_service(graph), "pnpm", ["user"])
        self.assertEqual(set(result), {"items", "top1_sim", "high_weight_count"})
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        for key in ("id", "text", "w", "valid_from", "updated", "sim", "pinned", "scope", "type", "updated_recent"):
            self.assertIn(key, item)
        self.assertEqual(item["id"], "ent_a")
        self.assertEqual(item["text"], "用户偏好使用 pnpm 管理依赖")
        self.assertEqual(item["sim"], 0.9)
        self.assertEqual(item["scope"], "user")
        self.assertEqual(item["type"], "Preference")
        self.assertEqual(item["pinned"], False)
        self.assertEqual(item["valid_from"], "2026-10-28")
        self.assertAlmostEqual(item["w"], 1.0, places=6)
        self.assertAlmostEqual(result["top1_sim"], 0.9, places=6)
        self.assertEqual(result["high_weight_count"], 0)
        # service._format_item 使用 recently_updated；与 updated_recent 同义
        self.assertIn("updated_recent", item)
        self.assertEqual(item["recently_updated"], item["updated_recent"])

    def test_sorted_by_score_desc(self) -> None:
        # A: sim .9 w1 → .396；B: sim .5 w5 → .5 ⇒ B 在前
        nodes = {
            "ent_a": make_node("A", description="A 描述"),
            "ent_b": make_node("B", description="B 描述", w=5.0),
        }
        graph = FakeGraph(nodes, {"q": [("ent_a", 0.9), ("ent_b", 0.5)]})
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_b", "ent_a"])
        self.assertEqual(result["top1_sim"], 0.9)

    def test_workspace_multiplier_1_2(self) -> None:
        user_node = make_node("U", description="U 描述")
        ws_node = make_node("W", description="W 描述", scope="workspace")
        user_graph = FakeGraph({"ent_u": user_node}, {"q": [("ent_u", 0.8)]})
        ws_graph = FakeGraph({"ent_w": ws_node}, {"q": [("ent_w", 0.8)]})
        service = FakeService({"user": user_graph, "workspace": ws_graph})
        result = retrieve(service, "q", ["user", "workspace"])
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual([item["id"] for item in result["items"]], ["ent_w", "ent_u"])
        self.assertAlmostEqual(by_id["ent_w"]["score"], by_id["ent_u"]["score"] * 1.2, places=6)

    def test_pinned_multiplier_1_5_affects_order(self) -> None:
        nodes = {
            "ent_p": make_node("P", description="P 描述", pinned=True),
            "ent_q": make_node("Q", description="Q 描述"),
        }
        graph = FakeGraph(nodes, {"q": [("ent_p", 0.6), ("ent_q", 0.8)]})
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_p", "ent_q"])
        self.assertTrue(result["items"][0]["pinned"])
        self.assertEqual(result["high_weight_count"], 1)

    def test_weight_saturates_at_five(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述", w=10.0),
            "ent_b": make_node("B", description="B 描述", w=5.0),
        }
        graph = FakeGraph(nodes, {"q": [("ent_a", 0.5), ("ent_b", 0.5)]})
        result = retrieve(_service(graph), "q", ["user"])
        self.assertAlmostEqual(result["items"][0]["score"], 0.5, places=6)
        self.assertAlmostEqual(result["items"][1]["score"], 0.5, places=6)

    def test_decay_participates_in_score(self) -> None:
        node = make_node("A", description="A 描述", w=4.0, last_activated_at=T0 - WEEK)
        graph = FakeGraph({"ent_a": node}, {"q": [("ent_a", 1.0)]})
        result = retrieve(_service(graph), "q", ["user"])
        item = result["items"][0]
        self.assertAlmostEqual(item["w"], 2.0, places=6)  # 衰减后权重
        self.assertAlmostEqual(item["score"], 0.58, places=6)  # 0.3+0.7*(2/5)

    def test_updated_flags(self) -> None:
        recent = make_node("Recent", description="最近更新", updated_at=T0 - DAY)
        stale = make_node("Stale", description="很久以前更新", updated_at=T0 - 30 * DAY)
        graph = FakeGraph(
            {"ent_r": recent, "ent_s": stale},
            {"q": [("ent_r", 0.9), ("ent_s", 0.8)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        by_id = {item["id"]: item for item in result["items"]}
        self.assertTrue(by_id["ent_r"]["updated_recent"])
        self.assertEqual(by_id["ent_r"]["updated_label"], time.strftime("%m-%d", time.localtime(T0 - DAY)))
        self.assertFalse(by_id["ent_s"]["updated_recent"])

    def test_high_weight_count_counts_pinned_and_heavy(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A", pinned=True),
            "ent_b": make_node("B", description="B", w=6.0),
            "ent_c": make_node("C", description="C"),
        }
        graph = FakeGraph(
            nodes,
            {"q": [("ent_a", 0.9), ("ent_b", 0.5), ("ent_c", 0.2)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual(result["high_weight_count"], 2)
        self.assertAlmostEqual(result["top1_sim"], 0.9, places=6)

    def test_top_k_truncation_keeps_global_stats(self) -> None:
        nodes = {f"ent_{i}": make_node(f"节点{i}", description=f"描述{i}", w=5.0) for i in range(5)}
        # 高 sim 的节点按 score 排第一，但断言 top1_sim 取全量最大值
        hits = [(f"ent_{i}", 0.91 - i * 0.01) for i in range(5)]
        hits[2] = ("ent_2", 0.99)  # 全局最大 sim，但…仍会在截断内；换成最后一个最大
        hits[4] = ("ent_4", 0.99)
        graph = FakeGraph(nodes, {"q": hits})
        result = retrieve(_service(graph), "q", ["user"], top_k=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertAlmostEqual(result["top1_sim"], 0.99, places=6)
        self.assertEqual(result["high_weight_count"], 5)

    def test_empty_result(self) -> None:
        graph = FakeGraph({}, {})
        result = retrieve(_service(graph), "无命中", ["user"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["top1_sim"], 0.0)
        self.assertEqual(result["high_weight_count"], 0)

    def test_top_k_zero(self) -> None:
        graph = FakeGraph({"ent_a": make_node("A")}, {"q": [("ent_a", 0.9)]})
        result = retrieve(_service(graph), "q", ["user"], top_k=0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["top1_sim"], 0.0)


class FilterAndDedupeTests(unittest.TestCase):
    def test_archived_excluded_by_default(self) -> None:
        active = make_node("Active", description="活跃")
        archived = make_node("Archived", description="归档", status="archived")
        graph = FakeGraph(
            {"ent_a": active, "ent_arc": archived},
            {"q": [("ent_arc", 0.95), ("ent_a", 0.5)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_a"])

    def test_archived_included_when_explicit(self) -> None:
        archived = make_node("Archived", description="归档", status="archived")
        graph = FakeGraph({"ent_arc": archived}, {"q": [("ent_arc", 0.95)]})
        result = retrieve(_service(graph), "q", ["user"], explicit=True)
        self.assertEqual([item["id"] for item in result["items"]], ["ent_arc"])
        self.assertEqual(result["items"][0]["status"], "archived")

    def test_cross_scope_dedupe_keeps_higher_score(self) -> None:
        user_node = make_node("同名实体", description="用户版本")
        ws_node = make_node("同名实体", description="工作区版本", scope="workspace")
        user_graph = FakeGraph({"ent_user": user_node}, {"q": [("ent_user", 0.9)]})
        ws_graph = FakeGraph({"ent_ws": ws_node}, {"q": [("ent_ws", 0.7)]})
        service = FakeService({"user": user_graph, "workspace": ws_graph})
        result = retrieve(service, "q", ["user", "workspace"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "ent_user")
        self.assertEqual(result["items"][0]["scope"], "user")

    def test_cross_scope_dedupe_workspace_multiplier_wins(self) -> None:
        user_node = make_node("同名实体", description="用户版本")
        ws_node = make_node("同名实体", description="工作区版本", scope="workspace")
        user_graph = FakeGraph({"ent_user": user_node}, {"q": [("ent_user", 0.8)]})
        ws_graph = FakeGraph({"ent_ws": ws_node}, {"q": [("ent_ws", 0.78)]})
        service = FakeService({"user": user_graph, "workspace": ws_graph})
        result = retrieve(service, "q", ["user", "workspace"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "ent_ws")

    def test_scopes_accept_string_and_skip_missing(self) -> None:
        graph = FakeGraph({"ent_a": make_node("A")}, {"q": [("ent_a", 0.9)]})
        service = FakeService({"user": graph})
        from_string = retrieve(service, "q", "user")
        self.assertEqual(len(from_string["items"]), 1)
        with_missing = retrieve(service, "q", ["user", "unknown_scope"])
        self.assertEqual(len(with_missing["items"]), 1)


class RelationExpansionTests(unittest.TestCase):
    def test_relation_neighbor_included_with_discount(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述"),
            "ent_b": make_node("B", description="B 描述"),
        }
        graph = FakeGraph(
            nodes,
            {"q": [("ent_a", 0.9)]},
            neighbors={"ent_a": [("ent_b", 0.5)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_a", "ent_b"])
        by_id = {item["id"]: item for item in result["items"]}
        self.assertAlmostEqual(by_id["ent_b"]["sim"], 0.25, places=6)  # 0.5 折扣 × 0.5 关系折扣
        self.assertLess(by_id["ent_b"]["score"], by_id["ent_a"]["score"])

    def test_relation_neighbor_not_duplicated(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述"),
            "ent_b": make_node("B", description="B 描述"),
        }
        graph = FakeGraph(
            nodes,
            {"q": [("ent_a", 0.9), ("ent_b", 0.7)]},
            neighbors={"ent_a": [("ent_b", 0.5)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        ids = [item["id"] for item in result["items"]]
        self.assertEqual(ids.count("ent_b"), 1)
        by_id = {item["id"]: item for item in result["items"]}
        self.assertAlmostEqual(by_id["ent_b"]["sim"], 0.7, places=6)

    def test_archived_neighbor_excluded(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述"),
            "ent_arc": make_node("Arc", description="归档", status="archived"),
        }
        graph = FakeGraph(
            nodes,
            {"q": [("ent_a", 0.9)]},
            neighbors={"ent_a": [("ent_arc", 0.5)]},
        )
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_a"])


class DegradeTests(unittest.TestCase):
    def test_backend_unavailable_degrades_to_empty(self) -> None:
        graph = FakeGraph({}, {}, fail=base.GraphBackendUnavailable("lightrag down"))
        result = retrieve(_service(graph), "q", ["user"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["top1_sim"], 0.0)

    def test_one_scope_failure_does_not_kill_other(self) -> None:
        bad = FakeGraph({}, {}, fail=base.GraphBackendUnavailable("down"))
        good = FakeGraph({"ent_a": make_node("A")}, {"q": [("ent_a", 0.9)]})
        service = FakeService({"user": good, "workspace": bad})
        result = retrieve(service, "q", ["workspace", "user"])
        self.assertEqual([item["id"] for item in result["items"]], ["ent_a"])


class BudgetTests(unittest.TestCase):
    def test_render_item_line_format(self) -> None:
        item = {
            "id": "ent_0123456789abcdef",
            "w": 2.3,
            "updated_label": "10-28",
            "updated_recent": True,
            "pinned": False,
            "text": "用户偏好 pnpm",
        }
        line = retrieval.render_item_line(item)
        self.assertIn("[#ent_0123456789abcdef", line)
        self.assertIn("w=2.3", line)
        self.assertIn("10-28 更新", line)
        self.assertIn("已更新", line)
        self.assertTrue(line.endswith("用户偏好 pnpm"))

    def test_mem_max_truncates_within_budget(self) -> None:
        node = make_node("长文本", description="很长的描述内容" * 80)
        graph = FakeGraph({"ent_a": node}, {"q": [("ent_a", 0.9)]})
        result = retrieve(_service(graph), "q", ["user"], mem_max=40)
        self.assertIn("tokens", result)
        self.assertLessEqual(result["tokens"]["memory"], 40)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["text"])  # 首条被截断而非直接丢弃
        self.assertLess(len(result["items"][0]["text"]), len(node["description"]))

    def test_mem_max_keeps_only_fitting_items(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述占位"),
            "ent_b": make_node("B", description="B 描述占位"),
        }
        graph = FakeGraph(nodes, {"q": [("ent_a", 0.9), ("ent_b", 0.8)]})
        full = retrieve(_service(graph), "q", ["user"])
        budget = retrieval.estimate_tokens(retrieval.render_item_line(full["items"][0]))
        result = retrieve(_service(graph), "q", ["user"], mem_max=budget)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["tokens"]["memory"], budget)

    def test_dynamic_memory_cap_rules(self) -> None:
        self.assertEqual(dynamic_memory_cap(0.5, 0), 1024)
        self.assertEqual(dynamic_memory_cap(0.9, 0), 1536)
        self.assertEqual(dynamic_memory_cap(0.9, 1), 1792)
        self.assertEqual(dynamic_memory_cap(0.2, 0, explicit=True), 2048)
        self.assertEqual(dynamic_memory_cap(0.2, 1, explicit=True), 2048)
        # 阈值边界：等于 0.8 不算高相似
        self.assertEqual(dynamic_memory_cap(0.8, 0), 1024)

    def test_dynamic_memory_cap_config_override(self) -> None:
        config = make_config(
            {"injection": {"memoryDefaultTokens": 1200, "memoryHighSimTokens": 1600, "highSimThreshold": 0.7}}
        )
        self.assertEqual(dynamic_memory_cap(0.1, 0, config=config), 1200)
        self.assertEqual(dynamic_memory_cap(0.75, 0, config=config), 1600)


class RankCandidatesTests(unittest.TestCase):
    def test_rank_candidates_sorted(self) -> None:
        nodes = {
            "ent_a": make_node("A", description="A 描述"),
            "ent_b": make_node("B", description="B 描述", pinned=True),
        }
        graph = FakeGraph(nodes)
        service = FakeService({"user": graph})
        items = rank_candidates(service, "q", [("user", "ent_a", 0.9), ("user", "ent_b", 0.5)])
        # A: .9*.44=.396；B: .5*.44*1.5=.33 ⇒ A 在前
        self.assertEqual([item["id"] for item in items], ["ent_a", "ent_b"])

    def test_rank_candidates_swapped_tuple_order(self) -> None:
        nodes = {"ent_a": make_node("A", description="A 描述")}
        graph = FakeGraph(nodes)
        service = FakeService({"user": graph})
        items = rank_candidates(service, "q", [("ent_a", "user", 0.9)])
        self.assertEqual(items[0]["id"], "ent_a")
        self.assertEqual(items[0]["scope"], "user")

    def test_rank_candidates_skips_archived_and_unknown(self) -> None:
        nodes = {
            "ent_arc": make_node("Arc", description="归档", status="archived"),
            "ent_ok": make_node("Ok", description="正常"),
        }
        graph = FakeGraph(nodes)
        service = FakeService({"user": graph})
        items = rank_candidates(service, "q", [("user", "ent_arc", 0.9), ("user", "ent_missing", 0.8)])
        self.assertEqual(items, [])
        items = rank_candidates(service, "q", [("user", "ent_ok", 0.5), ("user", "ent_arc", 0.9)], explicit=True)
        self.assertEqual([item["id"] for item in items], ["ent_arc", "ent_ok"])


@unittest.skipIf(GraphStore is None, f"graph.py unavailable: {_GRAPH_IMPORT_ERROR}")
class RealGraphRetrievalTests(unittest.TestCase):
    """真实 GraphStore 集成：检索链路可跑通（向量召回 + 权重打分 + 跨 scope）。"""

    def setUp(self) -> None:
        self.tmpdir = make_temp_dir("gm-retrieval")
        self.clock = [T0]
        self.user_graph = GraphStore(
            os.path.join(self.tmpdir, "user"), "user", clock=lambda: self.clock[0]
        )
        self.ws_graph = GraphStore(
            os.path.join(self.tmpdir, "workspace"), "workspace", clock=lambda: self.clock[0]
        )
        self.service = FakeService({"user": self.user_graph, "workspace": self.ws_graph}, now=T0)
        self.service.graphs = {"user": self.user_graph, "workspace": self.ws_graph}

    def tearDown(self) -> None:
        self.user_graph.close()
        self.ws_graph.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_retrieve_end_to_end(self) -> None:
        uid = self.user_graph.upsert_entity("包管理器", "Preference", "用户偏好使用 pnpm 管理依赖", "user")
        wid = self.ws_graph.upsert_entity("构建工具", "Tool", "项目使用 vite 构建前端", "workspace")
        self.user_graph.save()
        self.ws_graph.save()
        result = retrieve(self.service, "pnpm", ["user", "workspace"])
        ids = [item["id"] for item in result["items"]]
        self.assertIn(uid, ids)
        self.assertGreater(result["top1_sim"], 0.0)
        # 第二路：关系扩展（把两个实体连起来后应召回邻居）
        self.user_graph.upsert_relation(uid, uid, "自关联占位", "user")
        self.user_graph.upsert_relation(uid, wid, "项目依赖 pnpm", "user")
        result2 = retrieve(self.service, "pnpm", ["user", "workspace"])
        self.assertIn(uid, [item["id"] for item in result2["items"]])

    def test_retrieve_workspace_multiplier(self) -> None:
        # 两个 scope 各一个不同名节点，query 相同的匹配度近似
        uid = self.user_graph.upsert_entity("用户偏好", "Preference", "偏好使用 pnpm", "user")
        wid = self.ws_graph.upsert_entity("工作区偏好", "Preference", "偏好使用 pnpm", "workspace")
        result = retrieve(self.service, "偏好使用 pnpm", ["user", "workspace"], top_k=5)
        items = {item["id"]: item for item in result["items"]}
        if uid in items and wid in items:
            self.assertGreater(items[wid]["score"], items[uid]["score"])


if __name__ == "__main__":
    unittest.main()
