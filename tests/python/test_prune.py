"""T3 单测：两阶段剪枝状态机（契约 §2.6，R4）。

覆盖：B（compress 轮零删除，隔轮才归档）、E1（低规模 90 天零删除 + 可 recall + 激活回升）、
E2（high+1 开始剪、单调下降到 <=low 停）、E3（low~high 波动不再触发）、E4（单轮 ≤8%）、
E5（pinned 90% 连续 3 轮剪不到 low → 停止 + prune_stuck 告警）、E6（user 超上界、workspace 零剪枝）、
dry_run 只出报告、冷区 30 天未命中真删除、相似碎片 merge、水位线 calibration/持久化、P95 自适应。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(ROOT, "sidecar") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "sidecar"))

from base import Config, STATUS_ARCHIVED, STATUS_COMPRESSED, STATUS_PENDING_FORGET  # noqa: E402
from graph import GraphStore  # noqa: E402
from prune import Pruner  # noqa: E402

with open(os.path.join(ROOT, "config", "default.json"), "r", encoding="utf-8-sig") as _fh:
    DEFAULT_CONFIG_DATA = json.load(_fh)

BASE_TS = time.mktime(time.strptime("2026-01-05 10:00:00", "%Y-%m-%d %H:%M:%S"))
REPO_TMP = os.path.join(ROOT, ".tmp")


def make_tmp_dir(prefix: str) -> str:
    """测试夹具目录必须放在仓库 `.tmp/`（已 gitignore）：本机 Windows 沙箱下系统临时目录
    创建的子目录 ACL 受限，原子写会挂起/权限失败。"""
    os.makedirs(REPO_TMP, exist_ok=True)
    path = os.path.join(REPO_TMP, f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    return path


def make_config() -> Config:
    return Config(json.loads(json.dumps(DEFAULT_CONFIG_DATA)), base_dir=ROOT)


class FakeClock:
    def __init__(self, ts: float = BASE_TS) -> None:
        self.ts = float(ts)

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += float(seconds)

    def set(self, ts: float) -> None:
        self.ts = float(ts)

    def day(self, days: int = 0) -> None:
        self.ts = BASE_TS + days * 86400


class FakeLlm:
    def __init__(self, response=None) -> None:
        self.response = response or "合并后的描述：统一使用 pnpm 管理依赖。"
        self.calls: list[dict] = []

    async def complete(self, prompt, *, system=None, max_tokens=1024, purpose=""):
        self.calls.append({"prompt": prompt, "purpose": purpose, "max_tokens": max_tokens})
        if callable(self.response):
            return self.response(prompt, purpose)
        return self.response


class FakeJournal:
    def __init__(self) -> None:
        self.alarms: list[dict] = []
        self.changes: list[dict] = []
        self.reports: list[dict] = []
        self.activations: list[dict] = []

    def log_alarm(self, scope, kind, detail=None):
        self.alarms.append({"scope": scope, "kind": kind, "detail": detail})

    def log_change(self, scope, target_id, kind, before=None, after=None):
        self.changes.append(
            {"scope": scope, "target_id": target_id, "kind": kind, "before": before, "after": after}
        )

    def log_prune_report(self, scope, phase, result):
        self.reports.append({"scope": scope, "phase": phase, "result": result})

    def log_activation(self, *args, **kwargs):
        self.activations.append({"args": args, "kwargs": kwargs})

    def prune_history(self, scope):
        return list(self.reports)

    def injection_stats(self, scope):
        return {}


class PrunerTestBase(unittest.TestCase):
    scope = "workspace"

    def setUp(self) -> None:
        self.tmp = make_tmp_dir("gm-prune")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.config = make_config()
        # 测试夹具描述是模板化的；默认调高 merge 阈值避免无关用例被"相似碎片合并"影响。
        # merge 用例会单独把阈值还原为契约值 0.9。
        self.config.data["prune"]["mergeSimThreshold"] = 1.1
        self.journal = FakeJournal()
        self.llm = FakeLlm()
        self.graph = GraphStore(self.tmp, self.scope, clock=self.clock)
        self.pruner = Pruner(
            self.tmp, self.scope, self.config, self.graph, self.journal, self.llm, clock=self.clock
        )

    # ------------------------------------------------------------------ #
    def run_async(self, coro):
        return asyncio.run(coro)

    def make_entity(
        self,
        name: str,
        *,
        w: float = 1.0,
        days_old: float = 0.0,
        etype: str = "Concept",
        description: str | None = None,
        properties: dict | None = None,
    ) -> str:
        ts = self.clock()
        props = {
            "w": w,
            "last_activated_at": ts - days_old * 86400,
            "updated_at": ts - days_old * 86400,
            "created_at": ts - days_old * 86400,
        }
        props.update(properties or {})
        return self.graph.upsert_entity(
            name,
            etype,
            description or f"{name}：独立的项目知识条目，记录不同主题与结论。",
            self.scope,
            properties=props,
        )

    def status_of(self, entity_id: str) -> str:
        node = self.graph.get_entity(entity_id) or {}
        return str((node.get("properties") or {}).get("status") or "")

    def maintenance_step(self):
        """模拟 service.maintenance() 的单轮（不含 round_tick），返回 (gate, performed)。"""
        pruner = self.pruner
        gate = pruner.gate()
        phase = str(pruner.state().get("phase") or "none")
        action = str(gate.get("action") or "idle")
        performed = None
        if action in ("compress", "force") and phase != "compressed":
            performed = ("compress", self.run_async(pruner.compress()))
        elif action in ("prune", "force") and phase == "compressed":
            performed = ("prune", self.run_async(pruner.prune(dry_run=False)))
        return gate, performed


# --------------------------------------------------------------------------- #
# B：压缩先于删除（R4）
# --------------------------------------------------------------------------- #
class CompressBeforeDeleteTest(PrunerTestBase):
    def _seed_cold(self, count: int) -> list[str]:
        ids = []
        for index in range(count):
            ids.append(
                self.make_entity(
                    f"冷知识-{index:03d}",
                    w=0.2,
                    days_old=10,
                    description=f"冷知识-{index:03d}：这是第 {index} 条独立知识，主题为验收编号 {index * 7 % 97}。",
                )
            )
        return ids

    def test_b_compress_round_zero_deletion_then_archive_next_round(self) -> None:
        cold_ids = self._seed_cold(50)
        before_updated = {
            entity_id: (self.graph.get_entity(entity_id) or {}).get("properties", {}).get("updated_at")
            for entity_id in cold_ids
        }
        self.assertEqual(self.graph.count(), 50)
        self.assertEqual(self.pruner.round_tick(), 1)
        compressed = self.run_async(self.pruner.compress())
        self.assertEqual(sorted(compressed["compressed"]), sorted(cold_ids))
        self.assertEqual(compressed["merged"], [])
        self.assertEqual(self.graph.count(), 50)
        for entity_id in cold_ids:
            self.assertEqual(self.status_of(entity_id), STATUS_COMPRESSED)
            props = (self.graph.get_entity(entity_id) or {}).get("properties") or {}
            self.assertEqual(float(props.get("w")), 0.2)  # 权重不清零
            self.assertIsNotNone(props.get("compressed_at"))
            self.assertEqual(props.get("updated_at"), before_updated[entity_id])  # 不伪造"最近改过"
        # R4：同一轮 prune 只出报告，零归档/零删除
        same_round = self.run_async(self.pruner.prune(dry_run=False))
        self.assertEqual(same_round["archived"], [])
        self.assertEqual(same_round["deleted"], [])
        self.assertEqual(same_round["report"]["reason"], "same_round_compress")
        self.assertEqual(self.graph.count(), 50)
        # 隔轮才归档（单轮 ≤ceil(50*0.08)=4）
        self.assertEqual(self.pruner.round_tick(), 2)
        pruned = self.run_async(self.pruner.prune(dry_run=False))
        self.assertEqual(len(pruned["archived"]), 4)
        self.assertEqual(pruned["deleted"], [])
        self.assertLessEqual(len(pruned["archived"]), pruned["report"]["limit"])
        for entity_id in pruned["archived"]:
            self.assertEqual(self.status_of(entity_id), STATUS_ARCHIVED)
        self.assertEqual(self.graph.count(), 46)
        self.assertEqual(self.graph.count(include_archived=True), 50)

    def test_protected_nodes_are_not_compressed(self) -> None:
        now = self.clock()
        eligible = self.make_entity("可压缩节点", w=0.2, days_old=10)
        new_node = self.make_entity("新建节点", w=0.2, days_old=0)
        recent_node = self.make_entity("近期更新节点", w=0.2, days_old=1)
        pinned = self.make_entity("置顶节点", w=0.2, days_old=20, properties={"pinned": True})
        preference = self.make_entity("偏好节点", w=0.2, days_old=20, etype="Preference")
        protect_until = self.make_entity(
            "改口保护节点", w=0.2, days_old=20, properties={"protect_until": now + 86400}
        )
        workspace_hit = self.make_entity(
            "工作区命中节点",
            w=0.2,
            days_old=2,
            properties={"workspace_hit": True, "last_activated_at": now - 86400},
        )
        pending = self.make_entity(
            "待遗忘节点", w=0.05, days_old=20, properties={"status": STATUS_PENDING_FORGET, "updated_at": now}
        )
        self.pruner.round_tick()
        result = self.run_async(self.pruner.compress())
        self.assertEqual(result["compressed"], [eligible])
        for entity_id in (new_node, recent_node, pinned, preference, protect_until, workspace_hit, pending):
            self.assertNotEqual(self.status_of(entity_id), STATUS_COMPRESSED)
        self.assertGreaterEqual(len(result["protected"]), 1)
        self.assertEqual(result["compressed"][0], eligible)

    def test_pending_forget_only_prune_handles_it(self) -> None:
        pending = self.make_entity(
            "用户要求遗忘的节点", w=0.0, days_old=0, properties={"status": STATUS_PENDING_FORGET}
        )
        self.pruner.round_tick()
        compressed = self.run_async(self.pruner.compress())
        self.assertEqual(compressed["compressed"], [])
        self.assertNotEqual(self.status_of(pending), STATUS_COMPRESSED)
        self.pruner.round_tick()
        pruned = self.run_async(self.pruner.prune(dry_run=False))
        self.assertIn(pending, pruned["archived"])

    def test_merge_similar_fragments_keeps_highest_weight(self) -> None:
        self.config.data["prune"]["mergeSimThreshold"] = 0.9
        self.pruner.calibrate(high=1)  # 门控触发（count=2 > high=1）时才做碎片 merge
        shared = "Python 项目依赖管理统一使用 pnpm 工具进行安装与升级，锁文件提交到仓库。"
        first = self.make_entity("依赖管理", w=0.2, days_old=20, description=shared)
        second = self.make_entity("依赖管理碎片", w=0.5, days_old=20, description=shared)
        self.pruner.round_tick()
        result = self.run_async(self.pruner.compress())
        self.assertTrue(result.get("merge_enabled"))
        self.assertEqual(len(result["merged"]), 1, result)
        merged_ids = result["merged"][0]
        self.assertEqual(len(merged_ids), 2)
        self.assertEqual(self.graph.count(), 1)
        keep_id = merged_ids[0]
        self.assertEqual(keep_id, second)  # 保留最高权重节点
        keep = self.graph.get_entity(keep_id) or {}
        self.assertIn("pnpm", keep.get("description") or "")
        self.assertEqual(self.status_of(keep_id), STATUS_COMPRESSED)
        self.assertGreaterEqual(len(self.llm.calls), 1)
        self.assertEqual(self.llm.calls[0]["purpose"], "merge")

    def test_merge_only_under_size_gate(self) -> None:
        """门控是唯一开关：小图（count <= high）显式 compress 不做 LLM merge。"""
        self.config.data["prune"]["mergeSimThreshold"] = 0.9
        shared = "完全相同的一段记忆碎片描述，用于验证小图不合并。"
        first = self.make_entity("小图碎片A", w=0.2, days_old=20, description=shared)
        second = self.make_entity("小图碎片B", w=0.3, days_old=20, description=shared)
        self.pruner.round_tick()
        result = self.run_async(self.pruner.compress())
        self.assertFalse(result.get("merge_enabled"))
        self.assertEqual(result["merged"], [])
        self.assertEqual(sorted(result["compressed"]), sorted([first, second]))
        self.assertEqual(self.graph.count(), 2)
        self.assertEqual(self.llm.calls, [])


# --------------------------------------------------------------------------- #
# E1：低规模 90 天不删除、可 recall、激活权重回升
# --------------------------------------------------------------------------- #
class E1Test(PrunerTestBase):
    def test_e1_low_scale_90_days_zero_deletion_and_recall(self) -> None:
        cold_ids = []
        for index in range(200):
            cold_ids.append(
                self.make_entity(
                    f"冷知识节点-{index:03d}",
                    w=0.01,
                    days_old=120,
                    description=f"冷知识节点-{index:03d}：长期未使用的独立知识，编号 {index}。",
                )
            )
        for index in range(800):
            self.make_entity(f"活跃节点-{index:03d}", w=1.0, days_old=0)
        self.assertEqual(self.graph.count(), 1000)
        for day in range(90):
            self.clock.set(BASE_TS + day * 86400)
            self.pruner.round_tick()
            gate = self.pruner.gate()
            self.assertEqual(gate["action"], "idle", f"day {day}: count 远低 high 不应剪枝")
        self.assertEqual(self.graph.count(), 1000)
        self.assertEqual(self.graph.count(include_archived=True), 1000)
        self.assertEqual(self.journal.alarms, [])
        # 可 recall（未归档/未删除）
        hits = self.graph.search_entities("冷知识节点-005 编号", self.scope, top_k=5)
        self.assertTrue(hits, "冷节点应仍可召回")
        self.assertEqual(self.status_of(cold_ids[5]), "active")
        # 激活后权重回升
        now = self.clock()
        node = self.graph.get_entity(cold_ids[5]) or {}
        before = self.pruner._current_w(node, now)
        try:
            from weight import activate as weight_activate  # noqa: PLC0415

            new_props = weight_activate(node, now, pinned=False, tool_boosted=False, workspace_hit=False)
        except Exception:  # noqa: BLE001 — weight.py 由 T1 提供；未就绪时按契约公式手工模拟
            new_props = {
                "w": min(10.0, before + 0.5),
                "last_activated_at": now,
                "status": "active",
            }
        self.graph.update_properties(cold_ids[5], dict(new_props))
        after = self.pruner._current_w(self.graph.get_entity(cold_ids[5]) or {}, now)
        self.assertGreater(after, before + 0.3, (before, after))


# --------------------------------------------------------------------------- #
# E2：high+1 开始剪，单调下降到 <=low 停
# --------------------------------------------------------------------------- #
class E2Test(PrunerTestBase):
    def test_e2_high_plus_one_prune_monotonic_until_low(self) -> None:
        self.pruner.calibrate(high=100, low=80, hard=250)
        self.assertEqual(self.pruner.watermarks(), {"high": 100, "low": 80, "hard": 250})
        for index in range(30):
            self.make_entity(f"冷知识-{index}", w=0.2, days_old=30)
        for index in range(71):
            self.make_entity(f"活跃知识-{index}", w=1.0, days_old=0)
        self.assertEqual(self.graph.count(), 101)
        counts = [self.graph.count()]
        actions: list[str] = []
        for _ in range(40):
            self.pruner.round_tick()
            gate, performed = self.maintenance_step()
            if performed:
                actions.append(performed[0])
            counts.append(self.graph.count())
            if performed is None and gate["action"] == "idle":
                break
        self.assertLessEqual(self.graph.count(), 80, counts)
        for previous, current in zip(counts, counts[1:]):
            self.assertLessEqual(current, previous, counts)
        self.assertGreaterEqual(counts[0], 101)
        self.assertIn("compress", actions)
        self.assertIn("prune", actions)
        self.assertGreaterEqual(self.graph.count(include_archived=True) - self.graph.count(), 1)
        final_gate = self.pruner.gate()
        self.assertEqual(final_gate["action"], "idle")
        self.assertEqual(self.pruner.state()["phase"], "idle")


# --------------------------------------------------------------------------- #
# E3：low~high 波动 10 天，剪枝触发 ≤1
# --------------------------------------------------------------------------- #
class E3Test(PrunerTestBase):
    def test_e3_band_fluctuation_does_not_retrigger(self) -> None:
        self.pruner.calibrate(high=100, low=80, hard=250)
        for index in range(25):
            self.make_entity(f"冷知识-{index}", w=0.2, days_old=30)
        for index in range(76):
            self.make_entity(f"活跃知识-{index}", w=1.0, days_old=0)
        self.assertEqual(self.graph.count(), 101)
        # 第一次触发：压缩 + 剪到 <=low 后进入 idle
        trigger_episodes = 0
        previous_idle = True
        for _ in range(40):
            self.pruner.round_tick()
            gate, performed = self.maintenance_step()
            if performed and previous_idle:
                trigger_episodes += 1
                previous_idle = False
            if performed is None and gate["action"] == "idle":
                previous_idle = True
                if self.graph.count() <= 80:
                    break
        self.assertLessEqual(self.graph.count(), 80)
        self.assertLessEqual(trigger_episodes, 1)
        # 随后 10 天在 low~high 之间波动：不得再次触发
        target_counts = [95, 88, 97, 84, 92, 99, 86, 94, 90]
        for day, target in enumerate(target_counts):
            self.clock.set(BASE_TS + (day + 1) * 86400)
            while self.graph.count() < target:
                self.make_entity(f"波动节点-{day}-{self.graph.count()}", w=1.0, days_old=0)
            for _ in range(3):
                self.pruner.round_tick()
                gate, performed = self.maintenance_step()
                self.assertIsNone(performed, f"band 内不应触发剪枝: {gate}")
                self.assertEqual(gate["action"], "idle")
        self.assertLessEqual(trigger_episodes, 1)


# --------------------------------------------------------------------------- #
# E4：单轮处理量 ≤8%
# --------------------------------------------------------------------------- #
class E4Test(PrunerTestBase):
    def test_e4_single_round_processing_cap(self) -> None:
        self.pruner.calibrate(high=100, low=10, hard=250)
        for index in range(200):
            self.make_entity(f"冷知识-{index:03d}", w=0.2, days_old=30)
        self.pruner.round_tick()
        compressed = self.run_async(self.pruner.compress())
        self.assertEqual(len(compressed["compressed"]), 200)
        self.assertEqual(self.graph.count(), 200)
        self.pruner.round_tick()
        pruned = self.run_async(self.pruner.prune(dry_run=False))
        expected_limit = 16  # ceil(200 * 0.08)
        self.assertEqual(pruned["report"]["limit"], expected_limit)
        self.assertEqual(len(pruned["archived"]), expected_limit)
        self.assertEqual(len(pruned["archived"]) + len(pruned["deleted"]), expected_limit)
        self.assertEqual(self.graph.count(), 184)
        # dry_run 只报告，不改变图
        dry = self.run_async(self.pruner.prune(dry_run=True))
        self.assertEqual(dry["archived"], [])
        self.assertEqual(dry["deleted"], [])
        self.assertEqual(self.graph.count(), 184)
        self.assertLessEqual(len(dry["report"]["would_archive"]), 15)  # ceil(184*0.08)


# --------------------------------------------------------------------------- #
# E5：pinned 90%，连续 3 轮剪不到 low → 停止 + 告警
# --------------------------------------------------------------------------- #
class E5Test(PrunerTestBase):
    def test_e5_stuck_detection_and_stop(self) -> None:
        self.pruner.calibrate(high=100, low=80, hard=250)
        for index in range(108):
            self.make_entity(
                f"置顶知识-{index}",
                w=0.01,
                days_old=100,
                properties={"pinned": True, "status": STATUS_COMPRESSED, "compressed_at": self.clock() - 60 * 86400},
            )
        for index in range(12):
            self.make_entity(f"可剪知识-{index}", w=0.2, days_old=30)
        self.assertEqual(self.graph.count(), 120)
        rounds = 0
        archived_total = 0
        while rounds < 20:
            rounds += 1
            self.pruner.round_tick()
            _gate, performed = self.maintenance_step()
            if performed and performed[0] == "prune":
                archived_total += len(performed[1]["archived"])
            if self.pruner.state()["stuck"]:
                break
        state = self.pruner.state()
        self.assertTrue(state["stuck"], state)
        self.assertLessEqual(rounds, 8, f"不能无限重试: rounds={rounds}")
        self.assertGreater(self.graph.count(), 80, "pinned 占 90% 时不可能剪到 low")
        stuck_alarms = [alarm for alarm in self.journal.alarms if alarm["kind"] == "prune_stuck"]
        self.assertEqual(len(stuck_alarms), 1, self.journal.alarms)
        self.assertGreaterEqual(stuck_alarms[0]["detail"].get("fail_rounds"), 3)
        final_count = self.graph.count()
        # 停止后不再继续尝试：gate 返回 idle、不再归档
        for _ in range(3):
            self.pruner.round_tick()
            gate, performed = self.maintenance_step()
            self.assertEqual(gate["action"], "idle")
            self.assertIsNone(performed)
        self.assertEqual(self.graph.count(), final_count)
        direct = self.run_async(self.pruner.prune(dry_run=False))
        self.assertEqual(direct["archived"], [])
        self.assertEqual(direct["report"]["reason"], "stuck")
        self.assertEqual(len([a for a in self.journal.alarms if a["kind"] == "prune_stuck"]), 1)


# --------------------------------------------------------------------------- #
# E6：user 超上界、workspace 零剪枝（门控按 scope 独立）
# --------------------------------------------------------------------------- #
class E6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir("gm-prune-e6")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.config = make_config()
        self.config.data["prune"]["mergeSimThreshold"] = 1.1

    def _build(self, scope: str, home: str) -> tuple[Pruner, GraphStore, FakeJournal]:
        os.makedirs(home, exist_ok=True)
        graph = GraphStore(home, scope, clock=self.clock)
        journal = FakeJournal()
        pruner = Pruner(home, scope, self.config, graph, journal, FakeLlm(), clock=self.clock)
        return pruner, graph, journal

    def test_e6_user_over_limit_workspace_untouched(self) -> None:
        user_home = os.path.join(self.tmp, "user")
        ws_home = os.path.join(self.tmp, "workspace")
        user_pruner, user_graph, user_journal = self._build("user", user_home)
        ws_pruner, ws_graph, ws_journal = self._build("workspace", ws_home)
        user_pruner.calibrate(high=100, low=90, hard=250)
        ws_pruner.calibrate(high=100, low=80, hard=250)
        assert user_pruner.watermarks()["high"] == 100
        assert ws_pruner.watermarks()["high"] == 100
        # user：110 冷节点 > high=100 → 触发压缩+剪枝（窗口 20%，8%/轮可在 3 轮内到 low）
        now = self.clock()
        for index in range(110):
            user_graph.upsert_entity(
                f"用户冷知识-{index}",
                "Concept",
                f"用户冷知识-{index}：独立的用户偏好记录，编号 {index}。",
                "user",
                properties={
                    "w": 0.2,
                    "created_at": now - 30 * 86400,
                    "updated_at": now - 30 * 86400,
                    "last_activated_at": now - 30 * 86400,
                },
            )
        user_actions = []
        for _ in range(20):
            user_pruner.round_tick()
            gate = user_pruner.gate()
            phase = user_pruner.state()["phase"]
            if gate["action"] in ("compress", "force") and phase != "compressed":
                user_actions.append("compress")
                asyncio.run(user_pruner.compress())
            elif gate["action"] in ("prune", "force") and phase == "compressed":
                user_actions.append("prune")
                asyncio.run(user_pruner.prune(dry_run=False))
            if user_graph.count() <= 90:
                break
        self.assertIn("compress", user_actions)
        self.assertIn("prune", user_actions)
        self.assertLessEqual(user_graph.count(), 90)
        self.assertGreater(user_graph.count(include_archived=True) - user_graph.count(), 0)
        # workspace：60 冷节点 < high=100 → 10 轮 maintenance 全部 idle，零剪枝
        for index in range(60):
            ws_graph.upsert_entity(
                f"项目冷知识-{index}",
                "Concept",
                f"项目冷知识-{index}：独立的项目知识记录，编号 {index}。",
                "workspace",
                properties={
                    "w": 0.2,
                    "created_at": now - 30 * 86400,
                    "updated_at": now - 30 * 86400,
                    "last_activated_at": now - 30 * 86400,
                },
            )
        for _ in range(10):
            ws_pruner.round_tick()
            gate = ws_pruner.gate()
            self.assertEqual(gate["action"], "idle", gate)
        self.assertEqual(ws_graph.count(), 60)
        self.assertEqual(ws_graph.count(include_archived=True), 60)
        self.assertEqual(ws_journal.alarms, [])
        self.assertEqual(ws_pruner.state()["phase"], "idle")
        # user 的维护轮次不影响 workspace 的 round（各自持久化）
        self.assertEqual(user_pruner.state()["round"] > 0, True)
        self.assertEqual(ws_pruner.state()["round"], 10)


# --------------------------------------------------------------------------- #
# 其它状态机语义
# --------------------------------------------------------------------------- #
class PruneSemanticsTest(PrunerTestBase):
    def test_dry_run_reports_only_without_graph_change(self) -> None:
        for index in range(60):
            self.make_entity(f"冷知识-{index}", w=0.2, days_old=30)
        self.pruner.calibrate(high=50, low=40, hard=125)
        self.pruner.round_tick()
        self.run_async(self.pruner.compress())
        self.assertEqual(self.graph.count(), 60)
        self.pruner.round_tick()
        before = {
            node["id"]: dict((node.get("properties") or {}))
            for node in self.graph.list_entities(self.scope, include_archived=True)
        }
        dry = self.run_async(self.pruner.prune(dry_run=True))
        self.assertEqual(dry["archived"], [])
        self.assertEqual(dry["deleted"], [])
        self.assertTrue(dry["report"]["would_archive"], dry["report"])
        after = {
            node["id"]: dict((node.get("properties") or {}))
            for node in self.graph.list_entities(self.scope, include_archived=True)
        }
        self.assertEqual(before, after)
        self.assertEqual(self.journal.changes, [])
        self.assertEqual(self.pruner.state()["consecutive_fail_rounds"], 0)

    def test_cold_archived_deleted_after_30_days_without_activation(self) -> None:
        self.pruner.calibrate(high=1000, low=800, hard=2500)
        now = self.clock()
        for index in range(10):
            self.make_entity(f"活跃节点-{index}", w=1.0, days_old=0)
        deletable = self.make_entity(
            "冷区节点可删",
            w=0.01,
            days_old=40,
            properties={
                "status": STATUS_ARCHIVED,
                "archived_at": now - 31 * 86400,
                "last_activated_at": now - 40 * 86400,
            },
        )
        hit_after_archive = self.make_entity(
            "冷区节点曾命中",
            w=0.01,
            days_old=40,
            properties={
                "status": STATUS_ARCHIVED,
                "archived_at": now - 31 * 86400,
                "last_activated_at": now - 10 * 86400,
            },
        )
        fresh_archive = self.make_entity(
            "冷区节点刚归档",
            w=0.01,
            days_old=40,
            properties={
                "status": STATUS_ARCHIVED,
                "archived_at": now - 5 * 86400,
                "last_activated_at": now - 40 * 86400,
            },
        )
        self.pruner.round_tick()
        result = self.run_async(self.pruner.prune(dry_run=False))
        self.assertIn(deletable, result["deleted"])
        self.assertNotIn(hit_after_archive, result["deleted"])
        self.assertNotIn(fresh_archive, result["deleted"])
        self.assertIsNone(self.graph.get_entity(deletable))
        self.assertEqual(self.status_of(hit_after_archive), STATUS_ARCHIVED)
        self.assertEqual(self.status_of(fresh_archive), STATUS_ARCHIVED)
        delete_changes = [change for change in self.journal.changes if change["kind"] == "prune_delete"]
        self.assertEqual(len(delete_changes), 1)

    def test_watermarks_calibration_and_persistence(self) -> None:
        default = self.pruner.watermarks()
        self.assertEqual(default, {"high": 20000, "low": 16000, "hard": 50000})
        calibrated = self.pruner.calibrate(high=100)
        self.assertEqual(calibrated["watermarks"], {"high": 100, "low": 80, "hard": 250})
        # maintenance.json 持久化 round / compress_round / calibration；`calibrate(high)`
        # 优先级高于 config
        self.pruner.round_tick()
        self.pruner.round_tick()
        reopened = Pruner(
            self.tmp, self.scope, self.config, self.graph, FakeJournal(), FakeLlm(), clock=self.clock
        )
        self.assertEqual(reopened.state()["round"], 2)
        self.assertEqual(reopened.watermarks(), {"high": 100, "low": 80, "hard": 250})
        # 直接写 maintenance.json 的 calibration 也能覆盖 config（优先级：maintenance > config）
        with open(os.path.join(self.tmp, "maintenance.json"), "w", encoding="utf-8") as fh:
            json.dump({"calibration": {"high": 123, "low": 100, "hard": 300}, "round": 7}, fh)
        reopened2 = Pruner(
            self.tmp, self.scope, self.config, self.graph, FakeJournal(), FakeLlm(), clock=self.clock
        )
        self.assertEqual(reopened2.watermarks(), {"high": 123, "low": 100, "hard": 300})
        self.assertEqual(reopened2.state()["round"], 7)
        # user scope 默认水位线来自 config
        user_home = os.path.join(self.tmp, "user-home")
        user_graph = GraphStore(user_home, "user", clock=self.clock)
        user_pruner = Pruner(user_home, "user", self.config, user_graph, FakeJournal(), FakeLlm(), clock=self.clock)
        self.assertEqual(user_pruner.watermarks(), {"high": 5000, "low": 4000, "hard": 12000})

    def test_gate_force_when_over_hard(self) -> None:
        self.pruner.calibrate(high=50, low=40, hard=60)
        for index in range(61):
            self.make_entity(f"节点-{index}", w=1.0, days_old=0)
        self.pruner.round_tick()
        gate = self.pruner.gate()
        self.assertEqual(gate["action"], "force")
        self.assertEqual(gate["count"], 61)
        self.assertGreater(gate["count"], gate["hard"])

    def test_round_tick_and_compress_round_persist(self) -> None:
        self.assertEqual(self.pruner.state()["round"], 0)
        self.assertEqual(self.pruner.round_tick(), 1)
        self.pruner.calibrate(high=100, low=80, hard=250)
        for index in range(110):
            self.make_entity(f"冷知识-{index}", w=0.2, days_old=10)
        self.assertEqual(self.pruner.gate()["action"], "compress")
        self.run_async(self.pruner.compress())
        self.assertEqual(self.pruner.state()["compress_round"], 1)
        self.assertEqual(self.pruner.state()["phase"], "compressed")
        reopened = Pruner(
            self.tmp, self.scope, self.config, self.graph, FakeJournal(), FakeLlm(), clock=self.clock
        )
        self.assertEqual(reopened.state()["round"], 1)
        self.assertEqual(reopened.state()["compress_round"], 1)
        self.assertEqual(reopened.state()["phase"], "compressed")
        # 同轮 prune 仍拒绝归档（R4，重启后依然生效）
        same_round = self.run_async(reopened.prune(dry_run=False))
        self.assertEqual(same_round["archived"], [])
        self.assertEqual(same_round["report"]["reason"], "same_round_compress")

    def test_on_retrieval_p95_adaptive_shrink_and_grow(self) -> None:
        self.pruner.calibrate(high=10000)
        self.assertEqual(self.pruner.watermarks()["high"], 10000)
        # 连续 3 天 p95>150ms 且 count<high → high*=0.8
        actions = []
        for day in range(3):
            self.clock.set(BASE_TS + day * 86400)
            result = self.pruner.on_retrieval_p95(200.0, 5000)
            actions.append(result["action"])
        self.assertEqual(actions[0], None)
        self.assertEqual(actions[2], "shrink")
        self.assertEqual(self.pruner.watermarks()["high"], 8000)
        # 同日再次调用不会重复调整
        self.clock.set(BASE_TS + 2 * 86400)
        same_day = self.pruner.on_retrieval_p95(200.0, 5000)
        self.assertIsNone(same_day["action"])
        # 连续 3 天 p95<50ms 且 count>0.8*high → high*=1.2
        grow_actions = []
        for day in range(3, 6):
            self.clock.set(BASE_TS + day * 86400)
            result = self.pruner.on_retrieval_p95(10.0, 7000)
            grow_actions.append(result["action"])
        self.assertEqual(grow_actions[2], "grow")
        self.assertEqual(self.pruner.watermarks()["high"], 9600)
        self.assertGreaterEqual(len(self.pruner.state()["p95_daily"]), 1)
        reopened = Pruner(
            self.tmp, self.scope, self.config, self.graph, FakeJournal(), FakeLlm(), clock=self.clock
        )
        self.assertEqual(reopened.watermarks()["high"], 9600)

    def test_hysteresis_continues_after_dropping_below_high(self) -> None:
        self.pruner.calibrate(high=100, low=80, hard=250)
        for index in range(25):
            self.make_entity(f"冷知识-{index}", w=0.2, days_old=30)
        for index in range(76):
            self.make_entity(f"活跃知识-{index}", w=1.0, days_old=0)
        self.pruner.round_tick()
        self.assertEqual(self.pruner.gate()["action"], "compress")
        self.run_async(self.pruner.compress())
        self.pruner.round_tick()
        # 第一次 prune 后降到 high 以下但 >low：门控仍应继续 prune（hysteresis），
        # 直到 count<=low 才停。
        first = self.run_async(self.pruner.prune(dry_run=False))
        self.assertGreater(len(first["archived"]), 0)
        count_in_band = self.graph.count()
        self.assertGreater(count_in_band, 80)
        self.assertLessEqual(count_in_band, 100)
        gate = self.pruner.gate()
        self.assertEqual(gate["action"], "prune")
        self.assertTrue(gate["hysteresis"])
        second = self.run_async(self.pruner.prune(dry_run=False))
        self.assertLessEqual(self.graph.count(), count_in_band)
        self.assertGreaterEqual(len(second["report"]["would_archive"]) + len(second["archived"]), 0)


class ServiceMaintenancePruneReportTest(unittest.TestCase):
    """集成补充（可选）：service.maintenance 自动 compress/prune 路径也要写 journal.prune_reports。

    service.py 尚不可 import（他人并行开发中）时自动 skip，不阻塞本模块单测。
    """

    def test_service_maintenance_auto_path_logs_prune_reports(self) -> None:
        try:
            from service import MemoryService  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"service.py 尚不可用: {exc}")
        tmp = make_tmp_dir("gm-prune-svc")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        config_data = json.loads(json.dumps(DEFAULT_CONFIG_DATA))
        config_data["paths"]["userDataHome"] = os.path.join(tmp, "user-home")
        config_data["paths"]["workspaceDataHome"] = os.path.join(tmp, "workspace-home")
        config_data["llm"]["mode"] = "offline"
        config_data["panel"]["enabled"] = False
        config_data["prune"]["mergeSimThreshold"] = 1.1
        config = Config(config_data, base_dir=ROOT)

        async def _scenario():
            service = MemoryService(config)
            try:
                scope = "workspace"
                graph = service.graphs[scope]
                for index in range(21):
                    graph.upsert_entity(
                        f"集成冷知识-{index}",
                        "Concept",
                        f"集成冷知识-{index}：独立条目 {index}，用于 maintenance 自动路径。",
                        scope,
                        properties={"w": 0.2},
                    )
                # 老化：越过"新建 3 天 / 近 7 天 updated"保护，才允许 compress
                service.clock.advance(10 * 86400)
                service.pruners[scope].calibrate(high=20, low=16, hard=50)
                actions: list[str] = []
                for _ in range(20):
                    result = await service.maintenance({"scope": scope})
                    actions.extend(action.get("action") for action in (result.get("actions") or []))
                    if "compress" in actions and "prune" in actions and graph.count() <= 16:
                        break
                reports = service.journals[scope].prune_history(scope)
                return actions, {report.get("kind") for report in reports}, graph.count()
            finally:
                await service.close()

        actions, kinds, nodes = asyncio.run(_scenario())
        self.assertIn("compress", actions)
        self.assertIn("prune", actions)
        self.assertIn("compress", kinds, "maintenance 自动 compress 未写 prune_reports")
        self.assertIn("prune", kinds, "maintenance 自动 prune 未写 prune_reports")
        self.assertLessEqual(nodes, 16)


if __name__ == "__main__":
    unittest.main()
