"""注入预算（可调）与每条消息成本估算。

红线 R1 从「硬顶固定 3000」放宽为「硬顶 = 用户设定值」：
- 取值区间 [3000, 2_000_000]，默认 3000（不低于 3000，避免把预算调到没有意义的量级）；
- 各容器上限按 total/3000 等比缩放，保持 system/memory/day 的结构比例不变；
- 设定值持久化到 `<userDataHome>/budget.json`，sidecar 重启后仍生效；
- 成本按 DeepSeek 官方单价估算（默认 deepseek-flash 高峰价，人民币）。

缩放只影响「上限」，不影响恒定语义：在给定预算下，注入总量仍与轮数/时长/图谱规模无关。
"""
from __future__ import annotations

import os
from typing import Any, Optional

BUDGET_MIN = 3000
BUDGET_MAX = 2_000_000
BUDGET_DEFAULT = 3000
BUDGET_FILE = "budget.json"

# 3000 预算下的基线容器（与 config/default.json、src/budgeter.ts 保持一致）
BASE_CAPS: dict[str, int] = {
    "systemMaxTokens": 128,
    "memoryMaxTokens": 2048,
    "memoryDefaultTokens": 1024,
    "memoryHighSimTokens": 1536,
    "memoryBoostTokens": 1792,
    "memoryExplicitTokens": 2048,
    "dayMaxTokens": 1024,
    "dayTextMaxTokens": 512,
    "detailMaxTokens": 312,
    "historyMaxTokens": 200,
}

# 当天缓冲/摘要同样跟着预算走，否则调大预算后当天原文仍会被 512 token 的旧水位提前压缩
DAY_BASE_CAPS: dict[str, int] = {
    "rawTokenLimit": 512,
    "summaryMaxTokens": 360,
}

# DeepSeek 官方价（人民币/百万 token）。默认取 deepseek-flash 高峰时段：
# 输入缓存未命中 ¥2、缓存命中 ¥0.04、输出 ¥8；空闲时段（周一至周五 9-12/14-18 之外）为半价。
DEFAULT_PRICING: dict[str, Any] = {
    "model": "deepseek-flash",
    "currency": "CNY",
    "inputCacheMissPerM": 2.0,
    "inputCacheHitPerM": 0.04,
    "outputPerM": 8.0,
    "tier": "peak",
    "note": "deepseek-flash 高峰价（北京时间周一至周五 9:00-12:00 / 14:00-18:00）；其余时段半价",
}


def _get(config: Any, path: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(path, default)
    cursor = config
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def clamp_budget(value: Any) -> int:
    """把任意输入收敛到 [3000, 2_000_000]；非法值退回默认 3000。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return BUDGET_DEFAULT
    return max(BUDGET_MIN, min(BUDGET_MAX, number))


def scale_factor(total: Any) -> float:
    return clamp_budget(total) / float(BUDGET_DEFAULT)


def total_budget(config: Any) -> int:
    return clamp_budget(_get(config, "injection.totalHardTokens", BUDGET_DEFAULT))


def injection_caps(config: Any, total: Optional[Any] = None) -> dict[str, int]:
    """按当前预算等比缩放后的各容器上限（预算=3000 时与基线完全一致）。"""
    resolved = clamp_budget(total if total is not None else total_budget(config))
    factor = resolved / float(BUDGET_DEFAULT)
    caps: dict[str, int] = {}
    for name, base in BASE_CAPS.items():
        override = _get(config, f"injection.{name}")
        value = base if override is None else override
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = base
        caps[name] = max(1, int(round(value * factor)))
    return caps


def day_caps(config: Any, total: Optional[Any] = None) -> dict[str, int]:
    """当天缓冲/摘要的 token 水位，随预算等比缩放。"""
    resolved = clamp_budget(total if total is not None else total_budget(config))
    factor = resolved / float(BUDGET_DEFAULT)
    caps: dict[str, int] = {}
    for name, base in DAY_BASE_CAPS.items():
        override = _get(config, f"day.{name}")
        value = base if override is None else override
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = base
        caps[name] = max(1, int(round(value * factor)))
    return caps


def pricing(config: Any) -> dict[str, Any]:
    data = dict(DEFAULT_PRICING)
    raw = _get(config, "pricing")
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if v is not None})
    return data


def cost_per_message(tokens: Any, price: dict[str, Any]) -> dict[str, float]:
    """给定注入 token 数，返回每条消息的输入成本（缓存未命中 / 命中两档）。"""
    try:
        count = max(0, int(tokens))
    except (TypeError, ValueError):
        count = 0
    miss = float(price.get("inputCacheMissPerM") or 0.0)
    hit = float(price.get("inputCacheHitPerM") or 0.0)
    return {
        "tokens": count,
        "cache_miss": round(count * miss / 1_000_000.0, 6),
        "cache_hit": round(count * hit / 1_000_000.0, 6),
    }


def budget_file(user_home: str) -> str:
    return os.path.join(user_home, BUDGET_FILE)


def load_runtime_budget(user_home: str) -> Optional[int]:
    """读取面板写入的预算；没有文件/坏文件返回 None。"""
    try:
        from base import read_json  # noqa: PLC0415

        data = read_json(budget_file(user_home), None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("maxTokens")
    if value is None:
        return None
    return clamp_budget(value)


def save_runtime_budget(user_home: str, total: Any, *, updated_at: Optional[float] = None) -> dict[str, Any]:
    from base import write_json_atomic  # noqa: PLC0415

    value = clamp_budget(total)
    payload = {
        "maxTokens": value,
        "min": BUDGET_MIN,
        "max": BUDGET_MAX,
        "default": BUDGET_DEFAULT,
        "updated_at": updated_at,
    }
    os.makedirs(user_home, exist_ok=True)
    write_json_atomic(budget_file(user_home), payload)
    return payload
