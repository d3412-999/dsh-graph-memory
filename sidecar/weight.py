"""权重模型：读时惰性衰减 / 激活增强 / 综合打分（docs/CONTRACT.md §2.1 唯一实现）。

契约条款映射（本模块只读写 ``properties``，永不触碰 ``description`` — R3）：
- 7 天半衰期惰性衰减（§2.1）::

      w(t) = min(max_weight, w_last * 2 ** (-(t - last_activated_at) / 604800))

  只在读取时计算，不后台遍历图；时间戳缺失/非法时按“不衰减”处理。
- 激活（R5：只允许对注入的 ``injected_ids`` 调用）::

      boost = 1.0*pinned + 1.0*tool_boosted + 0.2*workspace_hit
      w = min(max_weight, decayed + 0.5 * (1 + boost))
      last_activated_at = now
      status == compressed -> active（复活）

- 排序（§2.1）::

      score = sim * (0.3 + 0.7 * min(1, w/5))
      if pinned: score *= 1.5
      if scope == "workspace": score *= 1.2

- 新建 3 天保护（§2.1/§2.6）：``now - created_at < 3d`` 的节点不参与压缩/归档。
- 改口权重保护（``w = max(w_old, w_new)``、``protect_until = now + 7d``）由
  ``supersede.py`` 执行；本模块只提供 ``supersede_protect_until`` 常量辅助。

本模块只 import ``base`` 与标准库；不 import 其他实现模块（契约 §4）。
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from base import (
    SECONDS_PER_DAY,
    SECONDS_PER_WEEK,
    SCOPE_WORKSPACE,
    STATUS_ACTIVE,
    STATUS_COMPRESSED,
)

# --------------------------------------------------------------------------- #
# 契约常量（config/default.json 的 weight.* 默认值与此一致）
# --------------------------------------------------------------------------- #
HALF_LIFE_SECONDS: float = SECONDS_PER_WEEK  # 604800 = 7 天半衰期
MAX_WEIGHT: float = 10.0
DEFAULT_WEIGHT: float = 1.0
BASE_ACTIVATION_GAIN: float = 0.5
PINNED_BOOST: float = 1.0
TOOL_BOOST: float = 1.0
WORKSPACE_HIT_BOOST: float = 0.2
PINNED_SCORE_MULTIPLIER: float = 1.5
WORKSPACE_SCORE_MULTIPLIER: float = 1.2
NEW_PROTECT_SECONDS: float = 3 * SECONDS_PER_DAY  # 259200 = 3 天
SUPERSEDE_PROTECT_SECONDS: float = SECONDS_PER_WEEK  # 改口保护 7 天
SCORE_FLOOR: float = 0.3
SCORE_WEIGHT_SPAN: float = 0.7
SCORE_WEIGHT_SATURATION: float = 5.0


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _properties(node: Any) -> dict[str, Any]:
    """安全读取节点 properties（图谱节点是 dict 规范，见 base.make_entity）。"""
    if not isinstance(node, dict):
        return {}
    props = node.get("properties")
    return props if isinstance(props, dict) else {}


def _as_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, default: float) -> float:
    parsed = _as_float(value, default)
    if parsed is None or parsed <= 0:
        return default
    return parsed


# --------------------------------------------------------------------------- #
# §2.1 衰减 / 读取
# --------------------------------------------------------------------------- #
def decayed(
    w_last: float,
    last_activated_at: float,
    now: float,
    half_life_seconds: float = HALF_LIFE_SECONDS,
) -> float:
    """读时惰性衰减：``min(10, w_last * 2 ** (-(now - last_activated_at)/半衰期))``。

    边界（实现为契约的严格子集，避免浮点/脏数据放大权重）：
    - ``w_last <= 0`` 或非法 → ``0.0``；
    - 时间戳缺失/非法、``now <= last_activated_at``（时钟回拨）→ 不衰减（封顶 max_weight），
      绝不允许“未来时间戳”把权重抬高；
    - 半衰期非法/非正 → 退化为不衰减。
    """
    base = max(0.0, _as_float(w_last, 0.0) or 0.0)
    if base <= 0.0:
        return 0.0
    last = _as_float(last_activated_at, None)
    now_value = _as_float(now, None)
    half_life = _positive_float(half_life_seconds, HALF_LIFE_SECONDS)
    if last is None or now_value is None:
        return min(MAX_WEIGHT, base)
    elapsed = now_value - last
    if elapsed <= 0:
        return min(MAX_WEIGHT, base)
    factor = 2.0 ** (-elapsed / half_life)
    return min(MAX_WEIGHT, base * factor)


def current_w(node: dict[str, Any], now: float) -> float:
    """节点当前权重（读时衰减，不写回；§2.1）。"""
    props = _properties(node)
    w_last = _as_float(props.get("w"), DEFAULT_WEIGHT)
    return decayed(w_last if w_last is not None else DEFAULT_WEIGHT, props.get("last_activated_at"), now)


def is_new_protected(
    node: dict[str, Any],
    now: float,
    new_protect_seconds: float = NEW_PROTECT_SECONDS,
) -> bool:
    """新建 3 天保护：``now - created_at < 3d`` 时节点不参与压缩/归档（§2.1/§2.6）。"""
    created = _as_float(_properties(node).get("created_at"), None)
    now_value = _as_float(now, None)
    if created is None or now_value is None:
        return False
    protect_seconds = _positive_float(new_protect_seconds, NEW_PROTECT_SECONDS)
    return (now_value - created) < protect_seconds


# --------------------------------------------------------------------------- #
# §2.1 激活
# --------------------------------------------------------------------------- #
def boost_for(
    node: dict[str, Any],
    *,
    tool_boosted: bool = False,
    workspace_hit: bool = False,
    pinned_boost: float = PINNED_BOOST,
    tool_boost: float = TOOL_BOOST,
    workspace_boost: float = WORKSPACE_HIT_BOOST,
) -> float:
    """激活 boost：``pinned_boost*已 pin + tool_boost*工具命中 + workspace_boost*workspace 命中``。

    pinned 状态来自节点 ``properties.pinned``（真相在 properties — R3）。
    """
    props = _properties(node)
    boost = 0.0
    if props.get("pinned"):
        boost += float(pinned_boost)
    if tool_boosted:
        boost += float(tool_boost)
    if workspace_hit:
        boost += float(workspace_boost)
    return boost


def activate(
    node: dict[str, Any],
    now: float,
    *,
    pinned: bool = False,
    tool_boosted: bool = False,
    workspace_hit: bool = False,
    max_weight: float = MAX_WEIGHT,
    gain: float = BASE_ACTIVATION_GAIN,
) -> dict[str, Any]:
    """激活一个节点，返回**新的完整 properties dict**（不改传入 node，§2.1）。

    仅允许对注入的 ``injected_ids`` 调用（R5）；写回 ``last_activated_at=now``；
    ``status == compressed`` 时复活为 ``active``（不改 archived/pending_forget，
    这两类状态由 prune/forget 流程单独管理）。
    """
    props: dict[str, Any] = copy.deepcopy(_properties(node))
    now_value = float(now)
    cap = _positive_float(max_weight, MAX_WEIGHT)
    gain_value = _as_float(gain, BASE_ACTIVATION_GAIN)
    w_prev = decayed(
        props.get("w", DEFAULT_WEIGHT),
        props.get("last_activated_at"),
        now_value,
    )
    boost = boost_for(
        node,
        tool_boosted=tool_boosted,
        workspace_hit=workspace_hit,
    )
    if pinned:
        boost += PINNED_BOOST
    w_new = min(cap, w_prev + (gain_value or 0.0) * (1.0 + boost))
    props["w"] = w_new
    props["last_activated_at"] = now_value
    if pinned:
        props["pinned"] = True
    if workspace_hit:
        props["workspace_hit"] = True
    if props.get("status") == STATUS_COMPRESSED:
        props["status"] = STATUS_ACTIVE
    return props


# --------------------------------------------------------------------------- #
# §2.1 综合打分
# --------------------------------------------------------------------------- #
def score(
    sim: float,
    node: dict[str, Any],
    now: float,
    scope: str,
    *,
    pinned_multiplier: float = PINNED_SCORE_MULTIPLIER,
    workspace_multiplier: float = WORKSPACE_SCORE_MULTIPLIER,
) -> float:
    """综合排序分：``sim * (0.3 + 0.7*min(1, w/5))`` → pinned ×1.5 → workspace ×1.2。"""
    sim_value = _as_float(sim, 0.0) or 0.0
    if sim_value <= 0.0:
        return 0.0
    w = current_w(node, now)
    factor = SCORE_FLOOR + SCORE_WEIGHT_SPAN * min(1.0, max(0.0, w / SCORE_WEIGHT_SATURATION))
    result = sim_value * factor
    if _properties(node).get("pinned"):
        result *= _positive_float(pinned_multiplier, PINNED_SCORE_MULTIPLIER)
    if scope == SCOPE_WORKSPACE:
        result *= _positive_float(workspace_multiplier, WORKSPACE_SCORE_MULTIPLIER)
    return float(result)


def supersede_protect_until(now: float, days: float = 7.0) -> float:
    """改口保护期（§2.1/§2.4）：``protect_until = now + 7d``，期间不参与剪枝。"""
    return float(now) + float(days) * SECONDS_PER_DAY
