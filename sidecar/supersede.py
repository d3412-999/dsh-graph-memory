"""改口（CONTRACT §2.4 / §4）—— 最新为准、静默替换，严格六步。

- `judge_supersede`：LLM 判 `{is_same_attribute,is_contradiction,confidence}`；
  JSON 解析失败 → `extract.offline_rules.judge` 确定性降级，绝不抛异常。
- `apply_supersede`：严格按序执行六步（缺一步即失败）：
  1) edit_entity(description := 新说法)
  2) valid_from := today，updated_at := now
  3) w := max(当前衰减值, 旧 w)（绝不清零）
  4) 同步重算向量库条目（edit_entity 已同步；再显式 reindex 一次，R6）
  5) Journal 追加 {before, after, date, confidence, source_turn_id}（7 天内可回滚）
  6) protect_until := now + 7d（期间不参与剪枝）
- `sweep_conflicts`：扫活跃节点矛盾对，只产出 conflict_report，不改图。
"""
from __future__ import annotations

import inspect
import json
import re
import time
from typing import Any, Optional

from base import (
    SECONDS_PER_WEEK,
    STATUS_ACTIVE,
    GraphMemoryError,
    has_negation_marker,
    import_offline_rules,
    log,
    normalize_space,
    today_str,
)

CONFIDENCE_THRESHOLD = 0.7
NEGATION_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_MIN_SIM = 0.62  # 写入时改口候选窗口（契约 §2.4）
SWEEP_MIN_SIM = 0.4  # 矛盾扫描召回窗口（比写入更宽，只报告不改图）
PROTECT_SECONDS = SECONDS_PER_WEEK

JUDGE_SYSTEM = (
    "你是记忆冲突裁决器。只输出严格 JSON 对象，不要 markdown、不要解释。"
    '对象形如 {"is_same_attribute": true|false, "is_contradiction": true|false, "confidence": 0到1的小数}。'
)


# --------------------------------------------------------------------------- #
# 服务对象取用（MemoryService：service.graphs[scope] / journals[scope] / now()）
# --------------------------------------------------------------------------- #
def _service_now(service: Any) -> float:
    for attr in ("now", "clock"):
        value = getattr(service, attr, None)
        if callable(value):
            try:
                return float(value())
            except TypeError:
                continue
        if isinstance(value, (int, float)):
            return float(value)
    return time.time()


def _service_graph(service: Any, scope: str) -> Any:
    graphs = getattr(service, "graphs", None)
    graph = None
    if isinstance(graphs, dict):
        graph = graphs.get(scope)
        if graph is None:
            for candidate in graphs.values():
                if getattr(candidate, "scope", None) == scope:
                    graph = candidate
                    break
        if graph is None and graphs:
            graph = next(iter(graphs.values()))
    if graph is None:
        graph = getattr(service, "graph", None)
    if graph is None:
        raise GraphMemoryError(f"service has no graph for scope={scope}", code="graph_unavailable")
    return graph


def _service_journal(service: Any, scope: str) -> Any:
    journals = getattr(service, "journals", None)
    journal = None
    if isinstance(journals, dict):
        journal = journals.get(scope)
        if journal is None and journals:
            journal = next(iter(journals.values()))
    if journal is None:
        journal = getattr(service, "journal", None)
    if journal is None:
        raise GraphMemoryError(f"service has no journal for scope={scope}", code="journal_unavailable")
    return journal


# --------------------------------------------------------------------------- #
# 否定 / 阈值
# --------------------------------------------------------------------------- #
def detect_negation(text: str) -> bool:
    """明确否定/改口词检测（只作加权信号，最终由 judge 判定 — 契约 §2.4）。"""
    return bool(has_negation_marker(text or ""))


def confidence_threshold_for(new_value: str) -> float:
    """含明确否定/改口词时阈值 0.6，否则 0.7。"""
    return NEGATION_CONFIDENCE_THRESHOLD if detect_negation(new_value) else CONFIDENCE_THRESHOLD


def should_supersede(
    verdict: Any,
    new_value: str = "",
    *,
    confidence_threshold: Optional[float] = None,
    negation_confidence_threshold: Optional[float] = None,
) -> bool:
    """按契约阈值判断 verdict 是否触发改写（service 可直接复用）。"""
    if not isinstance(verdict, dict):
        return False
    if not verdict.get("is_same_attribute") or not verdict.get("is_contradiction"):
        return False
    threshold = (
        negation_confidence_threshold if negation_confidence_threshold is not None else NEGATION_CONFIDENCE_THRESHOLD
    )
    if not detect_negation(new_value):
        threshold = confidence_threshold if confidence_threshold is not None else CONFIDENCE_THRESHOLD
    try:
        confidence = float(verdict.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence >= threshold


# --------------------------------------------------------------------------- #
# LLM 裁决
# --------------------------------------------------------------------------- #
def build_judge_prompt(before_description: str, new_value: str, context: str = "") -> str:
    """注意：`旧说法/新说法` 标签是 offline 规则解析依据，勿改措辞。"""
    context_text = (context or "").strip()[:600]
    return (
        "判断下面的「新说法」是否在讲与「旧说法」同一属性，以及是否构成矛盾/改口。\n"
        f"旧说法：{(before_description or '').strip()[:800]}\n"
        f"新说法：{(new_value or '').strip()[:800]}\n"
        f"上下文：{context_text}\n"
        "如果新说法明确否定、替换或修改了旧说法的取值/倾向，则 is_contradiction=true；"
        "如果只是在补充细节或讲不同属性，则 is_contradiction=false。\n"
        '只输出 JSON 对象：{"is_same_attribute": true|false, "is_contradiction": true|false, '
        '"confidence": 0到1的小数}'
    )


_BOOL_TRUE = {"true", "1", "yes", "y", "是", "对", "正确"}
_BOOL_FALSE = {"false", "0", "no", "n", "否", "不是", "错误"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
    return default


def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str) and value.strip().endswith("%"):
            try:
                confidence = float(value.strip()[:-1]) / 100.0
            except ValueError:
                return default
        else:
            return default
    if confidence > 1.0 and confidence <= 100.0:
        confidence /= 100.0
    return max(0.0, min(1.0, confidence))


def _coerce_verdict(data: Any) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    same_key = "is_same_attribute" if "is_same_attribute" in data else "same_attribute"
    contra_key = "is_contradiction" if "is_contradiction" in data else "contradiction"
    has_any = any(key in data for key in (same_key, contra_key, "confidence"))
    if not has_any:
        return None
    return {
        "is_same_attribute": _coerce_bool(data.get(same_key)),
        "is_contradiction": _coerce_bool(data.get(contra_key)),
        "confidence": _coerce_confidence(data.get("confidence"), 0.0),
    }


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value).strip()
    return value


def parse_verdict(raw: Any) -> Optional[dict[str, Any]]:
    """健壮解析 LLM 裁决：markdown 围栏 / 首尾花括号 / 键值文本兜底。"""
    if isinstance(raw, dict):
        return _coerce_verdict(raw)
    if raw is None:
        return None
    text = _strip_fence(str(raw))
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        payload = re.sub(r",\s*([\]}])", r"\1", text[start : end + 1])
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        verdict = _coerce_verdict(parsed)
        if verdict is not None:
            return verdict
    # 非 JSON 但含布尔/置信度的文本兜底（如 "is_same_attribute: true, confidence: 0.8"）
    flags: dict[str, bool] = {}
    for key in ("is_same_attribute", "is_contradiction"):
        match = re.search(rf'"?{key}"?\s*[:=：]\s*(true|false|yes|no|是|否|1|0)', text, re.IGNORECASE)
        if match:
            flags[key] = _coerce_bool(match.group(1))
    if flags:
        confidence_match = re.search(r'"?confidence"?\s*[:=：]\s*([0-9]+(?:\.[0-9]+)?%?)', text, re.IGNORECASE)
        return {
            "is_same_attribute": flags.get("is_same_attribute", False),
            "is_contradiction": flags.get("is_contradiction", False),
            "confidence": _coerce_confidence(confidence_match.group(1) if confidence_match else 0.0),
        }
    return None


def _offline_judge(before: str, after: str, context: str = "") -> dict[str, Any]:
    try:
        rules = import_offline_rules()
        verdict = _coerce_verdict(rules.judge(before, after, context))
        if verdict is not None:
            return verdict
    except Exception as exc:  # noqa: BLE001
        log("warning", "offline judge fallback failed", error=str(exc))
    negation = detect_negation(after)
    return {
        "is_same_attribute": negation,
        "is_contradiction": negation,
        "confidence": 0.75 if negation else 0.0,
    }


async def judge_supersede(
    before_description: str,
    new_value: str,
    context: str = "",
    llm: Any = None,
) -> dict[str, Any]:
    """判断新说法是否改写了旧说法（async；任何 LLM 故障都降级到 offline 规则）。"""
    before = (before_description or "").strip()
    after = (new_value or "").strip()
    if not after:
        return {"is_same_attribute": False, "is_contradiction": False, "confidence": 0.0}

    raw: Any = None
    if llm is not None:
        prompt = build_judge_prompt(before, after, context)
        try:
            result = llm.complete(prompt, system=JUDGE_SYSTEM, max_tokens=256, purpose="judge")
            if inspect.isawaitable(result):
                result = await result
            raw = result
        except Exception as exc:  # noqa: BLE001 — 改口判定失败必须降级
            log("warning", "llm judge call failed; fallback to offline rules", error=str(exc))
            raw = None

    verdict = parse_verdict(raw)
    if verdict is None:
        verdict = _offline_judge(before, after, context)
    return verdict


# --------------------------------------------------------------------------- #
# Journal 写入（签名以 Lead 约定为准；极窄 TypeError 兜底兼容 dict 单参实现）
# --------------------------------------------------------------------------- #
def _log_change(
    journal: Any,
    scope: str,
    node_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    confidence: float,
    source_turn_id: str,
    date: str,
) -> Any:
    logger = getattr(journal, "log_change", None)
    if not callable(logger):
        raise GraphMemoryError("journal has no log_change", code="journal_unavailable")
    try:
        return logger(
            scope,
            node_id,
            "supersede",
            before=before,
            after=after,
            confidence=confidence,
            source_turn_id=source_turn_id,
        )
    except TypeError:
        entry = {
            "scope": scope,
            "target_id": node_id,
            "kind": "supersede",
            "before": before,
            "after": after,
            "confidence": confidence,
            "source_turn_id": source_turn_id,
            "date": date,
        }
        fallback = journal.log_change
        try:
            return fallback(entry)
        except TypeError:
            return fallback(**entry)


# --------------------------------------------------------------------------- #
# 严格六步改写
# --------------------------------------------------------------------------- #
def _current_w(node: dict[str, Any], now: float) -> float:
    """惰性衰减后的当前权重（契约 §2.1：w(t)=min(10, w_last * 2^(-Δt/7d))）。"""
    props = (node or {}).get("properties") or {}
    try:
        w_last = float(props.get("w") if props.get("w") is not None else 1.0)
    except (TypeError, ValueError):
        w_last = 1.0
    last = props.get("last_activated_at") or props.get("updated_at") or props.get("created_at")
    try:
        age = max(0.0, float(now) - float(last)) if last is not None else 0.0
    except (TypeError, ValueError):
        age = 0.0
    decayed = w_last * (2.0 ** (-age / SECONDS_PER_WEEK))
    return min(10.0, decayed)


async def apply_supersede(
    service: Any,
    node: dict[str, Any],
    new_value: str,
    *,
    confidence: float,
    source_turn_id: str,
    scope: str,
) -> dict[str, Any]:
    """严格六步执行改口；任一必需步骤缺失/失败即抛 GraphMemoryError。"""
    new_description = (new_value or "").strip()
    if not new_description:
        raise GraphMemoryError("new_value is required", code="invalid_params")
    graph = _service_graph(service, scope)
    journal = _service_journal(service, scope)
    node_id = str((node or {}).get("id") or "")
    if not node_id:
        raise GraphMemoryError("node.id is required", code="invalid_params")
    current = graph.get_entity(node_id)
    if current is None:
        raise GraphMemoryError(f"entity not found: {node_id}", code="entity_not_found")

    now = _service_now(service)
    today = today_str(now)
    before_props = dict(current.get("properties") or {})
    before = {"description": current.get("description", ""), "properties": dict(before_props)}
    old_w = float(before_props.get("w") if before_props.get("w") is not None else 1.0)
    w_after = max(old_w, _current_w(current, now))

    try:
        # 1) description := 新说法（edit_entity 内部同步重算向量条目 — R6）
        graph.edit_entity(node_id, description=new_description)
        # 2) valid_from := today，updated_at := now
        graph.update_properties(node_id, {"valid_from": today, "updated_at": now})
        # 3) w := max(当前衰减值, 旧 w)，绝不清零
        graph.update_properties(node_id, {"w": w_after})
        # 4) 同步重算向量库条目（显式再索引一次，确保 R6）
        graph.edit_entity(node_id)
        # 5) Journal 追加 before/after（7 天内可回滚）
        latest = graph.get_entity(node_id) or {}
        after = {"description": latest.get("description", ""), "properties": dict(latest.get("properties") or {})}
        _log_change(
            journal,
            scope,
            node_id,
            before,
            after,
            float(confidence),
            str(source_turn_id or ""),
            today,
        )
        # 6) protect_until := now + 7d（期间不参与剪枝）
        protect_until = now + PROTECT_SECONDS
        graph.update_properties(node_id, {"protect_until": protect_until})
    except GraphMemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 — 六步中任何一步失败都视为改口失败
        raise GraphMemoryError(f"supersede failed at strict step: {exc}", code="supersede_failed") from exc

    final = graph.get_entity(node_id) or {}
    return {"node_id": node_id, "before": before, "after": {"description": final.get("description", ""), "properties": dict(final.get("properties") or {})}}


# --------------------------------------------------------------------------- #
# 矛盾扫描（只报告，不自动改）
# --------------------------------------------------------------------------- #
async def sweep_conflicts(service: Any, scope: str, days: int = 30) -> dict[str, Any]:
    """扫描活跃节点矛盾对，产出 conflict_report；不修改图谱。"""
    graph = _service_graph(service, scope)
    now = _service_now(service)
    window = float(days) * 86400.0 if days and float(days) > 0 else None

    try:
        nodes = graph.list_entities(scope, status=STATUS_ACTIVE)
    except TypeError:
        nodes = [n for n in graph.list_entities(scope) if (n.get("properties") or {}).get("status") == STATUS_ACTIVE]
    if window is not None and nodes:
        recent = []
        for item in nodes:
            props = item.get("properties") or {}
            updated = props.get("updated_at") or props.get("created_at") or 0
            try:
                if float(updated) >= now - window:
                    recent.append(item)
            except (TypeError, ValueError):
                recent.append(item)
        if len(recent) >= 2:
            nodes = recent

    min_sim = SWEEP_MIN_SIM
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    checked = 0
    conflicts = 0
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        description = str(node.get("description") or "")
        query = f"{node.get('name', '')}\n{description}".strip()
        try:
            neighbors = graph.search_entities(query, scope, top_k=6)
        except Exception as exc:  # noqa: BLE001 — 单节点检索失败跳过
            log("warning", "conflict sweep search failed", node_id=node_id, error=str(exc))
            continue
        for other_id, sim in neighbors:
            other_id = str(other_id)
            if other_id == node_id or float(sim) < min_sim:
                continue
            pair_key = tuple(sorted((node_id, other_id)))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            other = graph.get_entity(other_id)
            if other is None:
                continue
            checked += 1
            verdict = _offline_judge(description, str(other.get("description") or ""), "")
            if (
                verdict.get("is_same_attribute")
                and verdict.get("is_contradiction")
                and float(verdict.get("confidence") or 0.0) >= NEGATION_CONFIDENCE_THRESHOLD
            ):
                conflicts += 1
                pairs.append(
                    {
                        "a": node_id,
                        "b": other_id,
                        "a_name": node.get("name", ""),
                        "b_name": other.get("name", ""),
                        "sim": round(float(sim), 4),
                        "verdict": verdict,
                    }
                )
    return {
        "scope": scope,
        "pairs": pairs,
        "checked": checked,
        "conflicts": conflicts,
        "days": int(days or 0),
        "generated_at": now,
    }


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "NEGATION_CONFIDENCE_THRESHOLD",
    "apply_supersede",
    "build_judge_prompt",
    "confidence_threshold_for",
    "detect_negation",
    "judge_supersede",
    "parse_verdict",
    "should_supersede",
    "sweep_conflicts",
]
