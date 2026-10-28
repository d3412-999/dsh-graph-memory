"""双路召回、排序与动态预算统计（docs/CONTRACT.md §2.1/§2.2/§3.1）。

``retrieve(service, query, scopes, ...)`` 供 ``service.py`` 的 JSON-RPC ``retrieve`` 调用：

- 双路召回：① 实体向量相似度（``graph.search_entities``）；② 关系一层扩展
  （``graph.expand_related``，折扣分再乘 ``RELATION_DISCOUNT``）。user/workspace 两 scope 合并。
- 排序使用 ``weight.score``（§2.1）：``sim*(0.3+0.7*min(1,w/5))``，pinned ×1.5，
  workspace scope ×1.2；同一实体跨 scope 按规范化 name 去重，保留综合分高者。
- 默认排除 ``archived``（显式 ``memory_recall`` → ``explicit=True`` 时包含）。
- 返回：``{"items": [...], "top1_sim": float, "high_weight_count": int}``；
  items 已按 score 降序、``top_k`` 截断。``top1_sim``/``high_weight_count`` 供 TS 动态
  memory 预算（§2.2：top1>0.8 → 1536；命中 pin 或 w≥5 → 1792）。
- ``mem_max``（可选）传入时按注入行 token 截断并返回 ``tokens.memory``；
  预算截断严格不超限（优先整条保留，首条超限时按 token 截断其描述）。
- 降级精神（R7）：单 scope 后端不可用（``GraphBackendUnavailable``）只跳过该 scope 并记
  warning，绝不让检索抛错阻塞对话。
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional, Sequence

from base import (
    GraphMemoryError,
    SECONDS_PER_DAY,
    STATUS_ARCHIVED,
    Config,  # noqa: F401  (类型/文档用途)
    estimate_tokens,
    log,
    normalize_space,
    now_ts,
    truncate_to_tokens,
)
from weight import current_w
from weight import score as weight_score
import budget as budget_mod

DEFAULT_TOP_K = 20
RELATION_DISCOUNT = 0.5


# --------------------------------------------------------------------------- #
# service 鸭子类型访问（不 import service.py，避免循环依赖 — 契约 §4）
# --------------------------------------------------------------------------- #
def _cfg(config: Any, path: str, default: Any) -> Any:
    """同时支持 ``base.Config`` 与普通嵌套 dict 的配置读取。"""
    if config is None:
        return default
    if isinstance(config, dict):
        cur: Any = config
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(path, default)
        except Exception:  # noqa: BLE001 — 配置读取绝不能导致检索失败
            return default
        return default if value is None else value
    return default


def _service_graphs(service: Any) -> dict[str, Any]:
    graphs = getattr(service, "graphs", None)
    return graphs if isinstance(graphs, dict) else {}


def _service_config(service: Any) -> Any:
    return getattr(service, "config", None)


def _service_now(service: Any) -> float:
    fn = getattr(service, "now", None)
    if callable(fn):
        try:
            return float(fn())
        except Exception:  # noqa: BLE001
            pass
    elif isinstance(fn, (int, float)) and not isinstance(fn, bool):
        return float(fn)
    return float(now_ts())


def _normalize_scopes(scopes: Iterable[str] | str) -> list[str]:
    values = [scopes] if isinstance(scopes, str) else list(scopes or [])
    out: list[str] = []
    for scope in values:
        text = str(scope or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _props(node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("properties") if isinstance(node, dict) else None
    return props if isinstance(props, dict) else {}


def _is_archived(node: dict[str, Any]) -> bool:
    return _props(node).get("status") == STATUS_ARCHIVED


def _safe_get_entity(graph: Any, entity_id: str) -> Optional[dict[str, Any]]:
    try:
        node = graph.get_entity(entity_id)
    except GraphMemoryError:
        return None
    return node if isinstance(node, dict) else None


def _search_entities(graph: Any, query: str, scope: str, limit: int, explicit: bool) -> list[tuple[str, float]]:
    """调用 graph.search_entities；兼容不支持 include_archived 的鸭子类型实现。"""
    try:
        return list(graph.search_entities(query, scope, top_k=limit, include_archived=bool(explicit)))
    except TypeError:
        return list(graph.search_entities(query, scope, top_k=limit))


# --------------------------------------------------------------------------- #
# 召回
# --------------------------------------------------------------------------- #
def _recall_scope(
    graph: Any,
    scope: str,
    query: str,
    *,
    explicit: bool,
    search_limit: int,
    neighbor_k: int,
) -> dict[str, float]:
    """单 scope 双路召回：返回 {entity_id: sim}（关系扩展为折扣分）。"""
    candidates: dict[str, float] = {}
    try:
        hits = _search_entities(graph, query, scope, search_limit, explicit)
    except GraphMemoryError as exc:
        log("warning", "retrieve: entity search failed; skip scope", scope=scope, error=str(exc))
        return candidates
    for eid, sim in hits:
        node = _safe_get_entity(graph, eid)
        if node is None:
            continue
        if _is_archived(node) and not explicit:
            continue
        candidates[eid] = max(candidates.get(eid, 0.0), float(sim))
    # 第二路：关系扩展（用高相似度种子，扩展节点拿折扣分）
    seeds = [eid for eid, _sim in hits[: max(1, neighbor_k)]]
    if not seeds:
        return candidates
    try:
        related = list(graph.expand_related(seeds, top_k=max(1, neighbor_k * 2)))
    except GraphMemoryError as exc:
        log("warning", "retrieve: relation expansion failed", scope=scope, error=str(exc))
        return candidates
    for nid, discount in related:
        if nid in candidates:
            continue
        node = _safe_get_entity(graph, nid)
        if node is None:
            continue
        if _is_archived(node) and not explicit:
            continue
        candidates[nid] = max(candidates.get(nid, 0.0), float(discount) * RELATION_DISCOUNT)
    return candidates


# --------------------------------------------------------------------------- #
# 排序 / items 组装
# --------------------------------------------------------------------------- #
def _build_item(
    node: dict[str, Any],
    entity_id: str,
    scope: str,
    sim: float,
    now: float,
    *,
    pinned_multiplier: float,
    workspace_multiplier: float,
    recent_update_days: float,
) -> dict[str, Any]:
    props = _props(node)
    w = current_w(node, now)
    sc = weight_score(
        sim,
        node,
        now,
        scope,
        pinned_multiplier=pinned_multiplier,
        workspace_multiplier=workspace_multiplier,
    )
    updated_raw = props.get("updated_at")
    updated: Optional[float] = None
    if isinstance(updated_raw, (int, float)) and not isinstance(updated_raw, bool):
        updated = float(updated_raw)
    updated_label = time.strftime("%m-%d", time.localtime(updated)) if updated is not None else None
    recent = bool(updated is not None and (now - updated) <= float(recent_update_days) * SECONDS_PER_DAY)
    return {
        "id": entity_id,
        "text": node.get("description") or "",
        "w": round(float(w), 4),
        "valid_from": props.get("valid_from"),
        "updated": updated,
        "updated_at": updated,
        "updated_label": updated_label,
        "updated_recent": recent,
        # service._format_item 使用 recently_updated 渲染“已更新”标签；保留两个别名为同一语义
        "recently_updated": recent,
        "sim": round(float(sim), 6),
        "pinned": bool(props.get("pinned")),
        "scope": scope,
        "type": node.get("type"),
        "name": node.get("name"),
        "status": props.get("status"),
        "score": round(float(sc), 6),
    }


def _rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -float(item.get("score") or 0.0),
        -int(bool(item.get("pinned"))),
        -float(item.get("w") or 0.0),
        str(item.get("id") or ""),
    )


def _merge_and_rank(
    service: Any,
    query: str,
    scopes: Sequence[str],
    *,
    explicit: bool,
) -> tuple[list[dict[str, Any]], float, int]:
    """合并 user/workspace 双路召回并按 §2.1 打分排序（返回全量 ranked、top1_sim、high_weight_count）。"""
    graphs = _service_graphs(service)
    config = _service_config(service)
    now = _service_now(service)
    pinned_multiplier = float(_cfg(config, "weight.pinnedScoreMultiplier", 1.5))
    workspace_multiplier = float(_cfg(config, "injection.workspaceWeightMultiplier", 1.2))
    high_weight_threshold = float(_cfg(config, "injection.highWeightThreshold", 5.0))
    recent_update_days = float(_cfg(config, "injection.recentUpdateDays", 7.0))
    neighbor_k = int(_cfg(config, "graph.recentNeighborK", 5))
    search_limit = max(DEFAULT_TOP_K, int(_cfg(config, "graph.topK", DEFAULT_TOP_K)) * 3)

    merged: dict[str, dict[str, Any]] = {}
    for scope in scopes:
        graph = graphs.get(scope)
        if graph is None:
            continue
        candidates = _recall_scope(
            graph,
            scope,
            query,
            explicit=explicit,
            search_limit=search_limit,
            neighbor_k=neighbor_k,
        )
        for eid, sim in candidates.items():
            node = _safe_get_entity(graph, eid)
            if node is None:
                continue
            if _is_archived(node) and not explicit:
                continue
            item = _build_item(
                node,
                eid,
                scope,
                float(sim),
                now,
                pinned_multiplier=pinned_multiplier,
                workspace_multiplier=workspace_multiplier,
                recent_update_days=recent_update_days,
            )
            key = normalize_space(str(node.get("name") or eid)).lower() or str(eid)
            existing = merged.get(key)
            if existing is None or item["score"] > existing["score"]:
                # 同一实体跨 scope 去重（§任务描述）：保留综合分更高的一份
                merged[key] = item
    ranked = sorted(merged.values(), key=_rank_key)
    top1_sim = max((float(item["sim"]) for item in ranked), default=0.0)
    high_weight_count = sum(
        1
        for item in ranked
        if bool(item.get("pinned")) or float(item.get("w") or 0.0) >= high_weight_threshold
    )
    return ranked, top1_sim, high_weight_count


def entity_fallback_key(entity_id: str) -> str:
    return str(entity_id or "")


# --------------------------------------------------------------------------- #
# 注入预算（§2.2 / CONTRACT §5 注入行格式）
# --------------------------------------------------------------------------- #
def render_item_line(item: dict[str, Any]) -> str:
    """单条 memory item 的注入行（与 CONTRACT §5 格式一致，用于 token 预算估算）。"""
    label = item.get("updated_label") or "--"
    tags = ""
    if item.get("updated_recent"):
        tags += " | 已更新"
    if item.get("pinned"):
        tags += " | pinned"
    return f"[#{item.get('id')} | w={float(item.get('w') or 0.0):.1f} | {label} 更新{tags}] {item.get('text') or ''}"


def _trim_to_budget(items: list[dict[str, Any]], mem_max: int) -> tuple[list[dict[str, Any]], int]:
    """按注入行 token 截断 items（严格不超 mem_max）；首条超限时截断其描述。"""
    budget = max(0, int(mem_max))
    if budget <= 0:
        return [], 0
    kept: list[dict[str, Any]] = []
    used = 0
    for item in items:
        line = render_item_line(item)
        cost = estimate_tokens(line)
        if used + cost <= budget:
            kept.append(item)
            used += cost
            continue
        remaining = budget - used
        if remaining <= 0:
            break
        overhead = estimate_tokens(render_item_line({**item, "text": ""}))
        text_budget = max(0, remaining - overhead)
        if text_budget <= 0:
            break
        truncated = truncate_to_tokens(item.get("text") or "", text_budget).strip()
        if not truncated:
            break
        fitted = dict(item)
        fitted["text"] = truncated
        cost_fitted = estimate_tokens(render_item_line(fitted))
        if cost_fitted > remaining:
            break
        kept.append(fitted)
        used += cost_fitted
        break
    return kept, used


def dynamic_memory_cap(
    top1_sim: float,
    high_weight_count: int,
    explicit: bool = False,
    config: Any = None,
) -> int:
    """动态 memory token 上限（§2.2）：explicit 2048 > pin/w≥5 1792 > top1>0.8 1536 > 默认 1024。

    上限随「注入预算」等比缩放（预算默认 3000，可调至 2M）。
    """
    caps = budget_mod.injection_caps(config)
    default_cap = caps["memoryDefaultTokens"]
    high_sim_cap = caps["memoryHighSimTokens"]
    boost_cap = caps["memoryBoostTokens"]
    explicit_cap = caps["memoryExplicitTokens"]
    high_sim_threshold = float(_cfg(config, "injection.highSimThreshold", 0.8))
    if explicit:
        return explicit_cap
    if int(high_weight_count or 0) >= 1:
        return boost_cap
    if float(top1_sim or 0.0) > high_sim_threshold:
        return high_sim_cap
    return default_cap


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #
def retrieve(
    service: Any,
    query: str,
    scopes: Iterable[str] | str,
    *,
    explicit: bool = False,
    top_k: int = DEFAULT_TOP_K,
    mem_max: Optional[int] = None,
) -> dict[str, Any]:
    """检索入口（契约 §3.1 retrieve 的 Python 实现，service.py 调用）。

    - ``scopes``：``["user", "workspace"]`` 或单字符串；按给定顺序合并，重复 scope 去重；
    - ``explicit=True``：显式 ``memory_recall``，允许返回 archived 节点；
    - ``top_k``：返回条数上限（已按 score 降序）；
    - ``mem_max``：可选 memory token 上限；传入时额外返回 ``tokens: {"memory": int}``。
    """
    limit = int(top_k if top_k is not None else DEFAULT_TOP_K)
    if limit <= 0:
        return {"items": [], "top1_sim": 0.0, "high_weight_count": 0}
    scope_list = _normalize_scopes(scopes)
    ranked, top1_sim, high_weight_count = _merge_and_rank(
        service,
        query or "",
        scope_list,
        explicit=bool(explicit),
    )
    result: dict[str, Any] = {
        "items": ranked[:limit],
        "top1_sim": round(float(top1_sim), 6),
        "high_weight_count": int(high_weight_count),
    }
    if mem_max is not None:
        kept, used = _trim_to_budget(result["items"], int(mem_max))
        result["items"] = kept
        result["tokens"] = {"memory": int(used)}
    return result


def rank_candidates(
    service: Any,
    query: str,
    candidates: Sequence[tuple[str, str, float]],
    *,
    explicit: bool = False,
) -> list[dict[str, Any]]:
    """对 ``(scope, entity_id, sim)`` 候选（三元组顺序自动识别）统一打分排序，返回 item dicts。

    辅助 ``service.py`` 在已有候选（如 day detail/面板指定集合）上复用同一套排序规则。
    """
    graphs = _service_graphs(service)
    config = _service_config(service)
    now = _service_now(service)
    pinned_multiplier = float(_cfg(config, "weight.pinnedScoreMultiplier", 1.5))
    workspace_multiplier = float(_cfg(config, "injection.workspaceWeightMultiplier", 1.2))
    recent_update_days = float(_cfg(config, "injection.recentUpdateDays", 7.0))
    known_scopes = set(graphs.keys())
    items: list[dict[str, Any]] = []
    for entry in candidates or []:
        try:
            first, second, value = entry
        except (TypeError, ValueError):
            continue
        if first in known_scopes:
            scope, eid = str(first), str(second)
        elif second in known_scopes:
            scope, eid = str(second), str(first)
        else:
            scope, eid = str(first), str(second)
        graph = graphs.get(scope)
        if graph is None:
            continue
        node = _safe_get_entity(graph, eid)
        if node is None:
            continue
        if _is_archived(node) and not explicit:
            continue
        items.append(
            _build_item(
                node,
                eid,
                scope,
                float(value),
                now,
                pinned_multiplier=pinned_multiplier,
                workspace_multiplier=workspace_multiplier,
                recent_update_days=recent_update_days,
            )
        )
    items.sort(key=_rank_key)
    return items
