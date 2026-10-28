"""MemoryService 级回滚集成测试（graph-journal，Lead 授权新增）。

覆盖 Lead 修复的“浅拷贝导致 journal before 快照被原地修改”问题：
- ``set_weight`` → ``journal_rollback`` 必须恢复旧 w（而不是新值）；
- ``forget`` → ``journal_rollback`` 恢复 status/w；
- 7 天过期拒绝回滚、未知 change 拒绝；
- 回滚记录/rolled_back 标记；R6 语义由 journal→graph.edit_entity 保证（单测已覆盖）。

只通过 MemoryService 公开异步方法 + 图谱只读读取断言，数据全部落在仓库 ``.tmp/``（不污染真实 ~/.dsh）。
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import base  # noqa: E402
from config import build_config  # noqa: E402
from service import MemoryService  # noqa: E402

DAY = 86400.0
_TMP_ROOT = os.path.join(ROOT, ".tmp", "tests-py")


def make_temp_dir(tag: str) -> str:
    """仓库 .tmp/ 下唯一目录（sandbox 下 tempfile.mkdtemp 的子项不可写）。"""
    os.makedirs(_TMP_ROOT, exist_ok=True)
    path = os.path.join(_TMP_ROOT, f"{tag}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(path, exist_ok=False)
    return path


class ServiceRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = make_temp_dir("gm-svc-rollback")
        self.config = build_config(
            user_home=os.path.join(self.tmpdir, "user"),
            workspace_home=os.path.join(self.tmpdir, "workspace"),
            workspace_root=ROOT,
            raw_override='{"llm":{"mode":"offline"},"panel":{"enabled":false}}',
        )
        self.service = MemoryService(self.config)

    async def asyncTearDown(self) -> None:
        try:
            await self.service.close()
        except Exception:  # noqa: BLE001 — teardown 失败不掩盖用例结果
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    async def _ingest_one(self) -> str:
        result = await self.service.ingest(
            {
                "turn_text": "我喜欢用 pnpm 管理依赖，记住这个偏好，不要再改回 npm",
                "turn_id": "turn_1",
                "scope": "workspace",
            }
        )
        self.assertTrue(result.get("accepted"), result)
        node_ids = result.get("node_ids") or []
        self.assertTrue(node_ids, f"ingest 未产出节点: {result}")
        return str(node_ids[0])

    def _node(self, node_id: str) -> dict:
        node = self.service.graphs["workspace"].get_entity(node_id)
        self.assertIsNotNone(node, f"node 不存在: {node_id}")
        return node

    def _latest_change(self, node_id: str, kind: str | None = None) -> dict:
        entries = self.service.journals["workspace"].list_entries("workspace", node_id)
        for entry in entries:
            if kind is None or entry["kind"] == kind:
                return entry
        self.fail(f"未找到 kind={kind} 的 change：{[e['kind'] for e in entries]}")

    # ------------------------------------------------------------------ #
    # 用例
    # ------------------------------------------------------------------ #
    async def test_set_weight_rollback_restores_previous_weight(self) -> None:
        node_id = await self._ingest_one()
        w_before = float(self._node(node_id)["properties"]["w"])

        updated = await self.service.set_weight({"id": node_id, "w": 3.0})
        self.assertEqual(updated["id"], node_id)
        self.assertAlmostEqual(float(self._node(node_id)["properties"]["w"]), 3.0, places=6)

        change = self._latest_change(node_id, "weight")
        # 关键回归：journal before 快照必须是旧值（未被原地修改）
        self.assertAlmostEqual(float(change["before"]["properties"]["w"]), w_before, places=6)

        rolled = await self.service.journal_rollback({"change_id": change["id"]})
        self.assertEqual(rolled["scope"], "workspace")
        restored = rolled["restored"]
        self.assertEqual(restored["id"], node_id)
        self.assertAlmostEqual(float(restored["before"]["properties"]["w"]), w_before, places=6)

        node_after = self._node(node_id)
        self.assertAlmostEqual(float(node_after["properties"]["w"]), w_before, places=6)
        # 回滚状态与回滚记录
        self.assertTrue(self.service.journals["workspace"].get_change(change["id"])["rolled_back"])
        rollback_records = [
            entry
            for entry in self.service.journals["workspace"].list_entries("workspace", node_id)
            if entry["kind"] == "rollback"
        ]
        self.assertEqual(len(rollback_records), 1)

    async def test_forget_rollback_restores_status_and_weight(self) -> None:
        node_id = await self._ingest_one()
        before = self._node(node_id)["properties"]
        self.assertEqual(before["status"], "active")
        w_before = float(before["w"])

        forgotten = await self.service.forget({"id": node_id, "scope": "workspace"})
        self.assertEqual(forgotten["status"], "pending_forget")
        pending = self._node(node_id)["properties"]
        self.assertEqual(pending["status"], "pending_forget")
        self.assertEqual(float(pending["w"]), 0.0)

        change = self._latest_change(node_id, "forget")
        rolled = await self.service.journal_rollback({"change_id": change["id"]})
        self.assertEqual(rolled["restored"]["id"], node_id)
        restored = self._node(node_id)["properties"]
        self.assertEqual(restored["status"], "active")
        self.assertAlmostEqual(float(restored["w"]), w_before, places=6)

    async def test_rollback_expired_after_seven_days(self) -> None:
        node_id = await self._ingest_one()
        await self.service.set_weight({"id": node_id, "w": 3.0})
        change = self._latest_change(node_id, "weight")
        self.service.clock.advance(7 * DAY + 1)
        with self.assertRaises(base.GraphMemoryError) as ctx:
            await self.service.journal_rollback({"change_id": change["id"]})
        self.assertEqual(ctx.exception.code, "rollback_expired")
        # 未回滚成功：权重保持新值
        self.assertAlmostEqual(float(self._node(node_id)["properties"]["w"]), 3.0, places=6)
        self.assertFalse(self.service.journals["workspace"].get_change(change["id"])["rolled_back"])

    async def test_rollback_unknown_change_rejected(self) -> None:
        await self._ingest_one()
        with self.assertRaises(base.GraphMemoryError) as ctx:
            await self.service.journal_rollback({"change_id": 987654})
        self.assertEqual(ctx.exception.code, "rollback_unavailable")


if __name__ == "__main__":
    unittest.main()
