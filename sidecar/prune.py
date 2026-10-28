"""剪枝状态机（契约 §2.6，R4）。

四职责分离：
1. 门控（`gate`）：唯一开关=图谱规模（high/low/hard + hysteresis）；不剪时返回 idle；
2. 压缩（`compress`）：第 N 轮把 w_current<0.6 且非保护节点的 description 压到约 40%，
   `status=compressed`、记 `compressed_at`，**不清权重**；相似度>0.9 的碎片先 LLM merge
   （保留最高权重节点）；受保护：pinned / Preference / workspace_hit 近期 / 近 7 天 updated /
   protect_until / 新建 3 天内；`pending_forget` 只允许 prune 处理；
3. 归档（`prune`）：仅当 `round > compress_round` 才允许归档/删除（R4：同一轮 compress 后
   prune 只出报告）；归档条件 `status=compressed 且 w<0.3 且 age>3 天`；先剪边再归档点；
   冷区滞留 30 天且期间未命中（无激活记录）→ 真删除；单轮最多处理 ceil(count*0.08)；
   dry_run=True 只出报告不改图；连续 3 轮剪不到 low → 停止 + `journal.log_alarm(kind="prune_stuck")`；
4. 自适应（`on_retrieval_p95`）：每日 P95 记录；连续 3 天 p95>150ms 且 count<high → high*=0.8；
   长期 p95<50ms 且 count>0.8*high → high*=1.2；夹逼 [2000, calibrate_high*3]。

状态持久化：`{data_home}/maintenance.json`（round/compress_round/phase/连续失败/P95 历史/calibration）。
"""
from __future__ import annotations

import inspect
import math
import os
from typing import Any, Callable, Optional

from base import (
    SECONDS_PER_DAY,
    SCOPES,
    STATUS_ARCHIVED,
    STATUS_COMPRESSED,
    STATUS_PENDING_FORGET,
    embed_sparse,
    estimate_tokens,
    log,
    now_ts,
    read_json,
    sparse_cosine,
    today_str,
    truncate_to_tokens,
    write_json_atomic,
)

MAINTENANCE_FILE = "maintenance.json"

DEFAULT_WATERMARKS: dict[str, dict[str, int]] = {
    "workspace": {"high": 20000, "low": 16000, "hard": 50000},
    "user": {"high": 5000, "low": 4000, "hard": 12000},
}

MERGE_SCAN_CAP = 128
MERGE_CANDIDATE_CAP = 2000
P95_KEEP_DAYS = 14

MERGE_PROMPT = (
    "下面是一条记忆的多份相似碎片，请合并成一段简洁描述：保留全部核心事实、关键专名与数字，"
    "去除重复。只输出合并后的描述正文，不超过 {max_tokens} token。\n"
    "=== 碎片 ===\n{items}\n=== 碎片结束 ==="
)

_WEIGHT_MODULE: Any = None


def _weight_module() -> Any:
    """T1 的 weight.py 可能存在也可能尚未就绪；每次失败后重试（import 失败不缓存）。"""
    global _WEIGHT_MODULE
    if _WEIGHT_MODULE is not None:
        return _WEIGHT_MODULE
    try:
        import weight as weight_mod  # noqa: PLC0415

        _WEIGHT_MODULE = weight_mod
    except Exception:  # noqa: BLE001
        return None
    return _WEIGHT_MODULE


def cfg_get(config: Any, path: str, default: Any = None) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(path, default)
            return default if value is None else value
        except Exception:  # noqa: BLE001
            return default
    if isinstance(config, dict):
        cur: Any = config
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur
    return default


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _llm_mode(llm: Any) -> str:
    return str(getattr(llm, "mode", "") or "").lower()


async def _llm_call(llm: Any, prompt: str, *, max_tokens: int, purpose: str) -> str:
    complete = getattr(llm, "complete", None)
    if not callable(complete):
        raise RuntimeError("llm has no complete()")
    try:
        result = complete(prompt, max_tokens=max_tokens, purpose=purpose)
    except TypeError:
        result = complete(prompt, max_tokens=max_tokens)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, str) else str(result or "")


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    k = (len(ordered) - 1) * pct / 100.0
    low = int(math.floor(k))
    high = int(math.ceil(k))
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (k - low))


def _current_w_fallback(node: dict[str, Any], now: float) -> float:
    """weight.py 不可用时的惰性衰减公式（契约 §2.1：7 天半衰期，上限 10）。"""
    props = node.get("properties") or {}
    w_last = _num(props.get("w"), 1.0)
    last = _num(props.get("last_activated_at"), _num(props.get("updated_at"), _num(props.get("created_at"), now)))
    if now > last:
        w_last = w_last * (2.0 ** (-(now - last) / 604800.0))
    return max(0.0, min(10.0, w_last))


class Pruner:
    """单 scope 的两阶段剪枝器（一个实例只承载一个 scope 的图与门控）。"""

    def __init__(
        self,
        data_home: str,
        scope: str,
        config: Any,
        graph: Any,
        journal: Any = None,
        llm: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.data_home = os.path.abspath(str(data_home))
        self.scope = scope
        self.config = config
        self.graph = graph
        self.journal = journal
        self.llm = llm
        self.clock = clock
        self.maintenance_path = os.path.join(self.data_home, MAINTENANCE_FILE)
        self._state = self._load_state()

    # ------------------------------------------------------------------ #
    # 状态持久化
    # ------------------------------------------------------------------ #
    def _empty_state(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "round": 0,
            "compress_round": -1,
            "phase": "idle",
            "consecutive_fail_rounds": 0,
            "stuck": False,
            "stuck_alarm_sent": False,
            "calibration": {},
            "p95_history": {},
            "last_adaptive_date": None,
            "adaptive_base_high": None,
            "last_compress_at": None,
            "last_prune_at": None,
        }

    def _load_state(self) -> dict[str, Any]:
        raw = read_json(self.maintenance_path, {})
        state = self._empty_state()
        if isinstance(raw, dict):
            for key in state:
                if key in raw:
                    state[key] = raw[key]
        state["scope"] = self.scope
        state["round"] = int(state.get("round") or 0)
        state["compress_round"] = int(state.get("compress_round") if state.get("compress_round") is not None else -1)
        if state.get("phase") not in ("idle", "compressed"):
            state["phase"] = "idle"
        state["consecutive_fail_rounds"] = int(state.get("consecutive_fail_rounds") or 0)
        state["stuck"] = bool(state.get("stuck"))
        state["stuck_alarm_sent"] = bool(state.get("stuck_alarm_sent"))
        if not isinstance(state.get("calibration"), dict):
            state["calibration"] = {}
        if not isinstance(state.get("p95_history"), dict):
            state["p95_history"] = {}
        return state

    def _save(self) -> None:
        write_json_atomic(self.maintenance_path, dict(self._state))

    # ------------------------------------------------------------------ #
    # 配置 / 水位线
    # ------------------------------------------------------------------ #
    def _config_watermarks(self) -> dict[str, int]:
        raw = cfg_get(self.config, f"prune.watermarks.{self.scope}", None)
        fallback = DEFAULT_WATERMARKS.get(self.scope) or DEFAULT_WATERMARKS["workspace"]
        if not isinstance(raw, dict):
            raw = {}
        high = int(raw.get("high") or fallback["high"])
        low = int(raw.get("low") or round(high * 0.8))
        hard = int(raw.get("hard") or round(high * 2.5))
        return {"high": high, "low": low, "hard": hard}

    def _resolve_watermarks(self, calibration: Optional[dict[str, Any]] = None) -> dict[str, int]:
        base = self._config_watermarks()
        cal = calibration if isinstance(calibration, dict) else (self._state.get("calibration") or {})
        high = int(cal.get("high") or base["high"])
        if cal.get("low") is not None:
            low = int(cal["low"])
        elif cal.get("high") is not None:
            low = int(round(high * 0.8))
        else:
            low = base["low"]
        if cal.get("hard") is not None:
            hard = int(cal["hard"])
        elif cal.get("high") is not None:
            hard = int(round(high * 2.5))
        else:
            hard = base["hard"]
        return {"high": high, "low": low, "hard": hard}

    def watermarks(self) -> dict[str, int]:
        return self._resolve_watermarks()

    def calibrate(
        self,
        high: Any = None,
        low: Any = None,
        hard: Any = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """校准水位线（写入 maintenance.json 的 calibration，优先级高于 config）。"""
        if isinstance(high, dict):
            payload = high
            high = payload.get("high")
            low = payload.get("low")
            hard = payload.get("hard")
        cal = dict(self._state.get("calibration") or {})
        if dry_run:
            temp = dict(cal)
            if high is not None:
                temp["high"] = int(high)
            if low is not None:
                temp["low"] = int(low)
            if hard is not None:
                temp["hard"] = int(hard)
            return {"applied": False, "watermarks": self._resolve_watermarks(temp), "calibration": temp}
        if high is not None:
            cal["high"] = int(high)
        if low is not None:
            cal["low"] = int(low)
        if hard is not None:
            cal["hard"] = int(hard)
        self._state["calibration"] = cal
        if high is not None:
            self._state["adaptive_base_high"] = int(high)
        self._state["consecutive_fail_rounds"] = 0
        self._state["stuck"] = False
        self._state["stuck_alarm_sent"] = False
        self._save()
        return {"applied": True, "watermarks": self.watermarks(), "calibration": cal}

    # ------------------------------------------------------------------ #
    # 图读取辅助
    # ------------------------------------------------------------------ #
    def _count(self) -> int:
        graph = self.graph
        if graph is None:
            return 0
        try:
            return int(graph.count())
        except TypeError:
            try:
                return int(graph.count(self.scope))
            except Exception:  # noqa: BLE001
                return 0
        except Exception:  # noqa: BLE001
            return 0

    def _list_entities(
        self, *, include_archived: bool = False, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        graph = self.graph
        if graph is None:
            return []
        nodes: Any = None
        try:
            nodes = graph.list_entities(self.scope, include_archived=include_archived, status=status)
        except TypeError:
            try:
                nodes = graph.list_entities(self.scope)
            except TypeError:
                try:
                    nodes = graph.list_entities()
                except Exception:  # noqa: BLE001
                    nodes = []
            except Exception:  # noqa: BLE001
                nodes = []
        except Exception:  # noqa: BLE001
            nodes = []
        if not isinstance(nodes, list):
            return []
        out: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_status = (node.get("properties") or {}).get("status")
            if status is not None and node_status != status:
                continue
            if not include_archived and node_status == STATUS_ARCHIVED:
                continue
            out.append(node)
        return out

    def _get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        getter = getattr(self.graph, "get_entity", None) if self.graph is not None else None
        if not callable(getter):
            return None
        try:
            node = getter(entity_id)
        except Exception:  # noqa: BLE001
            return None
        return node if isinstance(node, dict) else None

    def _search_entities(self, query: str, top_k: int = 8) -> list[tuple[str, float]]:
        graph = self.graph
        if graph is None:
            return []
        try:
            pairs = graph.search_entities(query, self.scope, top_k=top_k)
        except TypeError:
            try:
                pairs = graph.search_entities(query, top_k=top_k)
            except Exception:  # noqa: BLE001
                return []
        except Exception:  # noqa: BLE001
            return []
        out: list[tuple[str, float]] = []
        for pair in pairs or []:
            try:
                out.append((str(pair[0]), float(pair[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def _current_w(self, node: dict[str, Any], now: float) -> float:
        weight_mod = _weight_module()
        if weight_mod is not None:
            fn = getattr(weight_mod, "current_w", None)
            if callable(fn):
                try:
                    return float(fn(node, now))
                except TypeError:
                    try:
                        return float(fn(node, now=now))
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
        return _current_w_fallback(node, now)

    def _protection_reason(self, node: dict[str, Any], now: float) -> Optional[str]:
        """契约 §2.6 的免疫/受保护清单（compress 与 prune 共用）。"""
        props = node.get("properties") or {}
        if props.get("pinned"):
            return "pinned"
        if str(node.get("type") or "") == "Preference":
            return "preference"
        protect_until = _num(props.get("protect_until"))
        if protect_until and protect_until > now:
            return "protect_until"
        created = _num(props.get("created_at"))
        new_protect = float(cfg_get(self.config, "weight.newProtectSeconds", 3 * SECONDS_PER_DAY))
        if created and now - created < new_protect:
            return "new"
        recent_days = float(cfg_get(self.config, "injection.recentUpdateDays", 7))
        recent_seconds = recent_days * SECONDS_PER_DAY
        updated = _num(props.get("updated_at"), created)
        if updated and now - updated < recent_seconds:
            return "recent_update"
        last_activated = _num(props.get("last_activated_at"))
        if props.get("workspace_hit") and last_activated and now - last_activated < recent_seconds:
            return "workspace_hit"
        return None

    # ------------------------------------------------------------------ #
    # 维护轮次 / 门控
    # ------------------------------------------------------------------ #
    def round_tick(self, scope: Optional[str] = None) -> int:
        """递增 maintenance round（TS 每个 turn 末尾调用一次），持久化并返回新值。"""
        self._state["round"] = int(self._state.get("round") or 0) + 1
        self._save()
        return int(self._state["round"])

    def gate(self, scope: Optional[str] = None) -> dict[str, Any]:
        """门控：唯一开关=图谱规模；返回 {action, count, high, low, hard, ratio, ...}。"""
        count = self._count()
        watermarks = self.watermarks()
        high = int(watermarks["high"])
        low = int(watermarks["low"])
        hard = int(watermarks["hard"])
        phase = str(self._state.get("phase") or "idle")
        stuck = bool(self._state.get("stuck"))
        action = "idle"
        reason = ""
        dirty = False
        if count <= low:
            reason = "below_low"
            if phase != "idle" or stuck or int(self._state.get("consecutive_fail_rounds") or 0) > 0:
                self._state["phase"] = "idle"
                self._state["stuck"] = False
                self._state["stuck_alarm_sent"] = False
                self._state["consecutive_fail_rounds"] = 0
                phase = "idle"
                dirty = True
        elif stuck:
            reason = "prune_stuck"
        elif count > hard:
            action = "force"
            reason = "hard_exceeded"
        elif count > high:
            if phase == "compressed":
                action = "prune"
                reason = "over_high"
            else:
                action = "compress"
                reason = "over_high"
        else:  # low < count <= high
            if phase == "compressed":
                action = "prune"
                reason = "hysteresis"
            else:
                reason = "within_band"
        if dirty:
            self._save()
        ratio = (count / high) if high else 0.0
        return {
            "action": action,
            "reason": reason,
            "count": count,
            "high": high,
            "low": low,
            "hard": hard,
            "ratio": round(ratio, 4),
            "phase": phase,
            "round": int(self._state.get("round") or 0),
            "compress_round": int(self._state.get("compress_round") or -1),
            "hysteresis": bool(low < count <= high),
            "stuck": stuck,
        }

    # ------------------------------------------------------------------ #
    # 压缩（第 N 轮）
    # ------------------------------------------------------------------ #
    def _compress_description(self, description: str) -> str:
        text = (description or "").strip()
        if len(text) <= 8:
            return text
        ratio = float(cfg_get(self.config, "prune.compressDescriptionRatio", 0.4))
        target = max(8, int(round(len(text) * ratio)))
        if target >= len(text):
            return text
        cut = text[:target]
        # 尽量在标点/空白处断开，避免残缺词
        for index in range(len(cut) - 1, max(0, len(cut) - 10) - 1, -1):
            if cut[index] in "。！？；，、,.!?; \n":
                cut = cut[: index + 1]
                break
        cut = cut.strip()
        return cut or text[:target]

    def _mark_compressed(
        self, entity_id: str, now: float, *, description: Optional[str] = None
    ) -> bool:
        """置 status=compressed / compressed_at，且**不改变** updated_at（避免伪造"最近改过"免疫）。"""
        node = self._get_entity(entity_id)
        if node is None:
            return False
        props = node.get("properties") or {}
        original_updated = props.get("updated_at")
        old_description = str(node.get("description") or "")
        new_description = description if description is not None else self._compress_description(old_description)
        if new_description != old_description:
            try:
                self.graph.edit_entity(entity_id, description=new_description)
            except TypeError:
                try:
                    self.graph.edit_entity(entity_id, description=new_description, properties=None)
                except Exception as exc:  # noqa: BLE001
                    log("warning", "prune: edit_entity failed during compress", entity=entity_id, error=str(exc))
                    return False
            except Exception as exc:  # noqa: BLE001
                log("warning", "prune: edit_entity failed during compress", entity=entity_id, error=str(exc))
                return False
        patch: dict[str, Any] = {"status": STATUS_COMPRESSED, "compressed_at": now}
        if original_updated is not None:
            patch["updated_at"] = original_updated
        try:
            self.graph.update_properties(entity_id, patch)
        except Exception as exc:  # noqa: BLE001
            log("warning", "prune: update_properties failed during compress", entity=entity_id, error=str(exc))
            return False
        return True

    def _find_merge_groups(
        self, candidates: list[tuple[float, dict[str, Any]]], now: float
    ) -> list[list[dict[str, Any]]]:
        """相似度 > mergeSimThreshold 的碎片分组（并查集；保留最高权重节点）。"""
        if len(candidates) < 2 or self.graph is None:
            return []
        if len(candidates) > MERGE_CANDIDATE_CAP:
            log(
                "warning",
                "prune: too many compress candidates; skip fragment merge this round",
                scope=self.scope,
                candidates=len(candidates),
            )
            return []
        threshold = float(cfg_get(self.config, "prune.mergeSimThreshold", 0.9))
        cand_map: dict[str, dict[str, Any]] = {node["id"]: node for _w, node in candidates if node.get("id")}
        if len(cand_map) < 2:
            return []
        parent: dict[str, str] = {entity_id: entity_id for entity_id in cand_map}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for _w, node in candidates[:MERGE_SCAN_CAP]:
            entity_id = node.get("id")
            if not entity_id:
                continue
            query = f"{node.get('name', '')}\n{node.get('description', '')}"
            for other_id, similarity in self._search_entities(query, top_k=8):
                if other_id == entity_id or other_id not in cand_map:
                    continue
                if similarity > threshold:
                    union(entity_id, other_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entity_id, node in cand_map.items():
            grouped.setdefault(find(entity_id), []).append(node)
        groups = [group for group in grouped.values() if len(group) >= 2]
        groups.sort(key=lambda group: sorted(str(node.get("id")) for node in group))
        return groups

    async def _merge_descriptions(
        self, group: list[dict[str, Any]], keep: dict[str, Any], now: float
    ) -> str:
        max_tokens = int(cfg_get(self.config, "llm.mergeMaxTokens", 512))
        items = []
        for node in group:
            name = str(node.get("name") or "")
            description = str(node.get("description") or "")
            items.append(f"- {name}: {description}" if name else f"- {description}")
        keep_description = str(keep.get("description") or "")
        mode = _llm_mode(self.llm)
        if self.llm is not None and mode not in ("offline",):
            prompt = MERGE_PROMPT.format(max_tokens=max_tokens, items="\n".join(items))
            try:
                text = await _llm_call(self.llm, prompt, max_tokens=max_tokens, purpose="merge")
                normalized = truncate_to_tokens((text or "").strip(), max_tokens)
                if normalized:
                    return normalized
            except Exception as exc:  # noqa: BLE001
                log("warning", "prune: fragment merge llm failed; deterministic fallback", scope=self.scope, error=str(exc))
        unique: list[str] = []
        for node in group:
            description = str(node.get("description") or "").strip()
            if description and description not in unique:
                unique.append(description)
        merged = "；".join(unique)
        if not merged:
            merged = keep_description
        return truncate_to_tokens(merged, max_tokens)

    async def compress(self, scope: Optional[str] = None) -> dict[str, Any]:
        """第 N 轮压缩：低权重非保护节点 description→约 40%，status=compressed（不清权重）。

        相似碎片 LLM merge 只在**门控触发的维护压缩**（count > high）里执行：门控是图谱规模
        唯一开关（§2.6），小规模显式 compress 不做昂贵的 LLM merge（避免把独立条目误并）。
        """
        now = now_ts(self.clock)
        round_no = int(self._state.get("round") or 0)
        threshold = float(cfg_get(self.config, "prune.compressWeightThreshold", 0.6))
        nodes = self._list_entities(include_archived=False)
        count_now = self._count()
        high = int(self.watermarks()["high"])
        merge_enabled = count_now > high
        candidates: list[tuple[float, dict[str, Any]]] = []
        protected: dict[str, int] = {}
        for node in nodes:
            props = node.get("properties") or {}
            status = props.get("status")
            if status in (STATUS_ARCHIVED, STATUS_COMPRESSED, STATUS_PENDING_FORGET):
                continue
            current = self._current_w(node, now)
            if current >= threshold:
                continue
            reason = self._protection_reason(node, now)
            if reason:
                protected[reason] = protected.get(reason, 0) + 1
                continue
            candidates.append((current, node))
        candidates.sort(key=lambda item: (item[0], str(item[1].get("id"))))
        groups = self._find_merge_groups(candidates, now) if merge_enabled else []
        grouped_ids: set[str] = set()
        for group in groups:
            grouped_ids.update(str(node.get("id")) for node in group)
        compressed: list[str] = []
        merged: list[list[str]] = []
        for group in groups:
            keep = max(group, key=lambda node: self._current_w(node, now))
            keep_id = str(keep.get("id"))
            keep_updated = (keep.get("properties") or {}).get("updated_at")
            merged_description = await self._merge_descriptions(group, keep, now)
            other_ids = [str(node.get("id")) for node in group if str(node.get("id")) != keep_id]
            if other_ids:
                try:
                    self.graph.merge_entities(other_ids, keep_id)
                except Exception as exc:  # noqa: BLE001
                    log("warning", "prune: merge_entities failed", entity=keep_id, error=str(exc))
            # merge_entities 会刷新 updated_at / 权重；这里恢复 updated_at 并置压缩态
            node_after = self._get_entity(keep_id) or keep
            if keep_updated is not None:
                try:
                    self.graph.update_properties(keep_id, {"updated_at": keep_updated})
                except Exception:  # noqa: BLE001
                    pass
            if self._mark_compressed(keep_id, now, description=merged_description):
                compressed.append(keep_id)
            merged.append([keep_id, *other_ids])
        for _current, node in candidates:
            entity_id = str(node.get("id") or "")
            if not entity_id or entity_id in grouped_ids:
                continue
            if self._mark_compressed(entity_id, now):
                compressed.append(entity_id)
        self._state["compress_round"] = round_no
        self._state["phase"] = "compressed"
        self._state["last_compress_at"] = now
        self._save()
        return {
            "compressed": compressed,
            "merged": merged,
            "skipped": None if candidates else "no_candidates",
            "count": self._count(),
            "candidates": len(candidates),
            "protected": protected,
            "merge_enabled": merge_enabled,
            "round": round_no,
            "phase": "compressed",
        }

    # ------------------------------------------------------------------ #
    # 归档 / 删除（第 N+1 轮起，R4）
    # ------------------------------------------------------------------ #
    def _archive_candidates(self, now: float) -> list[dict[str, Any]]:
        threshold = float(cfg_get(self.config, "prune.archiveWeightThreshold", 0.3))
        min_age = float(cfg_get(self.config, "prune.archiveMinAgeSeconds", 3 * SECONDS_PER_DAY))
        nodes = self._list_entities(include_archived=False)
        scored: list[tuple[float, dict[str, Any]]] = []
        for node in nodes:
            props = node.get("properties") or {}
            status = props.get("status")
            if status == STATUS_PENDING_FORGET:
                scored.append((0.0, node))
                continue
            if status != STATUS_COMPRESSED:
                continue
            current = self._current_w(node, now)
            if current >= threshold:
                continue
            updated = _num(props.get("updated_at"), _num(props.get("created_at")))
            if updated and now - updated < min_age:
                continue
            if self._protection_reason(node, now):
                continue
            scored.append((current, node))
        scored.sort(key=lambda item: (item[0], str(item[1].get("id"))))
        return [node for _score, node in scored]

    def _delete_candidates(self, now: float) -> list[dict[str, Any]]:
        nodes = self._list_entities(include_archived=True, status=STATUS_ARCHIVED)
        out: list[dict[str, Any]] = []
        for node in nodes:
            if self._cold_deletable(node, now):
                out.append(node)
        out.sort(key=lambda node: _num((node.get("properties") or {}).get("archived_at")))
        return out

    def _has_activation_since(self, entity_id: str, since: float) -> bool:
        journal = self.journal
        if journal is None:
            return False
        for name in ("has_activation_since", "last_activation_at", "activation_since"):
            fn = getattr(journal, name, None)
            if not callable(fn):
                continue
            try:
                if name == "last_activation_at":
                    value = fn(self.scope, entity_id)
                    if value is not None and _num(value) > since:
                        return True
                else:
                    value = fn(self.scope, entity_id, since)
                    if isinstance(value, bool):
                        if value:
                            return True
                    elif isinstance(value, (int, float)) and value > since:
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _cold_deletable(self, node: dict[str, Any], now: float) -> bool:
        """冷区滞留 30 天且期间未命中（无激活）→ 允许真删除。"""
        props = node.get("properties") or {}
        entity_id = str(node.get("id") or "")
        archived_at = _num(props.get("archived_at"))
        if not entity_id or not archived_at:
            return False
        retention = float(cfg_get(self.config, "prune.coldRetentionSeconds", 30 * SECONDS_PER_DAY))
        if now - archived_at < retention:
            return False
        last_activated = _num(props.get("last_activated_at"))
        if last_activated and last_activated > archived_at:
            return False
        if self._has_activation_since(entity_id, archived_at):
            return False
        return True

    def _delete_incident_relations(self, entity_id: str) -> int:
        """先剪边：删除候选点的全部关系，使其度为 0 再归档。"""
        graph = self.graph
        if graph is None:
            return 0
        relations: list[dict[str, Any]] = []
        relations_of = getattr(graph, "relations_of", None)
        if callable(relations_of):
            try:
                relations = relations_of(entity_id) or []
            except Exception:  # noqa: BLE001
                relations = []
        if not relations:
            raw_relations = getattr(graph, "relations", None)
            if isinstance(raw_relations, dict):
                relations = [
                    rel
                    for rel in raw_relations.values()
                    if isinstance(rel, dict) and (rel.get("source") == entity_id or rel.get("target") == entity_id)
                ]
        removed = 0
        for rel in relations:
            rel_id = rel.get("id") if isinstance(rel, dict) else None
            if not rel_id:
                continue
            try:
                graph.delete_relation(str(rel_id))
                removed += 1
            except Exception:  # noqa: BLE001
                continue
        return removed

    def _journal_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        journal = self.journal
        if journal is None:
            return None
        fn = getattr(journal, method, None)
        if not callable(fn):
            return None
        try:
            signature = inspect.signature(fn)
            parameters = signature.parameters
            accepts_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
            )
            if accepts_kwargs:
                return fn(*args, **kwargs)
            filtered = {key: value for key, value in kwargs.items() if key in parameters}
            return fn(*args, **filtered)
        except Exception:  # noqa: BLE001
            return None

    def _after_prune(
        self,
        count_before: int,
        count_after: int,
        watermarks: dict[str, int],
    ) -> dict[str, Any]:
        """连续失败计数 / prune_stuck 告警状态机。"""
        high = int(watermarks["high"])
        low = int(watermarks["low"])
        result: dict[str, Any] = {
            "reached_low": count_after <= low,
            "fail_rounds": int(self._state.get("consecutive_fail_rounds") or 0),
            "stuck": bool(self._state.get("stuck")),
            "alarm": None,
        }
        phase = str(self._state.get("phase") or "idle")
        if result["reached_low"]:
            self._state["consecutive_fail_rounds"] = 0
            self._state["stuck"] = False
            self._state["stuck_alarm_sent"] = False
            if phase != "idle":
                self._state["phase"] = "idle"
            result["fail_rounds"] = 0
            result["stuck"] = False
            return result
        tracked = count_before > high or phase == "compressed" or int(result["fail_rounds"]) > 0
        if not tracked:
            return result
        fail_rounds = int(result["fail_rounds"]) + 1
        self._state["consecutive_fail_rounds"] = fail_rounds
        max_stuck = int(cfg_get(self.config, "prune.maxStuckRounds", 3))
        if fail_rounds >= max_stuck:
            self._state["stuck"] = True
            if not self._state.get("stuck_alarm_sent"):
                self._state["stuck_alarm_sent"] = True
                detail = {
                    "scope": self.scope,
                    "round": int(self._state.get("round") or 0),
                    "count": count_after,
                    "high": high,
                    "low": low,
                    "fail_rounds": fail_rounds,
                }
                self._journal_call("log_alarm", self.scope, "prune_stuck", detail)
                result["alarm"] = "prune_stuck"
        result["fail_rounds"] = fail_rounds
        result["stuck"] = bool(self._state.get("stuck"))
        return result

    async def prune(self, dry_run: bool = True, scope: Optional[str] = None) -> dict[str, Any]:
        """第 N+1 轮起归档/删除；dry_run=True 只出报告不改图（R4）。"""
        if isinstance(dry_run, str) and dry_run in SCOPES:
            scope = dry_run
            dry_run = True
        dry_run = bool(dry_run)
        now = now_ts(self.clock)
        round_no = int(self._state.get("round") or 0)
        compress_round = int(self._state.get("compress_round") or -1)
        watermarks = self.watermarks()
        high = int(watermarks["high"])
        low = int(watermarks["low"])
        hard = int(watermarks["hard"])
        count_before = self._count()
        fraction = float(cfg_get(self.config, "prune.maxFractionPerRound", 0.08))
        limit = max(0, int(math.ceil(count_before * fraction))) if count_before > 0 else 0
        archive_candidates = self._archive_candidates(now)
        delete_candidates = self._delete_candidates(now) if limit > 0 else []
        same_round = round_no <= compress_round
        stuck = bool(self._state.get("stuck"))
        phase_before = str(self._state.get("phase") or "idle")
        archived: list[str] = []
        deleted: list[str] = []
        would_archive: list[str] = []
        would_delete: list[str] = []
        edges_removed = 0
        reason = "ok"
        fail_state = {"fail_rounds": int(self._state.get("consecutive_fail_rounds") or 0), "stuck": stuck, "alarm": None}
        if dry_run:
            would_archive = [str(node.get("id")) for node in archive_candidates[:limit]]
            remaining = max(0, limit - len(would_archive))
            would_delete = [str(node.get("id")) for node in delete_candidates[:remaining]]
            reason = "dry_run"
        elif stuck:
            reason = "stuck"
        elif same_round:
            # R4：同一轮 compress 后 prune 只出报告，禁止归档/删除
            reason = "same_round_compress"
        else:
            budget = limit
            for node in archive_candidates:
                if budget <= 0:
                    break
                entity_id = str(node.get("id") or "")
                if not entity_id:
                    continue
                before = {"properties": dict(node.get("properties") or {})}
                try:
                    edges_removed += self._delete_incident_relations(entity_id)
                    self.graph.update_properties(entity_id, {"status": STATUS_ARCHIVED, "archived_at": now})
                except Exception as exc:  # noqa: BLE001
                    log("warning", "prune: archive failed", entity=entity_id, error=str(exc))
                    continue
                after = {"properties": {"status": STATUS_ARCHIVED, "archived_at": now}}
                self._journal_call("log_change", self.scope, entity_id, "prune_archive", before, after)
                archived.append(entity_id)
                budget -= 1
            for node in delete_candidates:
                if budget <= 0:
                    break
                entity_id = str(node.get("id") or "")
                if not entity_id:
                    continue
                before = {"properties": dict(node.get("properties") or {})}
                try:
                    self.graph.delete_entity(entity_id)
                except Exception as exc:  # noqa: BLE001
                    log("warning", "prune: delete failed", entity=entity_id, error=str(exc))
                    continue
                self._journal_call("log_change", self.scope, entity_id, "prune_delete", before, {"deleted": True})
                deleted.append(entity_id)
                budget -= 1
            fail_state = self._after_prune(count_before, self._count(), watermarks)
            if fail_state.get("alarm"):
                reason = "prune_stuck"
            elif archived or deleted:
                reason = "pruned"
            else:
                reason = "no_candidates"
            self._state["last_prune_at"] = now
            self._save()
        count_after = count_before if dry_run else self._count()
        phase_after = str(self._state.get("phase") or "idle")
        report = {
            "scope": self.scope,
            "dry_run": dry_run,
            "round": round_no,
            "compress_round": compress_round,
            "phase_before": phase_before,
            "phase": phase_after,
            "count_before": count_before,
            "count_after": count_after,
            "high": high,
            "low": low,
            "hard": hard,
            "limit": limit,
            "archived": len(archived),
            "deleted": len(deleted),
            "would_archive": would_archive,
            "would_delete": would_delete,
            "edges_removed": edges_removed,
            "archive_candidates": len(archive_candidates),
            "delete_candidates": len(delete_candidates),
            "same_round": same_round,
            "hysteresis": bool(low < count_before <= high),
            "reason": reason,
            "fail_rounds": int(fail_state.get("fail_rounds") or 0),
            "stuck": bool(fail_state.get("stuck")),
            "ts": now,
        }
        return {"archived": archived, "deleted": deleted, "report": report}

    # ------------------------------------------------------------------ #
    # 运行期自适应（每日 P95）
    # ------------------------------------------------------------------ #
    def on_retrieval_p95(self, ms: Any, count: Any = None, scope: Any = None) -> dict[str, Any]:
        """记录每日检索 P95，并按 §2.6 自适应调整 high（夹逼 [2000, calibrate_high*3]）。"""
        if isinstance(ms, str) and ms in SCOPES:  # 兼容契约 §4 的 (scope, ms, count) 形式
            scope, ms, count = ms, count, scope
        try:
            latency_ms = float(ms)
        except (TypeError, ValueError):
            latency_ms = 0.0
        try:
            current_count = int(count) if count is not None else self._count()
        except (TypeError, ValueError):
            current_count = self._count()
        now = now_ts(self.clock)
        date = today_str(now)
        history = self._state.get("p95_history")
        if not isinstance(history, dict):
            history = {}
        day = history.get(date)
        if not isinstance(day, dict):
            day = {"samples": [], "p95": 0.0, "count": current_count}
        samples = [float(item) for item in (day.get("samples") or []) if isinstance(item, (int, float))]
        samples.append(latency_ms)
        if len(samples) > 500:
            samples = samples[-500:]
        p95 = _percentile(samples, 95)
        day.update({"samples": samples, "p95": p95, "count": current_count, "updated_at": now})
        history[date] = day
        keys = sorted(key for key in history if isinstance(key, str))
        for stale in keys[:-P95_KEEP_DAYS]:
            history.pop(stale, None)
        self._state["p95_history"] = history
        watermarks = self.watermarks()
        high = int(watermarks["high"])
        action: Optional[str] = None
        base = int(self._state.get("adaptive_base_high") or self._config_watermarks()["high"])
        base = max(1, base)
        lower = 2000 if base >= 2000 else base
        upper = max(base * 3, base)
        if bool(cfg_get(self.config, "prune.adaptive.enabled", True)) and self._state.get("last_adaptive_date") != date:
            day_keys = sorted(key for key in history if isinstance(key, str))[-3:]
            if len(day_keys) == 3:
                recent_days = [history[key] for key in day_keys]
                high_ms = float(cfg_get(self.config, "prune.adaptive.p95HighMs", 150))
                low_ms = float(cfg_get(self.config, "prune.adaptive.p95LowMs", 50))
                shrink = float(cfg_get(self.config, "prune.adaptive.highShrinkRatio", 0.8))
                grow = float(cfg_get(self.config, "prune.adaptive.highGrowRatio", 1.2))
                too_slow = all(
                    _num(item.get("p95")) > high_ms and _num(item.get("count")) < high for item in recent_days
                )
                too_fast = all(
                    _num(item.get("p95")) < low_ms and _num(item.get("count")) > 0.8 * high
                    for item in recent_days
                )
                if too_slow:
                    new_high = max(lower, int(round(high * shrink)))
                    action = "shrink"
                elif too_fast:
                    new_high = min(upper, int(round(high * grow)))
                    action = "grow"
                else:
                    new_high = high
                if action:
                    calibration = dict(self._state.get("calibration") or {})
                    calibration["high"] = new_high
                    self._state["calibration"] = calibration
                    self._state["last_adaptive_date"] = date
                    high = new_high
        self._save()
        return {
            "date": date,
            "p95": round(p95, 3),
            "samples": len(samples),
            "count": current_count,
            "high": high,
            "action": action,
        }

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        watermarks = self.watermarks()
        history = self._state.get("p95_history") if isinstance(self._state.get("p95_history"), dict) else {}
        p95_daily = {
            str(key): {"p95": round(_num(value.get("p95")), 3), "count": int(_num(value.get("count")))}
            for key, value in sorted(history.items())
            if isinstance(value, dict)
        }
        return {
            "scope": self.scope,
            "round": int(self._state.get("round") or 0),
            "compress_round": int(self._state.get("compress_round") or -1),
            "phase": str(self._state.get("phase") or "idle"),
            "consecutive_fail_rounds": int(self._state.get("consecutive_fail_rounds") or 0),
            "stuck": bool(self._state.get("stuck")),
            "high": int(watermarks["high"]),
            "low": int(watermarks["low"]),
            "hard": int(watermarks["hard"]),
            "count": self._count(),
            "calibration": dict(self._state.get("calibration") or {}),
            "last_compress_at": self._state.get("last_compress_at"),
            "last_prune_at": self._state.get("last_prune_at"),
            "p95_daily": p95_daily,
            "adaptive_base_high": self._state.get("adaptive_base_high"),
        }
