"""记忆抽取（CONTRACT §2.4 / §4）。

- `extract_memories(turn_text, *, scope, llm)`：LLM 抽取 → 健壮 JSON 解析（失败重试一次）
  → 仍失败使用 `offline_rules` 确定性规则；任何 LLM 异常都降级而绝不向上抛。
- `offline_rules`：模块级单例，实现 extract/summarize/judge 三个确定性方法；
  `llm.mode=offline` 与 LLM 故障降级共用，不依赖网络、无随机性。
- LLM 提示词要求严格 JSON 数组，元素 `{name,type,description}`；用 `<<<TURN ... TURN>>>`
  标记包裹原文，使 offline 规则能对「prompt」和「原文」都正确工作（llm_bridge 会把 prompt
  直接交给 offline_rules）。
"""
from __future__ import annotations

import inspect
import json
import re
from typing import Any, Optional

from base import (
    ENTITY_TYPES,
    estimate_tokens,
    log,
    normalize_space,
    truncate_to_tokens,
)

# --------------------------------------------------------------------------- #
# LLM 提示词
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM = (
    "你是记忆抽取器。只输出严格 JSON 数组，不要 markdown 代码围栏、不要解释、不要多余文本。"
    "数组元素形如 {\"name\":\"简短名词短语\",\"type\":\"Preference|Decision|Concept|Tool|Project|Person|"
    "Organization|Location|Event|Workflow|Data|Other\",\"description\":\"一句话描述\"}。"
    "description 不得包含权重、日期、pin 等元数据。没有值得长期记住的内容时输出 []。"
)

_TYPES_HINT = "|".join(ENTITY_TYPES)


def build_extract_prompt(turn_text: str, scope: str = "user") -> str:
    scope_hint = (
        "scope=user：只抽用户长期偏好/决定/个人事实；"
        if scope == "user"
        else "scope=workspace：只抽项目知识/技术决策/工具与约定；"
    )
    return (
        "从下面对话原文中抽取值得长期记住的记忆实体。\n"
        f"{scope_hint}\n"
        f"type 必须是以下之一：{_TYPES_HINT}。\n"
        "name 用简短名词短语（如「咖啡」「包管理器」）；description 用一句话（如「用户喜欢咖啡」）。\n"
        "只输出 JSON 数组；无内容输出 []。\n"
        "<<<TURN\n"
        f"{(turn_text or '').strip()}\n"
        "TURN>>>"
    )


# --------------------------------------------------------------------------- #
# 标记文本解包（prompt ↔ 原文 双用）
# --------------------------------------------------------------------------- #
_MARKED_RE = re.compile(r"<<<(TURN|TEXT|DAY|CONTENT|SUMMARY)\b(.*?)\1>>>", re.DOTALL)


def unwrap_marked_text(text: str) -> str:
    """若文本是带 `<<<TAG ... TAG>>>` 标记的 prompt，取标记内原文；否则原样返回。"""
    raw = text or ""
    match = _MARKED_RE.search(raw)
    if match:
        return match.group(2).strip()
    return raw.strip()


# --------------------------------------------------------------------------- #
# 离线规则引擎
# --------------------------------------------------------------------------- #
_PREF_VERB_RE = re.compile(
    r"(?P<subject>我|我们|用户|本人|自己)?\s*"
    r"(?:现在|最近|已经|一直|平时|通常|一般|很|特别|超级|比较|最|更|真的|就|也|还)?\s*"
    r"(?P<neg>不太|不再|从来不|从不|不)?\s*"
    r"(?P<verb>喜欢|偏爱|偏好|讨厌|习惯(?:用|喝|吃)?|爱用|爱喝|爱吃|中意|迷恋|钟爱)\s*"
    r"(?:(?:用|使用|喝|吃|玩|看|听|做|读)\s*)?"
    r"(?P<obj>[^\s，。；,;！!？?]{1,30})"
)

_DECISION_RE = re.compile(
    r"(?P<subject>我|我们|用户)?\s*"
    r"(?:决定|计划|准备|打算|以后(?:都)?|从今以后|从现在起|不再|必须|需要|要)\s*"
    r"(?P<obj>[^，。；,;！!？?\n]{1,60})"
)

_ASSIGN_RE = re.compile(
    r"(?P<attr>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z0-9_]{0,11})\s*"
    r"(?:从\s*[^，。；,;]{1,20}\s*)?"
    r"(?:改成|改为|换成|调整到|调整成|调整为|更新为|设置为|设为|变成|定为|改到|调到|升到|降到|"
    r"现在(?:是|用)|已经(?:是|用))\s*"
    r"(?P<val>[^\s，。；,;！!？?]{1,40})"
)

_TOOL_RE = re.compile(
    r"(?:使用|用|安装了?|升级到|迁移到|切换到|改用|采用|基于)\s*"
    r"(?P<tool>[A-Za-z][A-Za-z0-9_.+-]{1,40})"
)

_REMEMBER_RE = re.compile(r"(?:记住|记得|别忘了|记一下|记下|memory_pin|\bpin\b)\s*[:：]?\s*(?P<obj>[^。；\n]{2,80})")

# 通用抽取线索：出现这些词说明句子可能承载项目知识（离线模式保守兜底）
_GENERIC_CUES = (
    "用",
    "需要",
    "必须",
    "重要",
    "注意",
    "配置",
    "部署",
    "项目",
    "版本",
    "文件",
    "目录",
    "接口",
    "数据库",
    "依赖",
    "规则",
    "约定",
    "标准",
    "流程",
    "计划",
    "目标",
    "偏好",
    "习惯",
    "风格",
    "架构",
    "地址",
    "端口",
    "路径",
    "命名",
    "格式",
    "字段",
    "参数",
    "启动",
    "构建",
    "测试",
    "发布",
    "日志",
    "缓存",
    "权限",
    "安全",
    "性能",
    "监控",
    "告警",
    "分支",
    "提交",
    "回滚",
    "迁移",
)

_LEADING_FILLERS = (
    "那么",
    "所以",
    "另外",
    "然后",
    "就是",
    "其实",
    "不过",
    "而且",
    "并且",
    "对了",
    "我们",
    "我",
    "用户",
    "本人",
    "自己",
    "把",
    "将",
    "现在",
    "目前",
    "已经",
    "最近",
    "一直",
    "都",
    "也",
    "还",
    "就",
    "先",
    "再",
)

_OBJ_TRAILING_FILLERS = ("了", "吧", "呢", "啊", "呀", "哦", "的", "啦", "嘛", "哈")
_OBJ_LEADING_VERBS = ("继续", "开始", "以后", "都", "再", "改用", "使用", "用", "做", "搞", "弄", "采用", "选择")


def _strip_prefixes(text: str, prefixes: tuple[str, ...]) -> str:
    value = (text or "").strip()
    changed = True
    while changed and value:
        changed = False
        for prefix in prefixes:
            if value.startswith(prefix) and len(value) > len(prefix):
                value = value[len(prefix) :].strip()
                changed = True
                break
    return value


def _clean_obj(obj: str) -> str:
    value = _strip_prefixes(obj, _LEADING_FILLERS)
    value = _strip_prefixes(value, _OBJ_LEADING_VERBS)
    value = value.strip("，。；：:、,.!?！？\"'`“”‘’ ")
    for filler in _OBJ_TRAILING_FILLERS:
        if value.endswith(filler) and len(value) > len(filler) + 1:
            value = value[: -len(filler)].strip()
    return value


def _guess_name(text: str, limit: int = 24) -> str:
    compact = normalize_space(text)
    if not compact:
        return ""
    ascii_token = re.match(r"[A-Za-z][A-Za-z0-9_.+-]{1,24}", compact)
    if ascii_token:
        return ascii_token.group(0)
    clause = re.split(r"[，。；,;！!？?\s]", compact, maxsplit=1)[0].strip()
    return (clause or compact)[:limit]


def _guess_type(text: str) -> str:
    lowered = text or ""
    if re.search(r"喜欢|偏爱|偏好|讨厌|习惯|风格", lowered):
        return "Preference"
    if re.search(r"决定|计划|打算|要|以后|不再|改成|改为|换成|采用", lowered):
        return "Decision"
    return "Concept"


_TRAILING_META_RE = re.compile(r"(?i)\b(?:w|weight|pin|pinned)\s*=\s*[^\s，。；,;]*")


def _clean_description(text: str, limit: int = 500) -> str:
    value = normalize_space(text or "")
    # R3：description 严禁出现权重/pin 等元数据
    value = _TRAILING_META_RE.sub("", value).strip("，。；,; ")
    return value[:limit]


class _OfflineRules:
    """确定性离线规则引擎（llm.mode=offline 与 LLM 故障降级；不得联网）。"""

    # ---------------------------------------------------------------- extract
    def extract(self, text: str, scope: str = "") -> list[dict[str, Any]]:
        turn = unwrap_marked_text(text)
        turn = normalize_space(turn)
        if len(turn) < 4:
            return []
        facts: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(name: str, etype: str, description: str) -> None:
            name = normalize_space(name)[:120]
            description = _clean_description(description)
            if not name or not description:
                return
            key = name.lower()
            if key in seen:
                return
            seen.add(key)
            facts.append(
                {
                    "name": name,
                    "type": etype if etype in ENTITY_TYPES else "Other",
                    "description": description,
                }
            )

        # 1) 「X 改成/改为/换成 Y」→ 决策（含新值；改口语义的主要载体）
        for match in _ASSIGN_RE.finditer(turn):
            attr = _strip_prefixes(match.group("attr"), _LEADING_FILLERS)
            attr = attr.strip("，。；：:、,.!?！？\"'`“”‘’ ")
            value = match.group("val").strip()
            if not attr or not value:
                continue
            etype = "Preference" if re.search(r"喜欢|偏好|口味|风格|习惯", attr) else "Decision"
            value_text = value[:40]
            add(attr, etype, f"{attr}已改为{value_text}")

        # 2) 喜好 / 厌恶 → Preference
        for match in _PREF_VERB_RE.finditer(turn):
            obj = _clean_obj(match.group("obj"))
            if not obj:
                continue
            negative = bool(match.group("neg"))
            add(obj, "Preference", f"用户{'不' if negative else ''}喜欢{obj}")

        # 3) 决定 / 计划 / 以后 / 不再 → Decision
        for match in _DECISION_RE.finditer(turn):
            obj = _clean_obj(match.group("obj"))
            if not obj:
                continue
            add(_guess_name(obj), "Decision", f"用户决定{obj[:120]}")

        # 4) 工具 / 技术栈 → Tool
        for match in _TOOL_RE.finditer(turn):
            tool = match.group("tool").strip(".,;:，。；：")
            if not tool or not re.match(r"[A-Za-z]", tool):
                continue
            add(tool, "Tool", f"用户使用{tool}")

        # 5) 「记住/记得/pin」→ 「记住」类事实
        for match in _REMEMBER_RE.finditer(turn):
            obj = _clean_obj(match.group("obj"))
            if not obj:
                continue
            add(_guess_name(obj), _guess_type(obj), f"用户要求记住：{obj[:160]}")

        # 6) 保守兜底：有项目知识线索才抽 Concept，绝不凭空造实体
        if not facts and len(turn) >= 8 and any(cue in turn for cue in _GENERIC_CUES):
            name = _guess_name(turn)
            if name:
                add(name, _guess_type(turn), turn[:200])

        return facts[:8]

    # -------------------------------------------------------------- summarize
    def summarize(self, text: str, max_tokens: int = 360) -> str:
        limit = max(8, int(max_tokens or 360))
        source = unwrap_marked_text(text)
        compact = normalize_space(source)
        if not compact:
            return ""
        if estimate_tokens(compact) <= limit:
            return compact
        # 抽取式：按句取到接近上限（确定性、无额外依赖）
        sentences = re.findall(r"[^。！？!?\n]+[。！？!?]?", source)
        out = ""
        for sentence in sentences:
            sentence = normalize_space(sentence)
            if not sentence:
                continue
            candidate = (out + sentence).strip()
            if estimate_tokens(candidate) <= limit:
                out = candidate
            elif not out:
                out = truncate_to_tokens(sentence, limit)
                break
            else:
                break
        if not out:
            out = truncate_to_tokens(compact, limit)
        # 双保险：截断到硬上限
        while estimate_tokens(out) > limit and out:
            out = truncate_to_tokens(out, max(1, estimate_tokens(out) - 1))
        return out

    # ------------------------------------------------------------------ judge
    def judge(self, before: str, after: str, context: str = "") -> dict[str, Any]:
        before_value = (before or "").strip()
        after_value = (after or "").strip()
        # llm_bridge 的 offline 分支以 judge("", "", prompt) 调用：从 prompt 的中文标签里取回
        # 「旧说法 / 新说法」；完全拿不到时按否定词判断（保证“现在不喜欢了”仍判矛盾）。
        if not before_value or not after_value:
            parsed_before, parsed_after = _parse_judge_prompt(context or "")
            before_value = before_value or parsed_before
            after_value = after_value or parsed_after
        fallback_only = not before_value and not after_value
        if fallback_only:
            after_value = context or ""

        before_text = normalize_space(before_value)
        after_text = normalize_space(after_value)
        context_text = normalize_space(context or "")

        negation = _has_negation(after_text)
        if fallback_only:
            # 拿不到新旧说法（极端降级）：按否定词判断，宁可报冲突让上层复核
            return {
                "is_same_attribute": negation,
                "is_contradiction": negation,
                "confidence": 0.82 if negation else 0.5,
            }
        common = _common_content_tokens(before_text, after_text)
        context_common = _common_content_tokens(before_text, context_text)
        same_attribute = bool(common) or bool(context_common and negation)

        before_numbers = set(re.findall(r"\d+(?:\.\d+)?", before_text))
        after_numbers = set(re.findall(r"\d+(?:\.\d+)?", after_text))
        value_changed = bool(before_numbers) and bool(after_numbers) and before_numbers != after_numbers

        contradiction = False
        if same_attribute:
            if negation:
                contradiction = True
            elif value_changed:
                contradiction = True
            elif _has_antonym(before_text, after_text):
                contradiction = True

        if contradiction:
            confidence = 0.82 if negation else 0.78
        elif same_attribute:
            confidence = 0.7
        else:
            confidence = 0.35
        return {
            "is_same_attribute": same_attribute,
            "is_contradiction": contradiction,
            "confidence": round(confidence, 2),
        }


offline_rules = _OfflineRules()


# --------------------------------------------------------------------------- #
# judge 辅助
# --------------------------------------------------------------------------- #
_JUDGE_OLD_RE = re.compile(r"(?:旧说法|原说法|之前的?说法|原值|旧值|before)\s*[:：]\s*(?P<value>[^\n]+)", re.IGNORECASE)
_JUDGE_NEW_RE = re.compile(
    r"(?:新说法|新值|现在的?说法|新内容|after)\s*[:：]\s*(?P<value>[^\n]+)", re.IGNORECASE
)


def _parse_judge_prompt(prompt: str) -> tuple[str, str]:
    """从 judge prompt 里取回「旧说法 / 新说法」（llm_bridge offline 兜底路径）。"""
    raw = prompt or ""
    old_match = _JUDGE_OLD_RE.search(raw)
    new_match = _JUDGE_NEW_RE.search(raw)
    before = old_match.group("value").strip() if old_match else ""
    after = new_match.group("value").strip() if new_match else ""
    return before, after


_NEGATION_HINTS_RE = re.compile(
    r"不(?:太|再|是|喜欢|想|要|用|喝|吃|需要|支持|同意)?|别|取消|换成|改成|改回|没(?:有)?|"
    r"不再|不用|不需要|no longer|not anymore|instead|revert",
    re.IGNORECASE,
)

_FILLER_TOKENS = (
    "用户",
    "我们",
    "本人",
    "现在",
    "目前",
    "已经",
    "最近",
    "一直",
    "平时",
    "通常",
    "一般",
    "非常",
    "特别",
    "比较",
    "真的",
    "其实",
    "应该",
    "可以",
    "这个",
    "那个",
    "的话",
    "然后",
    "还有",
    "就是",
    "知道",
)


def _has_negation(text: str) -> bool:
    from base import has_negation_marker  # noqa: PLC0415 — 保持模块只依赖 base

    return bool(_NEGATION_HINTS_RE.search(text or "")) or has_negation_marker(text or "")


def _content_tokens(text: str) -> set[str]:
    value = text or ""
    for filler in _FILLER_TOKENS:
        value = value.replace(filler, " ")
    value = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", value)
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{1,}", value.lower()):
        tokens.add(word)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", value):
        for index in range(len(chunk) - 1):
            tokens.add(chunk[index : index + 2])
        if len(chunk) == 1:
            tokens.add(chunk)
    return tokens


def _common_content_tokens(left: str, right: str) -> set[str]:
    return _content_tokens(left) & _content_tokens(right)


_ANTONYM_PAIRS = (
    ("喜欢", "讨厌"),
    ("喜欢", "不喜欢"),
    ("支持", "反对"),
    ("开启", "关闭"),
    ("打开", "关闭"),
    ("增加", "减少"),
    ("允许", "禁止"),
    ("保留", "删除"),
    ("通过", "拒绝"),
    ("用", "不用"),
    ("要", "不要"),
)


def _has_antonym(before: str, after: str) -> bool:
    for left, right in _ANTONYM_PAIRS:
        if (left in before and right in after) or (right in before and left in after):
            return True
    return False


# --------------------------------------------------------------------------- #
# LLM 输出解析（健壮：markdown 围栏 / 首尾括号 / 尾逗号 / dict 包装）
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


def _strip_code_fence(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value).strip()
    return value


def _loads_lenient(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        repaired = re.sub(r",\s*([\]}])", r"\1", payload)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, TypeError):
            return None


def _coerce_item_list(data: Any) -> Optional[list[dict[str, Any]]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if any(key in data for key in ("name", "type", "description")):
            return [data]
        for key in ("memories", "entities", "items", "facts", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in data.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return None


def parse_memories(raw: Any) -> Optional[list[dict[str, Any]]]:
    """解析 LLM 输出；返回 None 表示「不是合法记忆 JSON」，[] 表示「合法但无记忆」。"""
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return _coerce_item_list(raw)
    text = _strip_code_fence(str(raw))
    if not text:
        return None
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start != -1 and array_end > array_start:
        parsed = _loads_lenient(text[array_start : array_end + 1])
        coerced = _coerce_item_list(parsed)
        if coerced is not None:
            return coerced
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start != -1 and object_end > object_start:
        parsed = _loads_lenient(text[object_start : object_end + 1])
        coerced = _coerce_item_list(parsed)
        if coerced is not None:
            return coerced
    return None


_TYPE_ALIASES = {
    "preference": "Preference",
    "偏好": "Preference",
    "偏好类": "Preference",
    "decision": "Decision",
    "决定": "Decision",
    "决策": "Decision",
    "concept": "Concept",
    "概念": "Concept",
    "tool": "Tool",
    "工具": "Tool",
    "project": "Project",
    "项目": "Project",
    "person": "Person",
    "人": "Person",
    "人物": "Person",
    "organization": "Organization",
    "org": "Organization",
    "组织": "Organization",
    "机构": "Organization",
    "location": "Location",
    "地点": "Location",
    "位置": "Location",
    "event": "Event",
    "事件": "Event",
    "workflow": "Workflow",
    "流程": "Workflow",
    "工作流": "Workflow",
    "data": "Data",
    "数据": "Data",
    "fact": "Data",
    "other": "Other",
    "其他": "Other",
}


def normalize_type(value: Any) -> str:
    text = normalize_space(str(value or "")).lower()
    if not text:
        return "Other"
    for etype in ENTITY_TYPES:
        if text == etype.lower():
            return etype
    return _TYPE_ALIASES.get(text, "Other")


def normalize_items(items: Any, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = normalize_space(str(item.get("name") or item.get("entity") or ""))[:120]
        description = _clean_description(str(item.get("description") or item.get("desc") or ""))
        if not name and description:
            name = _guess_name(description)
        if not name:
            continue
        if not description:
            description = name
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "type": normalize_type(item.get("type")), "description": description})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# LLM 调用（async，任何异常都降级）
# --------------------------------------------------------------------------- #
async def _complete(llm: Any, prompt: str, *, system: str, max_tokens: int, purpose: str) -> Optional[str]:
    if llm is None:
        return None
    try:
        result = llm.complete(prompt, system=system, max_tokens=max_tokens, purpose=purpose)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return None
        return str(result)
    except Exception as exc:  # noqa: BLE001 — LLM 故障必须降级而非中断
        log("warning", "llm extract call failed", purpose=purpose, error=str(exc))
        return None


async def extract_memories(turn_text: str, *, scope: str, llm: Any) -> list[dict[str, Any]]:
    """从回合原文抽取记忆实体；返回 [{"name","type","description"}]。

    LLM 输出非法 JSON 时重试一次；仍失败/异常则使用 offline_rules 确定性降级。
    """
    text = (turn_text or "").strip()
    if len(text) < 4:
        return []
    text = text[:12000]
    prompt = build_extract_prompt(text, scope)

    for attempt in range(2):
        raw = await _complete(llm, prompt, system=EXTRACT_SYSTEM, max_tokens=1024, purpose="extract")
        if raw is not None:
            parsed = parse_memories(raw)
            if parsed is not None:
                normalized = normalize_items(parsed)
                if normalized:
                    return normalized
                # LLM 合法返回空数组：先用确定性规则兜底一次，仍为空则尊重空结果
                break
        if attempt == 0:
            prompt += "\n\n注意：上次输出不是合法 JSON 数组。请只输出 JSON 数组本身，不要任何解释。"

    try:
        fallback = offline_rules.extract(text, scope)
        return normalize_items(fallback)
    except Exception as exc:  # noqa: BLE001
        log("warning", "offline extract fallback failed", error=str(exc))
        return []


__all__ = [
    "EXTRACT_SYSTEM",
    "build_extract_prompt",
    "extract_memories",
    "normalize_items",
    "normalize_type",
    "offline_rules",
    "parse_memories",
    "unwrap_marked_text",
]
