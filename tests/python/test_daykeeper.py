"""T3 单测：日缓冲 / 全量压缩 / 恒定 day_pack / 历史滚动摘要（契约 §2.5，R8）。

覆盖：B 相邻的 day 缓冲行为、D2 场景（30 天 × 50 轮：历史摘要始终单份 + day_pack tokens ≤1024）、
压缩限流、全量压缩（含已有摘要）、最近原文保底、原文不删、LLM 失败降级、detail 补召回、
历史摘要单份与上限、budget 三档上限。
"""
from __future__ import annotations

import asyncio
import glob
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

from base import (  # noqa: E402
    Config,
    estimate_tokens,
    read_json,
    read_jsonl,
    today_str,
    write_json_atomic,
)
from daykeeper import DayKeeper  # noqa: E402
from graph import GraphStore  # noqa: E402

with open(os.path.join(ROOT, "config", "default.json"), "r", encoding="utf-8-sig") as _fh:
    DEFAULT_CONFIG_DATA = json.load(_fh)

BASE_TS = time.mktime(time.strptime("2026-01-05 10:00:00", "%Y-%m-%d %H:%M:%S"))
REPO_TMP = os.path.join(ROOT, ".tmp")


def make_tmp_dir(prefix: str) -> str:
    """测试夹具目录必须放在仓库 `.tmp/`（已 gitignore）：本机 Windows 沙箱下系统临时目录
    创建的子目录 ACL 受限，`tempfile.mkdtemp()` 里的原子写会挂起/权限失败。"""
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


class FakeLlm:
    """可脚本化异步 LLM；response 可以是字符串或 callable(prompt, purpose) -> str。"""

    def __init__(self, response=None, *, fail: bool = False, mode: str = "", sleep: float = 0.0) -> None:
        self.response = response
        self.fail = fail
        self.mode = mode
        self.sleep = sleep
        self.calls: list[dict] = []

    async def complete(self, prompt, *, system=None, max_tokens=1024, purpose=""):
        self.calls.append(
            {"prompt": prompt, "purpose": purpose, "max_tokens": max_tokens, "system": system}
        )
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.fail:
            raise RuntimeError("scripted llm failure")
        if callable(self.response):
            return self.response(prompt, purpose)
        if self.response is None:
            return ""
        return self.response


class DayKeeperTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir("gm-day")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.config = make_config()
        self.graph = GraphStore(self.tmp, "workspace", clock=self.clock)
        self.keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, None, clock=self.clock)

    # ------------------------------------------------------------------ #
    def run_async(self, coro):
        return asyncio.run(coro)

    def date_path(self, day_ts: float | None = None) -> str:
        ts = self.clock() if day_ts is None else day_ts
        return today_str(ts)

    def turns_path(self, day_ts: float | None = None) -> str:
        return os.path.join(self.tmp, "days", self.date_path(day_ts), "turns.jsonl")

    def day_summary(self) -> dict:
        return read_json(os.path.join(self.tmp, "days", "day_summary.json"), {})

    def history_files(self) -> list[str]:
        return glob.glob(os.path.join(self.tmp, "days", "history_summary*"))

    def history_record(self) -> dict:
        return read_json(os.path.join(self.tmp, "days", "history_summary.json"), {})

    def append(self, index: int, text: str | None = None, *, turn_id: str | None = None, tag: str = "") -> dict:
        text = text if text is not None else self.turn_text(tag, index)
        return self.run_async(self.keeper.append_turn(text, turn_id or f"turn-{tag}-{index}", self.clock()))

    @staticmethod
    def turn_text(tag: str, index: int) -> str:
        return f"{tag}{index:02d}轮：记录项目进度与决策，使用 pnpm 管理依赖，继续推进验收测试。"

    @staticmethod
    def long_text(tag: str, index: int) -> str:
        return (
            f"{tag}{index:02d}轮：" + "用户讨论项目架构与依赖管理方案，" * 3 + "最终决定继续使用 pnpm。"
        )

    def assert_budget(self, pack: dict) -> None:
        tokens = pack["tokens"]
        self.assertLessEqual(tokens["day"], 512, f"day token overflow: {tokens}")
        self.assertLessEqual(tokens["detail"], 312, f"detail token overflow: {tokens}")
        self.assertLessEqual(tokens["history"], 200, f"history token overflow: {tokens}")
        self.assertLessEqual(tokens["total"], 1024, f"total token overflow: {tokens}")
        self.assertEqual(tokens["day"], estimate_tokens(pack["day_text"]))
        self.assertEqual(tokens["detail"], estimate_tokens(pack["detail_text"]))
        self.assertEqual(tokens["history"], estimate_tokens(pack["history_text"]))
        self.assertEqual(tokens["total"], tokens["day"] + tokens["detail"] + tokens["history"])


class DayKeeperBufferTest(DayKeeperTestBase):
    def test_append_turn_persists_turns_jsonl_and_day_state(self) -> None:
        for index in range(3):
            self.append(index)
        rows = read_jsonl(self.turns_path())
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["turn_id"], "turn--0")
        self.assertIn("text", rows[0])
        self.assertEqual(rows[0]["date"], self.date_path())
        state = read_json(os.path.join(self.tmp, "day_state.json"), {})
        self.assertEqual(state["turns"], 3)
        self.assertEqual(state["date"], self.date_path())
        # 同 turn_id 重放幂等
        self.append(2)
        self.assertEqual(len(read_jsonl(self.turns_path())), 3)
        state = read_json(os.path.join(self.tmp, "day_state.json"), {})
        self.assertEqual(state["turns"], 3)

    def test_day_pack_under_limit_returns_raw_text(self) -> None:
        texts = [f"短对话{index}：我们在推进项目。" for index in range(5)]
        for index, text in enumerate(texts):
            self.append(index, text)
        pack = self.run_async(self.keeper.day_pack("项目推进"))
        self.assertFalse(pack["compressed"])
        for text in texts:
            self.assertIn(text, pack["day_text"])
        self.assertEqual(pack["history_text"], "")
        self.assert_budget(pack)

    def test_state_exposes_day_and_history_tokens(self) -> None:
        self.append(0)
        state = self.keeper.state()
        self.assertIn("day_tokens", state)
        self.assertIn("history_tokens", state)
        self.assertGreaterEqual(state["day_tokens"], 1)
        self.assertEqual(state["turns"], 1)
        self.assertEqual(state["history_tokens"], 0)


class DayKeeperCompressTest(DayKeeperTestBase):
    def test_full_compression_triggered_over_raw_limit(self) -> None:
        for index in range(25):
            self.append(index)
        raw_rows_before = read_jsonl(self.turns_path())
        self.assertGreater(estimate_tokens("\n".join(r["text"] for r in raw_rows_before)), 512)
        result = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(result["compressed"], result)
        self.assertLessEqual(result["summary_tokens"], 360)
        summary = self.day_summary()
        self.assertEqual(summary["date"], self.date_path())
        self.assertTrue(summary["text"].strip())
        self.assertLessEqual(estimate_tokens(summary["text"]), 360)
        # 原文不删
        self.assertEqual(len(read_jsonl(self.turns_path())), 25)
        pack = self.run_async(self.keeper.day_pack("依赖管理"))
        self.assertTrue(pack["compressed"])
        # 最近 2 条原文保底
        last_two = [row["text"] for row in raw_rows_before[-2:]]
        for text in last_two:
            self.assertIn(text, pack["day_text"])
        self.assert_budget(pack)

    def test_compress_calls_llm_with_all_raw_text_and_previous_summary(self) -> None:
        llm = FakeLlm("当日摘要：项目继续使用 pnpm 管理依赖，验收测试推进中。")
        self.keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, llm, clock=self.clock)
        for index in range(25):
            self.append(index)
            self.clock.advance(60)
        first = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(first["compressed"], first)
        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertEqual(call["purpose"], "summarize")
        self.assertEqual(call["max_tokens"], 360)
        self.assertIn(self.turn_text("", 0), call["prompt"])
        self.assertIn(self.turn_text("", 24), call["prompt"])
        # 第二轮全量压缩：仍包含当天全部原文 + 已有当日摘要，且不是增量
        for index in range(25, 45):
            self.append(index)
            self.clock.advance(120)
        second = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(second["compressed"], second)
        self.assertEqual(len(llm.calls), 2)
        prompt = llm.calls[1]["prompt"]
        self.assertIn("已有当日摘要", prompt)
        self.assertIn("当日摘要：项目继续使用 pnpm", prompt)
        self.assertIn(self.turn_text("", 0), prompt)
        self.assertIn(self.turn_text("", 44), prompt)

    def test_compress_throttled_by_interval_and_turns(self) -> None:
        for index in range(25):
            self.append(index)
            self.clock.advance(1)
        first = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(first["compressed"], first)
        # 只追加 20 轮、间隔仅 20s → 距上次压缩 <30min → 跳过
        for index in range(25, 45):
            self.append(index)
            self.clock.advance(1)
        skipped = self.run_async(self.keeper.maybe_compress())
        self.assertFalse(skipped["compressed"])
        self.assertEqual(skipped["reason"], "throttled_interval")
        # 时间推进 30 分钟后允许再次压缩
        self.clock.advance(1800)
        again = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(again["compressed"], again)
        # 轮数不足 → 跳过
        fresh_home = os.path.join(self.tmp, "fresh")
        os.makedirs(fresh_home, exist_ok=True)
        fresh_clock = FakeClock(BASE_TS)
        fresh_graph = GraphStore(fresh_home, "workspace", clock=fresh_clock)
        keeper = DayKeeper(fresh_home, "workspace", self.config, fresh_graph, None, clock=fresh_clock)
        for index in range(10):
            asyncio.run(keeper.append_turn(self.long_text("L", index), f"L{index}", fresh_clock()))
            fresh_clock.advance(1)
        self.assertGreater(
            estimate_tokens("\n".join(self.long_text("L", i) for i in range(10))), 512
        )
        result = asyncio.run(keeper.maybe_compress())
        self.assertFalse(result["compressed"])
        self.assertEqual(result["reason"], "throttled_turns")

    def test_llm_failure_degrades_to_deterministic_summary(self) -> None:
        llm = FakeLlm(fail=True)
        self.keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, llm, clock=self.clock)
        for index in range(25):
            self.append(index)
        result = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(result["compressed"], result)
        self.assertGreater(result["summary_tokens"], 0)
        self.assertLessEqual(result["summary_tokens"], 360)
        self.assertTrue(self.day_summary()["text"].strip())


class DayKeeperHistoryTest(DayKeeperTestBase):
    def _compress_day(self, tag: str, turns: int = 25) -> None:
        for index in range(turns):
            self.append(index, self.turn_text(f"{tag}-", index))
            self.clock.advance(60)
        result = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(result["compressed"], result)

    def test_rollover_keeps_single_history_file_and_clears_day(self) -> None:
        self._compress_day("DAY1")
        day1 = self.date_path()
        self.assertEqual(self.history_files(), [])
        next_day_clock = self.clock()
        self.clock.advance(86400)
        rolled = self.run_async(self.keeper.rollover_if_needed())
        self.assertTrue(rolled["rolled"], rolled)
        self.assertLessEqual(rolled["history_tokens"], 200)
        files = self.history_files()
        self.assertEqual(len(files), 1, files)
        record = self.history_record()
        self.assertLessEqual(estimate_tokens(record["text"]), 200)
        self.assertEqual(record["days"], [day1])
        # 当天摘要清空重来（单份，滚动覆盖）
        summary = self.day_summary()
        self.assertEqual(summary["date"], self.date_path())
        self.assertEqual(summary["text"], "")
        # 原文不删
        self.assertEqual(len(read_jsonl(self.turns_path(next_day_clock))), 25)
        # 同一天再次调用不重复合并
        again = self.run_async(self.keeper.rollover_if_needed())
        self.assertFalse(again["rolled"])
        self.assertEqual(len(self.history_files()), 1)

        # 连续 3 天：history 始终只有一份，≤200 token，且保留最近一天信息
        for _day in range(2):
            self._compress_day(f"DAY{_day + 2}")
            day_ts = self.clock()
            self.assertEqual(len(read_jsonl(self.turns_path(day_ts))), 25)  # 原文不删
            self.clock.advance(86400)
            rolled = self.run_async(self.keeper.rollover_if_needed())
            self.assertTrue(rolled["rolled"], rolled)
            self.assertEqual(len(self.history_files()), 1)
            self.assertLessEqual(estimate_tokens(self.history_record()["text"]), 200)
        history_text = self.history_record()["text"]
        self.assertIn("DAY3", history_text)

    def test_day_pack_after_rollover_bounded_and_detail_recall(self) -> None:
        # day1：压缩并合并进历史
        self._compress_day("DAY1")
        self.clock.advance(86400)
        self.run_async(self.keeper.rollover_if_needed())
        history_tokens = estimate_tokens(self.history_record()["text"])
        self.assertGreater(history_tokens, 0)
        # day2：一条可补召回的关键细节 + 大量填充轮
        llm = FakeLlm("当日摘要：讨论项目进度与依赖管理。")
        self.keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, llm, clock=self.clock)
        detail_text = "关于部署流程：使用 Docker 部署到测试环境，镜像由 CI 构建。"
        self.append(0, detail_text, turn_id="detail-turn")
        date = self.date_path()
        self.graph.add_chunk(detail_text, "workspace", "detail-turn", date, kind="turn")
        for index in range(1, 25):
            self.append(index, self.turn_text("FILL-", index))
            self.clock.advance(60)
        result = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(result["compressed"], result)
        pack = self.run_async(self.keeper.day_pack("Docker 部署流程 镜像 CI"))
        self.assertNotIn("Docker", pack["day_text"])
        self.assertIn("Docker", pack["detail_text"])
        self.assertGreater(pack["tokens"]["history"], 0)
        self.assert_budget(pack)

    def test_detail_fallback_keyword_match_without_chunks(self) -> None:
        self.append(0, "排查内存泄漏：使用 tracemalloc 定位问题，最终修复缓存未释放。", turn_id="mem-turn")
        for index in range(1, 25):
            self.append(index, self.turn_text("FILL-", index))
            self.clock.advance(60)
        llm = FakeLlm("当日摘要：讨论项目进度。")
        self.keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, llm, clock=self.clock)
        result = self.run_async(self.keeper.maybe_compress())
        self.assertTrue(result["compressed"], result)
        pack = self.run_async(self.keeper.day_pack("内存泄漏 tracemalloc 缓存"))
        self.assertIn("tracemalloc", pack["detail_text"])
        self.assert_budget(pack)

    def test_day_pack_caps_huge_history_and_raw_text(self) -> None:
        days_dir = os.path.join(self.tmp, "days")
        os.makedirs(days_dir, exist_ok=True)
        huge_history = "历史摘要：" + "长期稳定的项目偏好与决策记录，" * 120
        write_json_atomic(
            os.path.join(days_dir, "history_summary.json"),
            {"version": 1, "scope": "workspace", "text": huge_history, "tokens": estimate_tokens(huge_history)},
        )
        for index in range(30):
            self.append(index, "超长原文" + "架构与依赖管理讨论，" * 6)
        pack = self.run_async(self.keeper.day_pack("架构"))
        self.assertLessEqual(estimate_tokens(pack["history_text"]), 200)
        self.assertLessEqual(estimate_tokens(pack["day_text"]), 512)
        self.assert_budget(pack)


class DayKeeperD2Test(DayKeeperTestBase):
    """D2：30 天 × 50 轮，历史摘要始终单份，day_pack tokens 恒定 ≤1024。"""

    def test_d2_thirty_days_constant_budget_and_single_history(self) -> None:
        day_starts = [BASE_TS + day * 86400 for day in range(30)]
        totals: list[int] = []
        day_tokens: list[int] = []
        for day, start in enumerate(day_starts):
            self.clock.set(start)
            if day > 0:
                rolled = self.run_async(self.keeper.rollover_if_needed())
                self.assertTrue(rolled["rolled"], rolled)
            for index in range(50):
                text = (
                    f"第{day:02d}天第{index:02d}轮：记录项目进度与决策，使用 pnpm 管理依赖，"
                    "继续推进验收测试并整理结论。"
                )
                self.run_async(self.keeper.append_turn(text, f"d{day}-{index}", self.clock()))
                self.clock.advance(60)
                self.run_async(self.keeper.maybe_compress())
            pack = self.run_async(self.keeper.day_pack("今天讨论了什么项目进度"))
            self.assert_budget(pack)
            totals.append(pack["tokens"]["total"])
            day_tokens.append(pack["tokens"]["day"])
            self.assertEqual(len(self.history_files()), 0 if day == 0 else 1)
            self.assertLessEqual(estimate_tokens(self.day_summary()["text"]), 360)
        # 历史摘要永远只有一份，且 ≤200 token
        self.assertEqual(len(self.history_files()), 1)
        self.assertLessEqual(estimate_tokens(self.history_record()["text"]), 200)
        # 恒定预算：所有天 ≤1024；历史摘要稳定后（第 4 天起）总量差值 <5%
        self.assertTrue(all(total <= 1024 for total in totals), totals)
        steady = totals[3:]
        self.assertLessEqual(max(steady) - min(steady), int(0.05 * max(steady)) + 1, totals)
        self.assertLessEqual(max(day_tokens) - min(day_tokens), int(0.05 * max(day_tokens)) + 1, day_tokens)


class DayKeeperConcurrencyTest(DayKeeperTestBase):
    """service 会用后台 task 并发调用 rollover_if_needed / maybe_compress：必须幂等、串行化。"""

    def test_concurrent_compress_calls_are_serialized(self) -> None:
        llm = FakeLlm("并发压缩摘要：" + "项目进度与依赖管理。" * 30, sleep=0.05)
        keeper = DayKeeper(self.tmp, "workspace", self.config, self.graph, llm, clock=self.clock)
        for index in range(25):
            self.run_async(keeper.append_turn(self.turn_text("", index), f"t{index}", self.clock()))

        async def _parallel():
            return await asyncio.gather(keeper.maybe_compress(), keeper.maybe_compress())

        first, second = asyncio.run(_parallel())
        flags = [bool(first["compressed"]), bool(second["compressed"])]
        self.assertEqual(flags.count(True), 1, (first, second))
        self.assertEqual(len(llm.calls), 1, "并发调用只允许真正压缩一次")
        summary = self.day_summary()
        self.assertEqual(summary["date"], self.date_path())
        self.assertLessEqual(estimate_tokens(summary["text"]), 360)

    def test_concurrent_rollover_merges_history_once(self) -> None:
        for index in range(25):
            self.append(index)
        self.assertTrue(self.run_async(self.keeper.maybe_compress())["compressed"])
        self.clock.advance(86400)

        async def _parallel():
            return await asyncio.gather(
                self.keeper.rollover_if_needed(), self.keeper.rollover_if_needed()
            )

        results = asyncio.run(_parallel())
        rolled = [result for result in results if result["rolled"]]
        self.assertEqual(len(rolled), 1, results)
        self.assertEqual(len(self.history_files()), 1)
        self.assertLessEqual(estimate_tokens(self.history_record()["text"]), 200)
        # 旧一天原文保留，新一天从 0 轮开始
        old_day_ts = self.clock() - 86400
        self.assertEqual(len(read_jsonl(self.turns_path(old_day_ts))), 25)
        self.assertEqual(self.keeper.state()["turns"], 0)

    def test_append_turn_during_rollover_keeps_new_day_turn(self) -> None:
        for index in range(10):
            self.append(index, text=f"旧日短对话{index}")
        self.clock.advance(86400)

        async def _parallel():
            return await asyncio.gather(
                self.keeper.rollover_if_needed(),
                self.keeper.append_turn("新一天的第一条原文", "new-day-1", self.clock()),
            )

        results = asyncio.run(_parallel())
        self.assertEqual(len(self.history_files()), 1)
        new_day_rows = read_jsonl(self.turns_path())
        self.assertEqual(len(new_day_rows), 1)
        self.assertEqual(new_day_rows[0]["turn_id"], "new-day-1")
        rolled = [result for result in results if isinstance(result, dict) and result.get("rolled")]
        self.assertLessEqual(len(rolled), 1)
        # 新一天 rollover 后 state 指向今天
        self.assertEqual(self.keeper.state()["date"], self.date_path())


if __name__ == "__main__":
    unittest.main()
