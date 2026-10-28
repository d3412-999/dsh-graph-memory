# -*- coding: utf-8 -*-
"""可调注入预算（[3000, 2_000_000]，默认 3000）与每条消息成本估算的单测。

覆盖：
- clamp：低于 3000 抬到 3000、高于 2M 压到 2M、非法值回落默认；
- 等比缩放：预算 3000 时各容器与基线一致，预算放大后同比放大；
- 成本估算：按官方单价（元/百万 token）线性折算，缓存命中/未命中两档；
- 持久化：面板写入 budget.json，service 重启后仍然生效；
- service.set_budget：返回值包含缩放后的 caps 与成本，配置同步更新。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import budget  # noqa: E402
from base import Config  # noqa: E402
from config import build_config  # noqa: E402
from service import MemoryService  # noqa: E402

_TMP_ROOT = os.path.join(ROOT, ".tmp", "tests-py")


def make_temp_dir(tag: str) -> str:
    os.makedirs(_TMP_ROOT, exist_ok=True)
    path = os.path.join(_TMP_ROOT, f"{tag}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(path, exist_ok=False)
    return path


def cfg(total=None, **extra):
    injection = {"totalHardTokens": total} if total is not None else {}
    injection.update(extra)
    return Config({"injection": injection, "day": {}})


class ClampTests(unittest.TestCase):
    def test_bounds(self) -> None:
        self.assertEqual(budget.clamp_budget(3000), 3000)
        self.assertEqual(budget.clamp_budget(2_000_000), 2_000_000)
        self.assertEqual(budget.clamp_budget(1), budget.BUDGET_MIN)
        self.assertEqual(budget.clamp_budget(9_999_999), budget.BUDGET_MAX)
        self.assertEqual(budget.clamp_budget("64000"), 64000)
        self.assertEqual(budget.clamp_budget("abc"), budget.BUDGET_DEFAULT)
        self.assertEqual(budget.clamp_budget(None), budget.BUDGET_DEFAULT)

    def test_min_is_3000_and_default_is_3000(self) -> None:
        self.assertEqual(budget.BUDGET_MIN, 3000)
        self.assertEqual(budget.BUDGET_DEFAULT, 3000)
        self.assertEqual(budget.BUDGET_MAX, 2_000_000)


class CapScalingTests(unittest.TestCase):
    def test_identity_at_default_budget(self) -> None:
        caps = budget.injection_caps(cfg(3000))
        for name, base in budget.BASE_CAPS.items():
            self.assertEqual(caps[name], base, name)

    def test_scales_linearly(self) -> None:
        caps = budget.injection_caps(cfg(30000))
        self.assertEqual(caps["memoryMaxTokens"], 2048 * 10)
        self.assertEqual(caps["dayTextMaxTokens"], 512 * 10)
        self.assertEqual(caps["historyMaxTokens"], 200 * 10)
        self.assertEqual(caps["memoryExplicitTokens"], 2048 * 10)

    def test_day_caps_scale_too(self) -> None:
        self.assertEqual(budget.day_caps(cfg(3000))["rawTokenLimit"], 512)
        self.assertEqual(budget.day_caps(cfg(30000))["rawTokenLimit"], 5120)
        self.assertEqual(budget.day_caps(cfg(30000))["summaryMaxTokens"], 3600)

    def test_custom_caps_are_scaled_from_their_own_value(self) -> None:
        caps = budget.injection_caps(cfg(3000, historyMaxTokens=400))
        self.assertEqual(caps["historyMaxTokens"], 400)


class CostTests(unittest.TestCase):
    def test_cost_linear_in_tokens(self) -> None:
        price = dict(budget.DEFAULT_PRICING)
        self.assertAlmostEqual(budget.cost_per_message(3000, price)["cache_miss"], 0.006, places=6)
        self.assertAlmostEqual(budget.cost_per_message(3000, price)["cache_hit"], 0.00012, places=6)
        self.assertAlmostEqual(budget.cost_per_message(2_000_000, price)["cache_miss"], 4.0, places=6)

    def test_cost_uses_configured_price(self) -> None:
        config = Config({"pricing": {"inputCacheMissPerM": 1.0, "inputCacheHitPerM": 0.02}})
        price = budget.pricing(config)
        self.assertEqual(price["inputCacheMissPerM"], 1.0)
        self.assertAlmostEqual(budget.cost_per_message(1_000_000, price)["cache_miss"], 1.0, places=6)


class RuntimeBudgetFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = make_temp_dir("gm-budget-file")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def test_roundtrip(self) -> None:
        self.assertIsNone(budget.load_runtime_budget(self.home))
        budget.save_runtime_budget(self.home, 64000, updated_at=1.0)
        self.assertEqual(budget.load_runtime_budget(self.home), 64000)
        with open(budget.budget_file(self.home), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["maxTokens"], 64000)
        self.assertEqual(payload["min"], 3000)
        self.assertEqual(payload["max"], 2_000_000)

    def test_out_of_range_value_is_clamped_on_save_and_load(self) -> None:
        budget.save_runtime_budget(self.home, 10)
        self.assertEqual(budget.load_runtime_budget(self.home), 3000)
        with open(budget.budget_file(self.home), "w", encoding="utf-8") as fh:
            json.dump({"maxTokens": 5_000_000}, fh)
        self.assertEqual(budget.load_runtime_budget(self.home), 2_000_000)


class ServiceBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = make_temp_dir("gm-budget-svc")
        self.user_home = os.path.join(self.tmpdir, "user")
        self.config = build_config(
            user_home=self.user_home,
            workspace_home=os.path.join(self.tmpdir, "workspace"),
            workspace_root=ROOT,
            raw_override='{"llm":{"mode":"offline"},"panel":{"enabled":false}}',
        )
        self.service = MemoryService(self.config)

    async def asyncTearDown(self) -> None:
        try:
            await self.service.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_default_budget_is_3000(self) -> None:
        report = await self.service.budget_state({})
        self.assertEqual(report["max_tokens"], 3000)
        self.assertEqual(report["caps"]["memoryMaxTokens"], 2048)
        self.assertAlmostEqual(report["cost_per_message"]["cache_miss"], 0.006, places=6)

    async def test_set_budget_updates_caps_and_persists(self) -> None:
        report = await self.service.set_budget({"max_tokens": 128000})
        self.assertEqual(report["max_tokens"], 128000)
        self.assertGreater(report["caps"]["memoryMaxTokens"], 2048)
        self.assertEqual(self.service.caps()["memoryMaxTokens"], report["caps"]["memoryMaxTokens"])
        self.assertTrue(os.path.isfile(budget.budget_file(self.user_home)))

        # 同一配置重建 service：预算从 budget.json 恢复
        await self.service.close()
        reborn = MemoryService(self.config)
        try:
            restored = await reborn.budget_state({})
            self.assertEqual(restored["max_tokens"], 128000)
        finally:
            await reborn.close()

    async def test_set_budget_clamps_out_of_range(self) -> None:
        low = await self.service.set_budget({"max_tokens": 500})
        self.assertEqual(low["max_tokens"], 3000)
        high = await self.service.set_budget({"max_tokens": 10_000_000})
        self.assertEqual(high["max_tokens"], 2_000_000)


if __name__ == "__main__":
    unittest.main()
