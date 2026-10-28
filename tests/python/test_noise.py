# -*- coding: utf-8 -*-
"""写入闸门 1-3 单测（CONTRACT §2.3）：F1 纯应答丢弃 / F2 放行信号 / F3 100 条短句零误杀 /
凭据丢弃+告警 / 工具输出一句话摘要。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIDECAR = os.path.join(ROOT, "sidecar")
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)

import noise  # noqa: E402

NOISE_PATH = os.path.join(ROOT, "config", "noiseWords.yaml")


class FakeJournal:
    def __init__(self) -> None:
        self.alarms: list[tuple[str, str, dict]] = []

    def log_alarm(self, scope: str, kind: str, detail: dict) -> None:
        self.alarms.append((scope, kind, detail))


# --------------------------------------------------------------------------- #
# F3 固定种子生成器：8~30 字短句，一半「无任何放行信号」的信息句 + 一半含信号句
# --------------------------------------------------------------------------- #
_PLAIN_NOUNS = [
    "会议纪要",
    "接口文档",
    "数据库迁移",
    "缓存策略",
    "日志轮转",
    "权限校验",
    "消息队列",
    "灰度发布",
    "单元测试",
    "依赖注入",
    "错误处理",
    "序列化",
    "索引优化",
    "监控告警",
    "回滚预案",
    "字段命名",
    "编码规范",
    "分支策略",
    "发布流程",
    "连接池",
    "线程池",
    "事务边界",
    "幂等设计",
    "数据校验",
    "压力测试",
    "需求评审",
    "版本迭代",
    "变更记录",
    "设计文档",
    "代码审查",
    "共享盘",
    "配置文件",
    "服务端",
    "客户端",
    "网关",
    "代理",
    "熔断阈值",
    "限流规则",
    "冷启动",
    "内存泄漏",
    "打包体积",
    "埋点上报",
    "兼容性测试",
    "联调",
]

_PLAIN_TEMPLATES = [
    "{a}和{b}都放在共享盘目录",
    "{a}的{b}写在设计文档里",
    "{a}相关{b}记录在变更记录里",
    "{a}与{b}放在同一个目录",
    "检查{a}和{b}的配置项",
    "{a}的实现依赖{b}",
    "{a}的{b}还没有补全",
    "{a}的{b}比较稳定",
    "{a}的{b}放在共享盘第三层目录",
    "把{a}的{b}同步到客户端",
    "{a}的{b}由服务端负责",
    "{a}和{b}的边界写清楚",
    "{a}的{b}记录在会议纪要里",
    "{a}的{b}还需要联调",
]

_SIGNAL_TEMPLATES = [
    "接口超时时间改成 {n} 毫秒",
    "下周一上午发布新版本",
    "用 React 重写列表页",
    "为什么{a}会失败",
    "记得把{a}也更新",
    "以后不要用 yarn 了",
    "配置在 /etc/{a}/config.json",
    "文档地址 https://{a}.example.com",
    "调用 useEffect() 初始化",
    "价格调整到 {n} 元",
    "今天先把{a}做完",
    '字段叫 "user_id"',
    "这个方案不行",
    "{a}从 {n1} 改成 {n2}",
    "@{mention} 看一下{a}",
    "预算超了 200 块",
]

_SAFE_A = ["接口", "缓存", "日志", "索引", "权限", "配置项", "字段", "模块", "服务", "网关", "文档", "任务"]
_SAFE_B = ["说明", "边界", "细节", "记录", "配置", "校验", "版本", "流程", "策略", "参数"]
_TOOLS = ["React", "Vue", "SQLite", "Redis"]
_MENTIONS = ["alice", "bob", "carol"]


def generate_short_sentences(seed: int = 20261028, count: int = 100) -> tuple[list[str], list[str]]:
    """返回 (全部短句, 其中无放行信号的短句)。固定种子，确定性。"""
    rng = random.Random(seed)
    plain: list[str] = []
    signal: list[str] = []
    seen: set[str] = set()

    def add(bucket: list[str], sentence: str) -> None:
        sentence = sentence.strip()
        if sentence in seen or not (8 <= len(sentence) <= 30):
            return
        seen.add(sentence)
        bucket.append(sentence)

    guard = 0
    while (len(plain) < count // 2 or len(signal) < count // 2) and guard < 20000:
        guard += 1
        if len(plain) < count // 2:
            template = rng.choice(_PLAIN_TEMPLATES)
            sentence = template.format(a=rng.choice(_SAFE_A), b=rng.choice(_SAFE_B))
            if noise.detect_pass_signal(sentence) is None:
                add(plain, sentence)
        else:
            template = rng.choice(_SIGNAL_TEMPLATES)
            sentence = template.format(
                a=rng.choice(_SAFE_A),
                b=rng.choice(_SAFE_B),
                n=rng.randint(1, 999),
                n1=rng.randint(1, 99),
                n2=rng.randint(100, 999),
                mention=rng.choice(_MENTIONS),
            )
            if noise.detect_pass_signal(sentence) is not None:
                add(signal, sentence)

    combined: list[str] = []
    for index in range(count):
        source = plain if index % 2 == 0 else signal
        combined.append(source[index // 2])
    return combined, plain


class NoiseWordLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.words = noise.load_noise_words(NOISE_PATH)

    def test_real_config_loaded(self) -> None:
        self.assertGreaterEqual(len(self.words["words"]), 40)
        self.assertGreaterEqual(len(self.words["stopwords"]), 20)
        for expected in ("好的", "没问题", "知道了", "谢谢"):
            self.assertIn(expected, self.words["words"])
        for expected in ("了", "的", "吧", "我"):
            self.assertIn(expected, self.words["stopwords"])

    def test_simple_parser_without_pyyaml(self) -> None:
        text = (
            "# comment\n"
            "words:\n"
            "  - 好的\n"
            "  - '谢谢'\n"
            '  - "知道了"  # inline comment\n'
            "stopwords:\n"
            "  - 了\n"
            "  - 吧\n"
        )
        parsed = noise._parse_noise_yaml_simple(text)
        self.assertEqual(parsed["words"], ["好的", "谢谢", "知道了"])
        self.assertEqual(parsed["stopwords"], ["了", "吧"])

    def test_simple_parser_flow_list(self) -> None:
        parsed = noise._parse_noise_yaml_simple("words: [好的, 可以]\nstopwords: [了]\n")
        self.assertEqual(parsed["words"], ["好的", "可以"])
        self.assertEqual(parsed["stopwords"], ["了"])

    def test_missing_file_uses_builtin_defaults(self) -> None:
        missing = os.path.join(ROOT, "config", "__does_not_exist__.yaml")
        words = noise.load_noise_words(missing)
        self.assertIn("好的", words["words"])
        self.assertIn("了", words["stopwords"])


class Gate1ToolSummaryTests(unittest.TestCase):
    def test_traceback_summarized(self) -> None:
        text = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in run\n'
            "    db.connect()\n"
            "Error: database connection refused"
        )
        result = noise.filter_turn(text, noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertTrue(result["sanitized"])
        self.assertFalse(result["dropped"])
        self.assertNotIn("\n", result["text"])
        self.assertIn("Error", result["text"])
        self.assertLessEqual(len(result["text"]), 120)

    def test_code_fence_summarized(self) -> None:
        text = "```python\nprint(1)\n```\n构建失败：缺少依赖"
        result = noise.filter_turn(text, noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertTrue(result["sanitized"])
        self.assertFalse(result["dropped"])
        self.assertEqual(result["text"], "构建失败：缺少依赖")

    def test_diff_summarized(self) -> None:
        text = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1 +1 @@\n"
            "- old line\n"
            "+ new line\n"
            "修复了登录超时问题"
        )
        result = noise.filter_turn(text, noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertTrue(result["sanitized"])
        self.assertFalse(result["dropped"])
        self.assertEqual(result["text"], "修复了登录超时问题")

    def test_json_tool_result_summarized(self) -> None:
        text = '{"ok":true,"path":"C:\\\\project\\\\config.json","count":3}'
        result = noise.filter_turn(text, noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertTrue(result["sanitized"])
        self.assertLessEqual(len(result["text"]), 120)

    def test_all_code_lines_fallback(self) -> None:
        text = "```\nconst x = 1;\n```"
        result = noise.filter_turn(text, noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertTrue(result["sanitized"])
        self.assertTrue(result["text"])

    def test_normal_chat_not_sanitized(self) -> None:
        result = noise.filter_turn("明天下午三点开会", noise_words=noise.load_noise_words(NOISE_PATH))
        self.assertFalse(result["sanitized"])
        self.assertFalse(result["dropped"])

    def test_tool_payload_detector(self) -> None:
        self.assertTrue(noise.looks_like_tool_payload("Traceback (most recent call last):\n boom"))
        self.assertTrue(noise.looks_like_tool_payload("```\nx\n```"))
        self.assertFalse(noise.looks_like_tool_payload("明天下午三点开会"))
        self.assertFalse(noise.looks_like_tool_payload("第一行普通消息\n第二行普通消息"))


class Gate2CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.words = noise.load_noise_words(NOISE_PATH)

    def _assert_credential_dropped(self, text: str) -> dict:
        journal = FakeJournal()
        result = noise.filter_turn(text, noise_words=self.words, journal=journal, scope="user")
        self.assertTrue(result["dropped"])
        self.assertTrue(result["alarm"])
        self.assertEqual(result["reason"], "credential")
        self.assertEqual(result["text"], "", "凭据绝不能回传明文")
        self.assertEqual(len(journal.alarms), 1)
        scope, kind, detail = journal.alarms[0]
        self.assertEqual(scope, "user")
        self.assertEqual(kind, "credential")
        self.assertEqual(detail.get("kind"), "credential_alarm")
        self.assertNotIn(text.strip(), json.dumps(detail, ensure_ascii=False))
        return result

    REAL_SK_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"  # 32 字符：OpenAI 真实形态
    REAL_BEARER = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"

    def test_sk_key(self) -> None:
        self._assert_credential_dropped("我的 key 是 " + self.REAL_SK_KEY)

    def test_bearer(self) -> None:
        self._assert_credential_dropped(self.REAL_BEARER)

    def test_private_key(self) -> None:
        self._assert_credential_dropped("-----BEGIN RSA PRIVATE KEY-----")

    def test_password_case_insensitive(self) -> None:
        self._assert_credential_dropped("配置里 PASSWORD = hunter2 别忘了")

    def test_password_equals_no_spaces(self) -> None:
        self._assert_credential_dropped("password=hunter2")

    def test_preview_masked(self) -> None:
        journal = FakeJournal()
        noise.filter_turn("我的 key 是 " + self.REAL_SK_KEY, noise_words=self.words, journal=journal)
        detail = journal.alarms[0][2]
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", json.dumps(detail, ensure_ascii=False))

    def test_alarm_without_journal(self) -> None:
        result = noise.filter_turn("password=12345678", noise_words=self.words)
        self.assertTrue(result["alarm"])
        self.assertTrue(result["dropped"])

    # ------------------------------------------------------------------ #
    # E2E 误伤回归：普通英文里的 "sk-" 子串 / "bearer token" 不得告警丢弃
    # ------------------------------------------------------------------ #
    def test_english_words_not_mistaken_for_credentials(self) -> None:
        for word in ["task-specific", "risk-assessment", "desk-station"]:
            with self.subTest(word=word):
                self.assertEqual(noise.find_credentials(word), [])
                journal = FakeJournal()
                result = noise.filter_turn(word, noise_words=self.words, journal=journal)
                self.assertFalse(result["alarm"], msg=f"误报告警: {word!r}")
                self.assertFalse(result["dropped"], msg=f"误伤丢弃: {word!r}")
                self.assertEqual(journal.alarms, [])

        sentences = [
            "a reusable set of task-specific instructions",
            "risk-assessment and desk-station notes for the task-specific plan",
            "the bearer token is required for this request",
        ]
        for text in sentences:
            with self.subTest(text=text):
                self.assertEqual(noise.find_credentials(text), [])
                journal = FakeJournal()
                result = noise.filter_turn(text, noise_words=self.words, journal=journal)
                self.assertFalse(result["alarm"], msg=f"误报告警: {text!r}")
                self.assertFalse(result["dropped"], msg=f"误伤丢弃: {text!r}")
                self.assertEqual(journal.alarms, [])

        # 边界从严：过短的 sk- 片段不告警
        for text in ["sk-abc123", "risk sk-short"]:
            self.assertEqual(noise.find_credentials(text), [], msg=text)

    def test_real_credential_shapes_still_alarmed(self) -> None:
        cases = [
            ("sk_key", self.REAL_SK_KEY),
            ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
            ("password", "password=hunter2"),
        ]
        for expected_kind, text in cases:
            with self.subTest(kind=expected_kind):
                journal = FakeJournal()
                result = noise.filter_turn(text, noise_words=self.words, journal=journal, scope="user")
                self.assertTrue(result["dropped"])
                self.assertTrue(result["alarm"])
                self.assertEqual(result["reason"], "credential")
                self.assertEqual(result["text"], "")
                self.assertEqual(len(journal.alarms), 1)
                self.assertIn(expected_kind, journal.alarms[0][2].get("patterns", []))


class Gate3SignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.words = noise.load_noise_words(NOISE_PATH)

    def _filter(self, text: str) -> dict:
        return noise.filter_turn(text, noise_words=self.words)

    def test_f1_pure_replies_dropped(self) -> None:
        pure_replies = [
            "好的",
            "嗯",
            "谢谢",
            "知道了",
            "收到",
            "明白",
            "没问题",
            "好的谢谢",
            "我看看",
            "稍等一下",
            "那就这样吧",
            "行吧，都可以",
            "嗯嗯，知道了",
            "好的，没问题的",
            "谢谢，辛苦了",
            "可以的，收到",
            "好的，收到，谢谢",
            "知道了知道了知道了",
            "收到收到收到收到",
            "没问题，你辛苦了",
            "行吧，那就这样吧",
            "谢谢，都行，可以",
            "好的好的好的好的",
            "嗯嗯嗯嗯嗯嗯嗯嗯",
            "没事，没关系",
        ]
        for text in pure_replies:
            with self.subTest(text=text):
                result = self._filter(text)
                self.assertTrue(result["dropped"], msg=f"纯应答被误放行: {text!r}")
                self.assertIn(result["reason"], {"too_short", "pure_noise"})

    def test_f2_pass_signals_released(self) -> None:
        cases = [
            ("number", "改成 3 个就行"),
            ("time", "明天下午碰头"),
            ("amount", "预算超了"),
            ("proper_name", "React 发布了新版本"),
            ("proper_name", "useEffect 清理副作用"),
            ("path_or_domain", "配置在 C:\\project\\config.json"),
            ("path_or_domain", "文档在 example.com 上"),
            ("mention", "@alice 你来看一下"),
            ("code_identifier", '字段叫 "user_id"'),
            ("question", "为什么构建会失败"),
            ("negation", "这个方案不行"),
            ("decision_preference", "我喜欢深色主题"),
            ("decision_preference", "我决定用 pnpm 替代 npm"),
            ("decision_preference", "记住我的默认分支是 main"),
        ]
        hits: set[str] = set()
        for expected, text in cases:
            with self.subTest(text=text):
                result = self._filter(text)
                self.assertFalse(result["dropped"], msg=f"放行信号句被误杀: {text!r}")
                self.assertEqual(result.get("signal"), expected)
                hits.add(expected)
        self.assertGreaterEqual(len(hits), 5, "F2 至少覆盖五类放行信号")

    def test_short_informative_sentences_kept(self) -> None:
        for text in [
            "会议纪要在共享盘",
            "接口文档缺错误码",
            "数据库迁移脚本写在文档里",
            "缓存策略需要重新评估",
            "日志轮转按大小切分",
        ]:
            with self.subTest(text=text):
                result = self._filter(text)
                self.assertFalse(result["dropped"], msg=f"有信息量的短句被误杀: {text!r}")

    def test_f3_generated_short_sentences_no_false_kill(self) -> None:
        sentences, plain = generate_short_sentences(count=100)
        self.assertEqual(len(sentences), 100)
        self.assertGreaterEqual(len(plain), 40, "生成器必须覆盖足够多『无放行信号』的不确定短句")
        dropped: list[str] = []
        for text in sentences:
            with self.subTest(text=text):
                self.assertGreaterEqual(len(text), 8)
                self.assertLessEqual(len(text), 30)
                result = self._filter(text)
                if result["dropped"]:
                    dropped.append(text)
        self.assertEqual(dropped, [], msg=f"F3 误杀 {len(dropped)} 条: {dropped}")

    def test_empty_text_dropped(self) -> None:
        for text in ["", "   ", "\n\t"]:
            result = noise.filter_turn(text, noise_words=self.words)
            self.assertTrue(result["dropped"])
            self.assertEqual(result["reason"], "empty")


if __name__ == "__main__":
    unittest.main()
