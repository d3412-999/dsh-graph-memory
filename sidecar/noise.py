"""写入四道闸门（CONTRACT §2.3）—— 本地零 LLM 成本。

顺序（不可颠倒）：
1. 工具结果/堆栈/diff/文件原文 → 只保留一句话摘要（首个非代码行/路径行，截断 120 字），
   继续后续闸门（sanitized=True）；
2. 凭据正则（sk-/Bearer/PRIVATE KEY/password=）→ 整条丢弃 + Journal 告警
   （journal.log_alarm(scope, "credential", detail)，detail.kind="credential_alarm"）；
3. 短句/无信息量：**先检查放行信号，未命中才判定丢弃**；拿不准就放行；
4. 与已有节点相似度 >0.95 的去重属于 service 的图操作（不在本模块）。

边界从严：任何一条放行信号命中即放行；只有「短」或「整句可由语气词/停用词拼出」
这类明确无信息量的句子才会被丢弃。被丢弃的句子仍由 service/daykeeper 保留为当天原文，
本模块只负责给出判定。
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

from base import has_negation_marker, log, normalize_space

# --------------------------------------------------------------------------- #
# 配置路径与内置兜底词表（与 config/noiseWords.yaml 等价；文件缺失时使用）
# --------------------------------------------------------------------------- #
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NOISE_WORDS_PATH = os.path.join(REPO_ROOT, "config", "noiseWords.yaml")

DEFAULT_WORDS = (
    "好的",
    "可以",
    "行",
    "嗯",
    "哦",
    "啊",
    "谢谢",
    "感谢",
    "明白",
    "收到",
    "了解",
    "知道了",
    "OK",
    "ok",
    "yes",
    "no",
    "对",
    "是的",
    "不是",
    "没问题",
    "随便",
    "都行",
    "再说",
    "看看",
    "稍等",
    "等等",
    "算了",
    "没事",
    "不客气",
    "哈哈",
    "行吧",
    "就这样",
    "先这样",
    "那就这样",
    "没别的",
    "你决定",
    "你来定",
    "我看下",
    "我想想",
    "稍后",
    "回头",
    "改天",
    "无所谓",
    "都可以",
    "没关系",
    "辛苦了",
    "棒",
    "赞",
    "厉害",
)

DEFAULT_STOPWORDS = (
    "了",
    "的",
    "吧",
    "呢",
    "吗",
    "啊",
    "呀",
    "哈",
    "我",
    "你",
    "先",
    "那",
    "就",
    "这",
    "是",
    "好",
    "看",
    "下",
    "一",
    "个",
    "稍",
    "等",
    "再",
    "没",
    "别",
    "地",
    "得",
    "着",
    "过",
    "要",
    "会",
    "能",
)


# --------------------------------------------------------------------------- #
# 词表解析：PyYAML 优先；缺失时用不依赖 PyYAML 的简易解析兜底
# --------------------------------------------------------------------------- #
def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_flow_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items: list[str] = []
    for part in re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", inner):
        item = _strip_quotes(part)
        if item:
            items.append(item)
    return items


def _parse_noise_yaml_simple(text: str) -> dict[str, list[str]]:
    """不依赖 PyYAML 的简易解析器（本机无 PyYAML 时真实生效，故必须健壮）。

    只支持本项目词表文件用到的 YAML 子集：顶层 `key:` + `- item` 列表，
    以及 `key: [a, b]` 行内列表；忽略注释与空行。
    """
    buckets: dict[str, list[str]] = {"words": [], "stopwords": []}
    current: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 去掉行内注释（本词表不含带 # 的词条）
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if not line[0].isspace() and stripped.endswith(":") and not stripped.startswith("-"):
            key = stripped[:-1].strip().lower()
            current = key if key in buckets else None
            continue
        match = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if match:
            item = _strip_quotes(match.group(1))
            if item and current:
                buckets[current].append(item)
            continue
        # key: [a, b] 行内列表
        flow = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(\[.*\])\s*$", stripped)
        if flow:
            key = flow.group(1).lower()
            if key in buckets:
                buckets[key].extend(_parse_flow_list(flow.group(2)))
    return buckets


def _load_yaml_file(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return {}
    try:  # PyYAML 优先（可选依赖）
        import yaml  # type: ignore  # noqa: PLC0415

        loaded = yaml.safe_load(content)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 — 无 PyYAML / 解析失败 → 简易解析兜底
        return _parse_noise_yaml_simple(content)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = _strip_quotes(str(item))
        if text:
            out.append(text)
    return out


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def load_noise_words(path: Optional[str] = None) -> dict[str, list[str]]:
    """加载 noiseWords.yaml → {"words": [...], "stopwords": [...]}。

    - path=None 用仓库默认 config/noiseWords.yaml；
    - 文件缺失/解析失败 → 内置等价默认表兜底（契约 §2.3）；
    - PyYAML 可用时优先用它，否则用 `_parse_noise_yaml_simple`。
    """
    target = path or DEFAULT_NOISE_WORDS_PATH
    data = _load_yaml_file(target) if target and os.path.isfile(target) else {}
    words = _as_str_list(data.get("words"))
    stopwords = _as_str_list(data.get("stopwords"))
    if not words:
        words = list(DEFAULT_WORDS)
    if not stopwords:
        stopwords = list(DEFAULT_STOPWORDS)
    return {"words": _dedupe(words), "stopwords": _dedupe(stopwords)}


# --------------------------------------------------------------------------- #
# 闸门 1：工具结果 / 堆栈 / diff / 文件原文 识别 + 一句话摘要
# --------------------------------------------------------------------------- #
_MAX_SUMMARY_CHARS = 120

_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)|^\s*File \"[^\"]+\", line \d+", re.MULTILINE)
_JS_STACK_RE = re.compile(r"^\s*at\s+\S.*(?:\(.*:\d+:\d+\)|:\d+:\d+)", re.MULTILINE)
_DIFF_RE = re.compile(r"^(?:diff --git |@@ -\d+.*?@@|(?:\+\+\+|---) [ab]/)", re.MULTILINE)
_TOOL_STREAM_RE = re.compile(r"(?im)^(?:stdout|stderr|exit\s*code|error|warning|output)\s*[:=]")
_PATH_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/][^\s]*|\.{1,2}[\\/][^\s]*|~[\\/][^\s]*|/[^\s]*|[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+)[\\/]?$"
)
_CODE_LINE_RE = re.compile(
    r"^\s*(?:at\s|Traceback\b|File \"|\.\.\.|>>>|\$ |> |#include\b|import\s|from\s+\S+\s+import\b|"
    r"def\s|class\s|function\s|const\s|let\s|var\s|return\b|if\s*\(|for\s*\(|while\s*\(|print\(|"
    r"console\.|public\s|private\s|package\s|using\s|namespace\s|@\w+\(|</?[A-Za-z][\w:-]*)"
)
_CODE_TAIL_RE = re.compile(r"[{};]\s*$|[)\]>]\s*$")
_FENCE_RE = re.compile(r"^\s*```")


def _is_code_line(line: str) -> bool:
    if _FENCE_RE.match(line):
        return True
    stripped = line.strip()
    if stripped.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
        return True
    if _CODE_LINE_RE.match(line):
        return True
    if not stripped:
        return False
    if stripped.startswith(("{", "}", "[", "]", ")", "(", "+", "-", "@", "*", "/*", "//")):
        return True
    if _CODE_TAIL_RE.search(stripped) and not stripped.endswith("。") and not stripped.endswith("，"):
        return True
    # 符号密度高的行（JSON/代码/命令行）
    symbols = sum(1 for ch in stripped if ch in "{}[]()<>;=+*/\\|&%$#`~^")
    if len(stripped) >= 12 and symbols / len(stripped) > 0.15:
        return True
    return False


def _is_path_only(line: str) -> bool:
    return bool(_PATH_ONLY_RE.match(line.strip()))


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return False
    import json  # noqa: PLC0415

    try:
        json.loads(stripped)
        return True
    except (ValueError, TypeError):
        return False


def looks_like_tool_payload(text: str) -> bool:
    """判定「工具结果/堆栈/diff/文件原文」。宁可漏判（继续由闸门 3 处理）不可过度改写正常对话。"""
    raw = text or ""
    if not raw.strip():
        return False
    if "```" in raw:
        return True
    if _TRACEBACK_RE.search(raw) or _JS_STACK_RE.search(raw) or _DIFF_RE.search(raw):
        return True
    if _looks_like_json(raw):
        return True
    if _TOOL_STREAM_RE.search(raw):
        return True
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    code_like = sum(1 for line in lines if _is_code_line(line) or _is_path_only(line))
    total_chars = sum(len(line.strip()) for line in lines)
    if total_chars < 40:
        return False
    return code_like / len(lines) >= 0.5


def summarize_tool_output(text: str, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """取首个非代码行/路径行，截断 120 字；全被过滤时退回首行。"""
    fallback = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not fallback:
            fallback = stripped.lstrip("`").strip()
        if _is_code_line(stripped) or _is_path_only(stripped):
            continue
        return stripped[:max_chars]
    return fallback[:max_chars]


# --------------------------------------------------------------------------- #
# 闸门 2：凭据
# --------------------------------------------------------------------------- #
# 注意量化边界（E2E 误伤修复）：
# - sk- key 必须有左边界且密钥段足够长（OpenAI 真 key >=32 字符，这里取 16 保守），
#   避免 task-specific / risk-assessment / desk-station 里的 "sk-" 被误判；
# - Bearer 后必须是「像 token 的串」：
#     * 含至少一个数字且总长 >=8（如 abcdef123456.token），或
#     * 不含数字但总长 >=20（如长随机字母 token）；
#   这样既能拦住真 Bearer X，又不会误伤 "bearer token is required" / "Bearer authentication"；
# - PRIVATE KEY 头 / password= 按契约保留。
_BEARER_TOKEN_MIN_CHARS = 20
_BEARER_DIGIT_TOKEN_MIN_CHARS = 8
_BEARER_RE = (
    r"(?i)\bBearer\s+(?:"
    r"[A-Za-z0-9._~+/-]*[0-9][A-Za-z0-9._~+/-]{%d,}"
    r"|[A-Za-z0-9._~+/-]{%d,}"
    r")(?![A-Za-z0-9._~+/-])"
) % (_BEARER_DIGIT_TOKEN_MIN_CHARS - 1, _BEARER_TOKEN_MIN_CHARS)
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{16,}(?![A-Za-z0-9])")),
    ("bearer", re.compile(_BEARER_RE)),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password", re.compile(r"(?i)password\s*=")),
)


def _mask_secret(kind: str, matched: str) -> str:
    if kind == "sk_key":
        return matched[:4] + "***"
    if kind == "bearer":
        return "Bearer ***"
    if kind == "private_key":
        return re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "-----BEGIN *** PRIVATE KEY-----", matched)
    if kind == "password":
        return "password=***"
    return "***"


def find_credentials(text: str) -> list[dict[str, str]]:
    """返回命中的凭据描述（detail 内只保留掩码，不落明文）。"""
    hits: list[dict[str, str]] = []
    raw = text or ""
    for kind, pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(raw)
        if match is None:
            continue
        hits.append({"kind": kind, "preview": _mask_secret(kind, match.group(0))})
    return hits


def _alarm_credential(journal: Any, scope: str, hits: list[dict[str, str]]) -> None:
    if journal is None or not hits:
        return
    detail = {
        "kind": "credential_alarm",
        "reason": "credential",
        "patterns": [hit["kind"] for hit in hits],
        "previews": [hit["preview"] for hit in hits],
    }
    logger = getattr(journal, "log_alarm", None)
    if not callable(logger):
        log("warning", "journal has no log_alarm; credential alarm skipped", scope=scope)
        return
    try:
        logger(scope, "credential", detail)
    except TypeError:
        try:  # 兼容关键字签名
            logger(scope=scope, kind="credential", detail=detail)
        except Exception as exc:  # noqa: BLE001
            log("warning", "credential alarm log failed", scope=scope, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — 告警失败不得阻断判定
        log("warning", "credential alarm log failed", scope=scope, error=str(exc))


# --------------------------------------------------------------------------- #
# 闸门 3：放行信号（先放行，后丢弃）
# --------------------------------------------------------------------------- #
# 数字 / 数量 / 金额
_NUMBER_RE = re.compile(r"\d")
_CJK_QUANTITY_RE = re.compile(
    r"[一二三四五六七八九十百千万亿两半]\s*(?:个|条|次|遍|只|台|件|份|张|页|行|字|元|块|万|千|百|亿|倍|人|天|年|月|日|号|点|分|秒|小时|分钟|周)"
)
# 日期 / 时间 / 相对时间
_TIME_WORDS = (
    "今天",
    "明天",
    "昨天",
    "前天",
    "后天",
    "今日",
    "明日",
    "上午",
    "下午",
    "中午",
    "晚上",
    "早上",
    "凌晨",
    "半夜",
    "清晨",
    "傍晚",
    "今晚",
    "明早",
    "周末",
    "周",
    "星期",
    "礼拜",
    "月底",
    "月初",
    "年初",
    "年底",
    "季度",
    "小时",
    "分钟",
    "秒钟",
    "刚才",
    "刚刚",
    "最近",
    "以后",
    "之前",
    "之后",
    "现在",
    "目前",
    "马上",
    "立刻",
    "稍后",
    "回头",
    "改天",
    "下次",
    "上次",
    "本周",
    "下周",
    "上周",
    "每天",
    "每周",
    "每月",
    "每年",
    "点",
)
_AMOUNT_WORDS = (
    "价格",
    "费用",
    "成本",
    "预算",
    "工资",
    "薪水",
    "金额",
    "花费",
    "账单",
    "人民币",
    "美元",
    "块钱",
)
# 专名：大写英文 / 驼峰 / 路径 / 域名 / @提及
_UPPER_WORD_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.+-]*[A-Za-z0-9]\b|\b[A-Z]{2,}\b")
_CAMEL_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|/[A-Za-z0-9_.-]+/)[^\s，。；！？、\"'<>|]*")
_DOMAIN_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.(?:com|cn|org|net|io|dev|ai|co|app|me|xyz)\b"
    r"|\b[\w.+-]+@[\w-]+\.[\w.]+\b"
)
# 代码标识符：下划线 / 点号 / 括号 / 被引号包裹
_CODE_IDENT_RE = re.compile(
    r"[A-Za-z0-9]+_[A-Za-z0-9_]+|[\w]+[._][\w]+|\(\)|[`\"“”‘’][^`\"“”‘’\n]{1,60}[`\"“”‘’]"
)
# @提及
_AT_MENTION_RE = re.compile(r"@[A-Za-z0-9_\u4e00-\u9fff-]{1,30}")
# 疑问句
_QUESTION_RE = re.compile(r"[?？]|吗|呢|谁|什么|怎么|为什么|为啥|哪|几|如何|是否|能不能|可不可以|有没有|多少")
# 决策 / 意愿 / 改口 / 偏好 / 记住
_DECISION_RE = re.compile(
    r"我喜欢|我讨厌|我偏好|我习惯|我们(?:要|决定|计划|准备|打算)|我要|我想|决定|计划|准备|打算|"
    r"以后|从今|从现在起|不再|改用|改成|改为|换成|调整|采用|选择|继续|停止|取消|不要|不用|别用|"
    r"记得|记住|偏好|希望|需要|最好|必须|一定|接下来|建议|推荐|同意|拒绝|接受|放弃|保留|删除|"
    r"memory_pin|\bpin\b|标记为|重点关注"
)
_PREFERENCE_RE = re.compile(r"喜欢|偏爱|偏好|讨厌|不喜欢|习惯|倾向于|爱用|爱喝|爱吃|迷恋|钟爱|中意")


def _hit_any(text: str, needles: Iterable[str]) -> Optional[str]:
    for needle in needles:
        if needle and needle in text:
            return needle
    return None


def detect_pass_signal(text: str) -> Optional[str]:
    """返回首个命中的放行信号名；None 表示无信号（才允许进入丢弃判定）。"""
    if not text:
        return None
    if _NUMBER_RE.search(text) or _CJK_QUANTITY_RE.search(text):
        return "number"
    if _hit_any(text, _TIME_WORDS):
        return "time"
    if _hit_any(text, _AMOUNT_WORDS):
        return "amount"
    if _UPPER_WORD_RE.search(text) or _CAMEL_RE.search(text):
        return "proper_name"
    if _PATH_RE.search(text) or _DOMAIN_RE.search(text):
        return "path_or_domain"
    if _AT_MENTION_RE.search(text):
        return "mention"
    if _CODE_IDENT_RE.search(text):
        return "code_identifier"
    if _QUESTION_RE.search(text):
        return "question"
    if has_negation_marker(text):
        return "negation"
    if _DECISION_RE.search(text) or _PREFERENCE_RE.search(text):
        return "decision_preference"
    return None


# 纯语气词判定：整句（去标点空白后）能否完全由 noiseWords + stopwords 拼出
_CLEAN_FOR_COVERAGE_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")


def _coverage_tokens(noise_words: Optional[dict[str, Any]]) -> set[str]:
    words = list(DEFAULT_WORDS)
    stopwords = list(DEFAULT_STOPWORDS)
    if isinstance(noise_words, dict):
        words = _as_str_list(noise_words.get("words")) or words
        stopwords = _as_str_list(noise_words.get("stopwords")) or stopwords
    tokens: set[str] = set()
    for item in [*words, *stopwords]:
        key = item.lower().strip()
        if key:
            tokens.add(key)
    return tokens


def _is_fully_covered(text: str, tokens: set[str]) -> bool:
    """整句覆盖率=1.0：去掉标点/空白后，字符串可被词表 token 完整切分。"""
    cleaned = _CLEAN_FOR_COVERAGE_RE.sub("", text or "").lower()
    if not cleaned:
        return True
    max_len = max((len(token) for token in tokens), default=0)
    if max_len == 0:
        return False
    reachable = [False] * (len(cleaned) + 1)
    reachable[0] = True
    for start in range(len(cleaned)):
        if not reachable[start]:
            continue
        limit = min(max_len, len(cleaned) - start)
        for length in range(1, limit + 1):
            if cleaned[start : start + length] in tokens:
                reachable[start + length] = True
        if reachable[len(cleaned)]:
            return True
    return reachable[len(cleaned)]


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
MIN_TEXT_LENGTH = 8
MAX_NOISE_TEXT_LENGTH = 30


def filter_turn(
    turn_text: str,
    *,
    noise_words: Optional[dict[str, Any]] = None,
    journal: Any = None,
    scope: str = "workspace",
) -> dict[str, Any]:
    """四道闸门中的 1-3 道（第 4 道相似度去重在 service 层）。

    返回 {"text", "dropped", "reason", "alarm", "sanitized"}：
    - text：可用文本；工具结果等为「一句话摘要」；凭据丢弃时为空串（绝不回传明文）；
    - dropped：是否整条丢弃（不进图，但仍由 service 保留为当天原文）；
    - reason：passed / empty / credential / too_short / pure_noise；
    - alarm：是否已触发凭据告警；
    - sanitized：是否做了工具结果→摘要的改写。
    """
    raw = (turn_text or "").strip()
    if not raw:
        return {"text": "", "dropped": True, "reason": "empty", "alarm": False, "sanitized": False}

    # 闸门 1：工具结果/堆栈/diff/文件原文 → 一句话摘要（继续后续闸门）
    sanitized = False
    if looks_like_tool_payload(raw):
        summary = summarize_tool_output(raw)
        sanitized = True
        working = summary
    else:
        working = raw

    # 闸门 2：凭据（对原文与摘要同时检查，避免摘要恰好跳过含密钥的行）
    hits = find_credentials(raw)
    if not hits and working != raw:
        hits = find_credentials(working)
    if hits:
        _alarm_credential(journal, scope, hits)
        log("warning", "credential detected; turn dropped", scope=scope, patterns=[h["kind"] for h in hits])
        return {
            "text": "",
            "dropped": True,
            "reason": "credential",
            "alarm": True,
            "sanitized": sanitized,
        }

    check_text = " ".join(working.split())
    if not check_text:
        return {
            "text": "",
            "dropped": True,
            "reason": "empty",
            "alarm": False,
            "sanitized": sanitized,
        }

    # 闸门 3：先放行信号，后丢弃判定（边界从严，拿不准就放行）
    signal = detect_pass_signal(check_text)
    if signal is not None:
        return {
            "text": working,
            "dropped": False,
            "reason": "passed",
            "alarm": False,
            "sanitized": sanitized,
            "signal": signal,
        }

    if len(check_text) < MIN_TEXT_LENGTH:
        return {
            "text": working,
            "dropped": True,
            "reason": "too_short",
            "alarm": False,
            "sanitized": sanitized,
        }

    if len(check_text) <= MAX_NOISE_TEXT_LENGTH:
        tokens = _coverage_tokens(noise_words)
        if _is_fully_covered(check_text, tokens):
            return {
                "text": working,
                "dropped": True,
                "reason": "pure_noise",
                "alarm": False,
                "sanitized": sanitized,
            }

    return {
        "text": working,
        "dropped": False,
        "reason": "passed",
        "alarm": False,
        "sanitized": sanitized,
    }


__all__ = [
    "DEFAULT_NOISE_WORDS_PATH",
    "DEFAULT_STOPWORDS",
    "DEFAULT_WORDS",
    "MAX_NOISE_TEXT_LENGTH",
    "MIN_TEXT_LENGTH",
    "detect_pass_signal",
    "filter_turn",
    "find_credentials",
    "load_noise_words",
    "looks_like_tool_payload",
    "summarize_tool_output",
]
