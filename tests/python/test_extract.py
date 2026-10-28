# -*- coding: utf-8 -*-
"""记忆抽取与离线规则单测（CONTRACT §2.4 / §4）。

覆盖：offline_rules.extract（喜好/改口/决策/工具）、summarize（token 上限）、judge（否定改口
判矛盾且 confidence>=0.6）、LLM 输出健壮解析、LLM 失败降级、llm_bridge offline 模式端到端。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIDECAR = os.path.join(ROOT, "sidecar")
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)

from base import ENTITY_TYPES, Config, estimate_tokens  # noqa: E402
from extract import (  # noqa: E402
    build_extract_prompt,
    extract_memories,
    normalize_items,
    normalize_type,
    offline_rules,
    parse_memories,
)
from llm_bridge import LlmBridge  # noqa: E402


def _offline_bridge() -> LlmBridge:
    return LlmBridge(Config({"llm": {"mode": "offline", "timeoutMs": 1000}}))


class _AsyncLlm:
    """可脚本化的假 LLM：按顺序返回 responses，或抛异常。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, prompt, *, system=None, max_tokens=1024, purpose=""):
        self.calls.append({"prompt": prompt, "purpose": purpose, "system": system})
        value = self.responses.pop(0) if len(self.responses) > 1 else (self.responses[0] if self.responses else "")
        if isinstance(value, Exception):
            raise value
        return value


def _all_types_valid(facts: list[dict]) -> bool:
    return all(fact.get("type") in ENTITY_TYPES for fact in facts)


def _no_metadata(description: str) -> bool:
    lowered = description.lower()
    return "w=" not in lowered and "pin=" not in lowered and "weight=" not in lowered


class OfflineExtractTests(unittest.TestCase):
    def test_preference_coffee(self) -> None:
        facts = offline_rules.extract("我喜欢咖啡", "user")
        self.assertTrue(facts)
        self.assertTrue(_all_types_valid(facts))
        self.assertTrue(
            any(
                fact["type"] == "Preference" and "咖啡" in (fact["name"] + fact["description"])
                for fact in facts
            ),
            msg=facts,
        )

    def test_assignment_price_80(self) -> None:
        facts = offline_rules.extract("价格改成 80", "workspace")
        self.assertTrue(facts)
        self.assertTrue(
            any(
                fact["type"] in {"Decision", "Preference"} and "80" in (fact["name"] + fact["description"])
                for fact in facts
            ),
            msg=facts,
        )

    def test_negated_preference(self) -> None:
        facts = offline_rules.extract("现在不喜欢咖啡了", "user")
        self.assertTrue(facts)
        self.assertTrue(
            any(fact["type"] == "Preference" and "咖啡" in fact["description"] for fact in facts), msg=facts
        )

    def test_decision_and_tool(self) -> None:
        facts = offline_rules.extract("我们以后用 pnpm 管理依赖", "workspace")
        self.assertTrue(facts)
        joined = " ".join(fact["name"] + fact["description"] for fact in facts)
        self.assertIn("pnpm", joined)
        self.assertTrue(_all_types_valid(facts))

    def test_descriptions_have_no_metadata(self) -> None:
        for text in ["我喜欢咖啡", "价格改成 80", "记住我的默认分支是 main", "使用 pnpm 管理依赖"]:
            for fact in offline_rules.extract(text, "workspace"):
                self.assertTrue(_no_metadata(fact["description"]), msg=fact)

    def test_generic_plain_text_returns_empty(self) -> None:
        facts = offline_rules.extract("今天天气不错", "user")
        self.assertEqual(facts, [], "离线规则不得凭空造实体")
        self.assertEqual(offline_rules.extract("", "user"), [])

    def test_summarize_bounded_and_deterministic(self) -> None:
        text = "今天讨论了接口设计、数据库迁移和灰度发布流程。" * 40
        summary = offline_rules.summarize(text, 60)
        self.assertTrue(summary)
        self.assertLessEqual(estimate_tokens(summary), 60)
        self.assertEqual(summary, offline_rules.summarize(text, 60))

    def test_summarize_short_text(self) -> None:
        summary = offline_rules.summarize("只有一个短句。", 360)
        self.assertEqual(summary, "只有一个短句。")

    def test_judge_negation_contradiction(self) -> None:
        verdict = offline_rules.judge("用户喜欢咖啡", "现在不喜欢了")
        self.assertEqual(verdict["is_same_attribute"], True)
        self.assertEqual(verdict["is_contradiction"], True)
        self.assertGreaterEqual(verdict["confidence"], 0.6)
        self.assertLessEqual(verdict["confidence"], 1.0)

    def test_judge_unrelated_not_contradiction(self) -> None:
        verdict = offline_rules.judge("用户喜欢咖啡", "用户今天加班")
        self.assertFalse(verdict["is_contradiction"])

    def test_judge_same_statement_not_contradiction(self) -> None:
        verdict = offline_rules.judge("用户喜欢咖啡", "用户喜欢咖啡")
        self.assertTrue(verdict["is_same_attribute"])
        self.assertFalse(verdict["is_contradiction"])

    def test_judge_parses_prompt_labels(self) -> None:
        prompt = build_extract_prompt("我喜欢咖啡", "user")  # 不相关 prompt，仅验证不崩
        verdict = offline_rules.judge("", "", prompt)
        self.assertEqual(set(verdict), {"is_same_attribute", "is_contradiction", "confidence"})

    def test_judge_numeric_change(self) -> None:
        verdict = offline_rules.judge("端口是 3000", "端口是 8080")
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])

    def test_unwrap_marked_prompt(self) -> None:
        # llm_bridge offline 会把 prompt 传给 offline_rules.extract：必须能从标记里取原文
        facts = offline_rules.extract(build_extract_prompt("我喜欢咖啡", "user"), "user")
        self.assertTrue(any("咖啡" in fact["description"] for fact in facts), msg=facts)


class ParseAndNormalizeTests(unittest.TestCase):
    def test_fenced_json(self) -> None:
        raw = '```json\n[{"name":"咖啡","type":"Preference","description":"用户喜欢咖啡"}]\n```'
        parsed = parse_memories(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "咖啡")

    def test_array_with_prose(self) -> None:
        raw = '好的，抽取结果如下：[{"name":"pnpm","type":"Tool","description":"使用 pnpm"}] 希望有帮助'
        parsed = parse_memories(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0]["name"], "pnpm")

    def test_dict_wrapper(self) -> None:
        raw = '{"memories": [{"name":"咖啡","type":"preference","description":"喜欢咖啡"}]}'
        parsed = parse_memories(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0]["type"], "preference")

    def test_trailing_comma(self) -> None:
        raw = '[{"name":"a","type":"Concept","description":"x"},]'
        parsed = parse_memories(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 1)

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(parse_memories("not json at all"))
        self.assertIsNone(parse_memories(""))
        self.assertIsNone(parse_memories(None))

    def test_empty_array_is_valid(self) -> None:
        self.assertEqual(parse_memories("[]"), [])

    def test_type_normalization(self) -> None:
        self.assertEqual(normalize_type("preference"), "Preference")
        self.assertEqual(normalize_type("决定"), "Decision")
        self.assertEqual(normalize_type("工具"), "Tool")
        self.assertEqual(normalize_type("whatever"), "Other")
        self.assertEqual(normalize_type(None), "Other")

    def test_normalize_items_filters_and_dedupes(self) -> None:
        items = [
            {"name": "咖啡", "type": "preference", "description": "用户喜欢咖啡"},
            {"name": "咖啡", "type": "Preference", "description": "重复"},
            {"name": "", "type": "Concept", "description": "没有名字但有描述"},
            {"description": ""},
        ]
        facts = normalize_items(items)
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0]["name"], "咖啡")
        self.assertEqual(facts[0]["type"], "Preference")
        self.assertEqual(facts[0]["description"], "用户喜欢咖啡")
        self.assertTrue(facts[1]["name"])


class AsyncExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_bridge_extracts_preference(self) -> None:
        facts = await extract_memories("我喜欢咖啡", scope="user", llm=_offline_bridge())
        self.assertTrue(facts)
        self.assertTrue(
            any(fact["type"] == "Preference" and "咖啡" in fact["description"] for fact in facts), msg=facts
        )
        self.assertLessEqual(len(facts), 12)
        self.assertTrue(_all_types_valid(facts))

    async def test_offline_bridge_assignment(self) -> None:
        facts = await extract_memories("价格改成 80", scope="workspace", llm=_offline_bridge())
        self.assertTrue(
            any("80" in (fact["name"] + fact["description"]) for fact in facts), msg=facts
        )

    async def test_llm_markdown_fenced_json(self) -> None:
        llm = _AsyncLlm(['```json\n[{"name":"咖啡","type":"Preference","description":"用户喜欢咖啡"}]\n```'])
        facts = await extract_memories("我喜欢咖啡", scope="user", llm=llm)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["type"], "Preference")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["purpose"], "extract")

    async def test_llm_invalid_json_retries_once_then_offline(self) -> None:
        llm = _AsyncLlm(["完全不是 JSON", "还是不是 JSON"])
        facts = await extract_memories("我喜欢咖啡", scope="user", llm=llm)
        self.assertEqual(len(llm.calls), 2, "非法 JSON 应重试一次")
        self.assertTrue(any(fact["type"] == "Preference" for fact in facts), msg=facts)
        self.assertIn("只输出 JSON 数组", llm.calls[1]["prompt"])

    async def test_llm_exception_falls_back_offline(self) -> None:
        llm = _AsyncLlm([RuntimeError("host llm down")])
        facts = await extract_memories("我喜欢咖啡", scope="user", llm=llm)
        self.assertTrue(facts)
        self.assertTrue(any(fact["type"] == "Preference" for fact in facts), msg=facts)

    async def test_llm_empty_array_falls_back_when_offline_has_facts(self) -> None:
        llm = _AsyncLlm(["[]"])
        facts = await extract_memories("我喜欢咖啡", scope="user", llm=llm)
        self.assertTrue(facts, "LLM 空结果时应用离线规则兜底一次")

    async def test_short_text_skips_llm(self) -> None:
        llm = _AsyncLlm(["[]"])
        facts = await extract_memories("嗯", scope="user", llm=llm)
        self.assertEqual(facts, [])
        self.assertEqual(llm.calls, [])

    async def test_offline_bridge_judge_purpose(self) -> None:
        bridge = _offline_bridge()
        prompt = "旧说法：用户喜欢咖啡\n新说法：现在不喜欢了\n上下文：\n只输出 JSON。"
        import json  # noqa: PLC0415

        raw = await bridge.complete(prompt, purpose="judge")
        verdict = json.loads(raw)
        self.assertTrue(verdict["is_same_attribute"])
        self.assertTrue(verdict["is_contradiction"])
        self.assertGreaterEqual(verdict["confidence"], 0.6)

    async def test_offline_bridge_summarize_purpose(self) -> None:
        bridge = _offline_bridge()
        text = "讨论接口设计。" * 60
        summary = await bridge.complete(text, purpose="summarize", max_tokens=360)
        self.assertTrue(summary)
        self.assertLessEqual(estimate_tokens(summary), 360)


if __name__ == "__main__":
    unittest.main()
