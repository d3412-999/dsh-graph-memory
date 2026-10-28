"""跨语言 token 计量一致性 + 注入预算算法校验（Python 侧，允许 import sidecar/base）。

覆盖：
1. `base.estimate_tokens` / `truncate_to_tokens` 与 CONTRACT §2.2 公式逐样本一致；
2. tests/acceptance/injection_model.py 的独立镜像与 base 完全一致（同一公式两实现不得漂移）；
3. TS 侧 `lib/budgeter.js`（scripts/emit-tokens.mjs）与 Python 公式在固定样本上逐字节一致
   —— 这是 CONTRACT §2.2「TS budgeter 与 Python 必须完全一致」的跨语言交叉验证；
4. 预算装配算法：动态 memory 上限 1024/1536/1792/2048、优先级截断、总预算硬顶 3000；
5. config/default.json 默认值与 §2.2 常量一致（默认配置真的被加载，防 BOM 类回归）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIDECAR_DIR = os.path.join(REPO_ROOT, "sidecar")
ACCEPTANCE_DIR = os.path.join(REPO_ROOT, "tests", "acceptance")
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")
TOKEN_SAMPLES = os.path.join(FIXTURES_DIR, "token_samples.json")

for _path in (SIDECAR_DIR, ACCEPTANCE_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import base  # noqa: E402
import injection_model  # noqa: E402


# --------------------------------------------------------------------------- #
# 独立参考实现（不 import 产品代码，仅用于对照）
# --------------------------------------------------------------------------- #
def ref_is_cjk(char: str) -> bool:
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def ref_estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if ref_is_cjk(char))
    return cjk + (len(text) - cjk + 3) // 4


# 固定样本：空串 / 纯 ASCII / 混合 / 纯 CJK / CJK 标点 / emoji / 数字
TOKEN_CASES = [
    ("", 0),
    ("abc", 1),
    ("abcd", 1),
    ("abcde", 2),
    ("你好", 2),
    ("你好world", 4),
    ("价格改成 80", 5),
    ("明天下午三点开会", 8),
    ("pnpm 依赖管理", 6),  # 4 个 CJK + "pnpm "（5 字符 → ceil(5/4)=2）
    ("A" * 400, 100),
    ("sk-abcdefgh12345678", 5),
    ("，。！？", 4),
    ("中文 mixed ASCII 12345", 2 + 19 // 4 + 1),
]


def expected_tokens(text: str) -> int:
    """手工推导期望值（不依赖被测实现）。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for char in text:
        if ref_is_cjk(char):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


class TokenFormulaTest(unittest.TestCase):
    def test_samples_match_contract_formula(self) -> None:
        for text, expected in TOKEN_CASES:
            with self.subTest(text=text[:20]):
                self.assertEqual(expected_tokens(text), expected, f"手工期望值自检失败: {text!r}")
                self.assertEqual(base.estimate_tokens(text), expected, f"base.estimate_tokens 偏差: {text!r}")
                self.assertEqual(
                    injection_model.estimate_tokens(text), expected, f"injection_model 镜像偏差: {text!r}"
                )

    def test_mirror_matches_base_on_random_samples(self) -> None:
        samples = [
            "pnpm",
            "用户偏好 pnpm 管理依赖",
            "2026-10-28 更新了一条记忆",
            "a b c d e f g",
            "Redis/PostgreSQL 双写",
            "接口超时 P95>150ms",
            "😀 emoji 👍 test",
            "中" * 33,
        ]
        for text in samples:
            self.assertEqual(base.estimate_tokens(text), injection_model.estimate_tokens(text), text)

    def test_truncate_to_tokens_contract(self) -> None:
        texts = ["纯中文字符串需要按 token 截断", "mixed English and 中文 text " * 3, "A" * 100, "abcdef"]
        for text in texts:
            for limit in (0, 1, 4, 16, 64):
                truncated = base.truncate_to_tokens(text, limit)
                self.assertLessEqual(base.estimate_tokens(truncated), limit, f"截断超限: {limit}")
                self.assertTrue(text.startswith(truncated), "截断结果必须是原文前缀")
                self.assertEqual(truncated, injection_model.truncate_to_tokens(text, limit), "镜像 truncate 不一致")


class TsBudgeterCrossLanguageTest(unittest.TestCase):
    """CONTRACT §2.2：TS `budgeter.estimateTokens` 与 Python 公式必须逐样本一致。"""

    SCRIPT = os.path.join(REPO_ROOT, "scripts", "emit-tokens.mjs")

    def _node(self) -> str | None:
        node = shutil.which("node")
        if not node:
            return None
        return node

    def test_ts_budgeter_matches_python(self) -> None:
        node = self._node()
        if not node:
            self.skipTest("node 不在 PATH（跳过 TS budgeter 交叉校验）")
        if not os.path.isfile(self.SCRIPT):
            self.skipTest(f"TS 令牌工具未就绪: {self.SCRIPT}")
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        texts = [text for text, _expected in TOKEN_CASES]
        with open(TOKEN_SAMPLES, "w", encoding="utf-8") as fh:
            json.dump({"texts": texts}, fh, ensure_ascii=False)
        completed = subprocess.run(
            [node, self.SCRIPT, "--samples", TOKEN_SAMPLES],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, f"node emit-tokens 失败: {completed.stderr[-2000:]}")
        payload = json.loads(completed.stdout)
        ts_tokens = payload.get("tokens")
        self.assertIsInstance(ts_tokens, list, f"输出格式不符: {payload}")
        self.assertEqual(len(ts_tokens), len(texts), "样本数量不一致")
        for index, text in enumerate(texts):
            with self.subTest(index=index, text=text[:16]):
                self.assertEqual(int(ts_tokens[index]), expected_tokens(text), f"TS budgeter 偏差: {text!r}")
                self.assertEqual(int(ts_tokens[index]), base.estimate_tokens(text), "TS 与 Python 不一致")


class InjectionAssemblyTest(unittest.TestCase):
    LIMITS = injection_model.default_limits()

    def test_dynamic_memory_cap_tiers(self) -> None:
        self.assertEqual(injection_model.dynamic_memory_cap(), 1024)
        self.assertEqual(injection_model.dynamic_memory_cap(top1_sim=0.85), 1536)
        self.assertEqual(injection_model.dynamic_memory_cap(pin_or_high_weight_count=1), 1792)
        self.assertEqual(injection_model.dynamic_memory_cap(explicit=True), 2048)
        # 优先级：explicit 最高档
        self.assertEqual(injection_model.dynamic_memory_cap(top1_sim=0.9, pin_or_high_weight_count=2, explicit=True), 2048)
        self.assertEqual(injection_model.dynamic_memory_cap(top1_sim=0.9, pin_or_high_weight_count=2), 1792)

    def _assemble(self, *, mem_text="", day_text="", detail="", history="", top1=0.0, high_count=0, explicit=False):
        return injection_model.assemble_injection(
            system_text=injection_model.SYSTEM_INSTRUCTIONS,
            memory_text=mem_text,
            day_pack={"day_text": day_text, "detail_text": detail, "history_text": history},
            retrieve={"items": [], "top1_sim": top1, "high_weight_count": high_count},
            explicit=explicit,
            limits=self.LIMITS,
        )

    def test_total_hard_cap_3000(self) -> None:
        huge = "记忆" * 4000
        assembled = self._assemble(mem_text=huge, day_text="当天" * 4000, detail="细节" * 2000, history="历史" * 2000)
        tokens = assembled["tokens"]
        self.assertLessEqual(tokens["total"], 3000, f"注入总量（实际文本 token）超硬顶: {tokens}")
        self.assertLessEqual(tokens["system"], injection_model.SYSTEM_MAX)
        self.assertLessEqual(tokens["day"], injection_model.DAY_TEXT_MAX)
        self.assertLessEqual(tokens["memory"], injection_model.MEMORY_MAX)
        self.assertLessEqual(tokens["detail"], injection_model.DETAIL_MAX)
        self.assertLessEqual(tokens["history"], injection_model.HISTORY_MAX)

    def test_system_truncated_to_128(self) -> None:
        assembled = injection_model.assemble_injection(
            system_text="指令" * 500,
            memory_text="",
            day_pack={"day_text": "有一天的原文", "detail_text": "", "history_text": ""},
            retrieve={},
            limits=self.LIMITS,
        )
        self.assertLessEqual(assembled["tokens"]["system"], 128)
        self.assertTrue(assembled["truncated"]["system"])

    def test_day_priority_kept_when_overflow(self) -> None:
        """当天原文优先级最高：超预算时先牺牲 history → memory → detail，day_text 保底。"""
        assembled = self._assemble(
            mem_text="记忆内容" * 1000,
            day_text="当天原文" * 300,
            detail="补召回" * 300,
            history="历史摘要" * 300,
        )
        tokens = assembled["tokens"]
        self.assertLessEqual(tokens["total"], 3000)
        self.assertGreater(tokens["day"], 0, "当天原文不应被全部截掉")

    def test_empty_injection_zero_tokens(self) -> None:
        assembled = injection_model.assemble_injection(
            system_text="", memory_text="", day_pack={}, retrieve={}, limits=self.LIMITS
        )
        self.assertEqual(assembled["tokens"]["total"], 0)
        self.assertEqual(assembled["text"], "")


class TsInjectionAssemblyTest(unittest.TestCase):
    """CONTRACT §2.2：TS `budgeter.assembleInjection` 与 Python 镜像装配结果逐段一致。

    用 `node scripts/emit-injection.mjs --input tests/fixtures/injection_cases.json` 做跨语言
    预算装配交叉校验；system 常量文本两侧实现不同（各自固定段），只比较其上限与
    memory/day/detail/history 分量。
    """

    SCRIPT = os.path.join(REPO_ROOT, "scripts", "emit-injection.mjs")
    CASES = os.path.join(FIXTURES_DIR, "injection_cases.json")

    def test_python_mirror_matches_ts_assembly(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH")
        if not os.path.isfile(self.SCRIPT) or not os.path.isfile(self.CASES):
            self.skipTest(f"TS injection 工具/fixture 未就绪: {self.SCRIPT}")
        completed = subprocess.run(
            [node, self.SCRIPT, "--input", self.CASES],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, f"node emit-injection 失败: {completed.stderr[-2000:]}")
        payload = json.loads(completed.stdout)
        ts_cases = {case["name"]: case for case in payload.get("cases") or []}
        with open(self.CASES, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for case in raw["cases"]:
            with self.subTest(case=case["name"]):
                ts = ts_cases[case["name"]]
                ts_tokens = ts["tokens"]
                components = sum(int(ts_tokens[key]) for key in ("system", "memory", "day", "detail", "history"))
                # TS 的 tokens.total 口径 = 实际注入文本 token（文本非空时含包装开销；空结果=0）
                self.assertGreaterEqual(int(ts_tokens["total"]), components if int(ts_tokens["total"]) else 0)
                self.assertLessEqual(int(ts_tokens["total"]), injection_model.TOTAL_HARD)
                self.assertLessEqual(int(ts_tokens["system"]), injection_model.SYSTEM_MAX)

                memory_text = "\n".join(str(item.get("rendered") or "") for item in case.get("memory_items") or [])
                mirrored = injection_model.assemble_injection(
                    system_text=injection_model.SYSTEM_INSTRUCTIONS,
                    memory_text=memory_text,
                    day_pack=case.get("day") or {},
                    retrieve={
                        "items": case.get("memory_items") or [],
                        "top1_sim": case.get("top1_sim", 0.0),
                        "high_weight_count": case.get("high_weight_count", 0),
                    },
                    explicit=bool(case.get("explicit")),
                )
                for key in ("memory", "day", "detail", "history"):
                    self.assertEqual(
                        int(mirrored["tokens"][key]),
                        int(ts_tokens[key]),
                        f"TS/Python 装配分量不一致 {case['name']}.{key}: TS={ts_tokens} PY={mirrored['tokens']}",
                    )
                # system 常量文本不同：只要求两侧都在 128 上限内且实际总量差 ≤24 token
                self.assertLessEqual(int(mirrored["tokens"]["system"]), injection_model.SYSTEM_MAX)
                self.assertLessEqual(
                    abs(int(mirrored["tokens"]["total"]) - int(ts_tokens["total"])),
                    24,
                    f"TS/Python 注入总量差异过大: TS={ts_tokens} PY={mirrored['tokens']}",
                )


class DefaultConfigTest(unittest.TestCase):
    """默认配置必须真的被加载（回归：UTF-8 BOM 会让 load_defaults 静默返回 {}）。"""

    CONFIG_PATH = os.path.join(REPO_ROOT, "config", "default.json")

    @classmethod
    def setUpClass(cls) -> None:
        with open(cls.CONFIG_PATH, "rb") as fh:
            cls.raw_bytes = fh.read()

    def test_no_bom(self) -> None:
        self.assertFalse(self.raw_bytes.startswith(b"\xef\xbb\xbf"), "config/default.json 带 UTF-8 BOM，会导致默认配置加载失败")

    def test_defaults_loaded(self) -> None:
        import config as sidecar_config

        defaults = sidecar_config.load_defaults()
        self.assertTrue(defaults, "sidecar load_defaults() 返回空配置")
        injection = defaults.get("injection") or {}
        expected = {
            "totalHardTokens": 3000,
            "systemMaxTokens": 128,
            "memoryMaxTokens": 2048,
            "dayMaxTokens": 1024,
            "dayTextMaxTokens": 512,
            "detailMaxTokens": 312,
            "historyMaxTokens": 200,
        }
        for key, value in expected.items():
            self.assertEqual(injection.get(key), value, f"默认配置 injection.{key} 不一致")
        self.assertEqual((defaults.get("prune") or {}).get("watermarks", {}).get("workspace", {}).get("high"), 20000)
        self.assertEqual((defaults.get("llm") or {}).get("mode"), "host")


if __name__ == "__main__":
    unittest.main(verbosity=2)
