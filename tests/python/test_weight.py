"""weight.py 单测：CONTRACT §2.1 衰减 / 激活 / 打分 / 新建保护。

逐条断言契约条款（7 天半衰期、+0.5*(1+boost)、score 三段乘子、压缩态复活、3 天保护），
并验证 R3：权重/时间只存在于 properties，description 永不被本模块改写。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import base  # noqa: E402
import weight  # noqa: E402

DAY = 86400.0
WEEK = 7 * DAY
T0 = 1_700_000_000.0


def make_node(
    *,
    name: str = "包管理器",
    description: str = "用户偏好使用 pnpm 管理依赖",
    scope: str = "workspace",
    created_at: float = T0,
    **properties: object,
) -> dict:
    node = base.make_entity(name, "Preference", description, scope, created_at=created_at, properties=properties)
    return node


class DecayTests(unittest.TestCase):
    """§2.1 读时惰性衰减。"""

    def test_half_life_is_seven_days(self) -> None:
        self.assertAlmostEqual(weight.decayed(2.0, T0, T0 + WEEK), 1.0, places=9)

    def test_one_day_decay(self) -> None:
        expected = 2.0 * 2 ** (-DAY / WEEK)
        self.assertAlmostEqual(weight.decayed(2.0, T0, T0 + DAY), expected, places=9)

    def test_capped_at_ten(self) -> None:
        self.assertEqual(weight.decayed(20.0, T0, T0), 10.0)
        self.assertEqual(weight.decayed(12.0, T0, T0 + WEEK), 6.0)

    def test_clock_skew_never_inflates(self) -> None:
        # now < last_activated_at（时钟回拨/未来时间戳）不得把权重抬高
        self.assertEqual(weight.decayed(2.0, T0, T0 - WEEK), 2.0)
        self.assertEqual(weight.decayed(11.0, T0, T0 - WEEK), 10.0)

    def test_missing_or_invalid_timestamp_means_no_decay(self) -> None:
        self.assertEqual(weight.decayed(3.0, None, T0 + WEEK), 3.0)  # type: ignore[arg-type]
        self.assertEqual(weight.decayed(3.0, T0, None), 3.0)  # type: ignore[arg-type]

    def test_zero_and_negative_weight(self) -> None:
        self.assertEqual(weight.decayed(0.0, T0, T0 + WEEK), 0.0)
        self.assertEqual(weight.decayed(-1.0, T0, T0 + WEEK), 0.0)

    def test_custom_half_life(self) -> None:
        self.assertAlmostEqual(weight.decayed(1.0, 0.0, 10.0, half_life_seconds=10.0), 0.5, places=9)

    def test_current_w_reads_properties(self) -> None:
        node = make_node(w=2.0, last_activated_at=T0)
        self.assertAlmostEqual(weight.current_w(node, T0 + WEEK), 1.0, places=9)
        # 缺属性时按默认权重 1.0 处理
        bare = {"id": "ent_x", "name": "x", "type": "Concept", "description": "d", "properties": {}}
        self.assertEqual(weight.current_w(bare, T0), 1.0)

    def test_current_w_does_not_mutate_node(self) -> None:
        node = make_node(w=4.0, last_activated_at=T0)
        before = dict(node["properties"])
        weight.current_w(node, T0 + WEEK)
        self.assertEqual(node["properties"], before)


class ActivateTests(unittest.TestCase):
    """§2.1 激活：boost / +0.5*(1+boost) / 封顶 / 复活 / R5 辅助。"""

    def test_plain_activation_adds_half(self) -> None:
        node = make_node(w=1.0, last_activated_at=T0)
        now = T0 + WEEK  # 衰减到 0.5
        props = weight.activate(node, now)
        self.assertAlmostEqual(props["w"], 1.0, places=9)  # 0.5 + 0.5*(1+0)
        self.assertEqual(props["last_activated_at"], now)
        self.assertEqual(props["status"], "active")

    def test_pinned_boost(self) -> None:
        node = make_node(w=1.0, last_activated_at=T0)
        props = weight.activate(node, T0, pinned=True)
        self.assertAlmostEqual(props["w"], 2.0, places=9)  # 1 + 0.5*2
        self.assertTrue(props["pinned"])

    def test_tool_and_workspace_boost(self) -> None:
        node = make_node(w=1.0, last_activated_at=T0)
        props = weight.activate(node, T0, tool_boosted=True, workspace_hit=True)
        self.assertAlmostEqual(props["w"], 2.1, places=9)  # 1 + 0.5*(1+1+0.2)
        self.assertTrue(props["workspace_hit"])

    def test_activation_capped_at_ten(self) -> None:
        node = make_node(w=10.0, last_activated_at=T0)
        props = weight.activate(node, T0, pinned=True, tool_boosted=True)
        self.assertEqual(props["w"], 10.0)

    def test_compressed_revives_active(self) -> None:
        node = make_node(w=2.0, last_activated_at=T0, status="compressed", compressed_at=T0)
        props = weight.activate(node, T0 + DAY)
        self.assertEqual(props["status"], "active")
        # 先衰减（1 天后 1.811）再加 0.5*(1+0)
        expected = 2.0 * 2 ** (-DAY / WEEK) + 0.5
        self.assertAlmostEqual(props["w"], expected, places=9)

    def test_archived_is_not_revived_by_activate(self) -> None:
        node = make_node(w=2.0, last_activated_at=T0, status="archived")
        props = weight.activate(node, T0)
        self.assertEqual(props["status"], "archived")

    def test_returns_new_properties_dict_without_mutating_node(self) -> None:
        node = make_node(w=1.0, last_activated_at=T0)
        original = dict(node["properties"])
        props = weight.activate(node, T0 + DAY, pinned=True)
        self.assertIsNot(props, node["properties"])
        self.assertEqual(node["properties"], original)
        for key in ("w", "last_activated_at", "status", "pinned", "created_at"):
            self.assertIn(key, props)

    def test_r3_description_untouched(self) -> None:
        node = make_node(description="用户偏好使用 pnpm 管理依赖")
        weight.activate(node, T0, pinned=True)
        self.assertEqual(node["description"], "用户偏好使用 pnpm 管理依赖")
        self.assertNotIn("w=", node["description"])
        self.assertNotIn("pin", node["description"])

    def test_boost_for_helper(self) -> None:
        node = make_node(pinned=True)
        self.assertEqual(weight.boost_for(node), 1.0)
        self.assertAlmostEqual(weight.boost_for(node, tool_boosted=True, workspace_hit=True), 2.2)
        self.assertEqual(weight.boost_for(make_node()), 0.0)


class ScoreTests(unittest.TestCase):
    """§2.1 排序：score = sim*(0.3+0.7*min(1,w/5)) → pinned ×1.5 → workspace ×1.2。"""

    def test_low_weight_score(self) -> None:
        node = make_node(w=1.0, last_activated_at=T0)
        # 0.3 + 0.7*0.2 = 0.44
        self.assertAlmostEqual(weight.score(0.5, node, T0, "user"), 0.22, places=9)

    def test_saturation_at_weight_five(self) -> None:
        node = make_node(w=5.0, last_activated_at=T0)
        self.assertAlmostEqual(weight.score(1.0, node, T0, "user"), 1.0, places=9)
        heavy = make_node(w=10.0, last_activated_at=T0)
        self.assertAlmostEqual(weight.score(1.0, heavy, T0, "user"), 1.0, places=9)

    def test_decay_applies_to_score(self) -> None:
        node = make_node(w=4.0, last_activated_at=T0)
        now = T0 + WEEK  # 衰减到 2.0 → factor 0.3+0.7*0.4=0.58
        self.assertAlmostEqual(weight.score(1.0, node, now, "user"), 0.58, places=9)

    def test_pinned_multiplier(self) -> None:
        node = make_node(w=5.0, last_activated_at=T0, pinned=True)
        self.assertAlmostEqual(weight.score(1.0, node, T0, "user"), 1.5, places=9)

    def test_workspace_multiplier(self) -> None:
        node = make_node(w=5.0, last_activated_at=T0)
        self.assertAlmostEqual(weight.score(1.0, node, T0, "workspace"), 1.2, places=9)

    def test_pinned_and_workspace_multiply(self) -> None:
        node = make_node(w=5.0, last_activated_at=T0, pinned=True)
        self.assertAlmostEqual(weight.score(1.0, node, T0, "workspace"), 1.8, places=9)

    def test_zero_similarity_is_zero(self) -> None:
        node = make_node(w=5.0, last_activated_at=T0)
        self.assertEqual(weight.score(0.0, node, T0, "workspace",), 0.0)


class NewProtectionTests(unittest.TestCase):
    """§2.1/§2.6 新建 3 天保护。"""

    def test_protected_within_three_days(self) -> None:
        node = make_node(created_at=T0)
        self.assertTrue(weight.is_new_protected(node, T0 + 2 * DAY))
        self.assertTrue(weight.is_new_protected(node, T0))

    def test_not_protected_after_three_days(self) -> None:
        node = make_node(created_at=T0)
        self.assertFalse(weight.is_new_protected(node, T0 + 3 * DAY))
        self.assertFalse(weight.is_new_protected(node, T0 + 10 * DAY))

    def test_missing_created_at_is_not_protected(self) -> None:
        node = {"id": "ent_x", "name": "x", "type": "Concept", "description": "d", "properties": {}}
        self.assertFalse(weight.is_new_protected(node, T0))

    def test_supersede_protect_until(self) -> None:
        self.assertEqual(weight.supersede_protect_until(T0), T0 + WEEK)


if __name__ == "__main__":
    unittest.main()
