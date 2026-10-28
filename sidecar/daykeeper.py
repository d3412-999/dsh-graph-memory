"""日缓冲 / 恒定 day_context（契约 §2.5，R8）。

职责：
- 每个 turn 原文追加 `days/<YYYY-MM-DD>/turns.jsonl`，并维护 `day_state.json`；
- 当天原文 token > 512 时触发**全量压缩**（LLM 一次通读当天全部原文 + 已有当日摘要），
  压缩结果 ≤360 token，写单份 `days/day_summary.json`；保留最近 N 条原文保底；原文永不删除；
- 限流：距上次压缩 < 30 分钟 或 < 20 轮 → 跳过；
- 跨天 rollover：`history = LLM(旧 history + 当日 summary) ≤200`，写回单份
  `days/history_summary.json`（R8：历史摘要永远只有一份），当天清空重来；
- `day_pack(query)` 返回 `{day_text, detail_text, history_text, tokens}`，
  各段上限 512 / 312 / 200，总量 ≤1024（base.estimate_tokens 计量）。

所有 LLM 调用失败/不可用 → 确定性降级（extract.offline_rules.summarize，缺失时本地简易摘要），
绝不向上抛错。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from typing import Any, Callable, Optional

from base import (
    SCOPES,
    append_jsonl,
    embed_sparse,
    estimate_tokens,
    log,
    now_ts,
    read_json,
    read_jsonl,
    sparse_cosine,
    today_str,
    truncate_to_tokens,
    write_json_atomic,
)

DAYS_DIR = "days"
DAY_SUMMARY_FILE = "day_summary.json"
HISTORY_SUMMARY_FILE = "history_summary.json"
DAY_STATE_FILE = "day_state.json"

DEFAULT_DAY_TEXT_MAX = 512
DEFAULT_DETAIL_MAX = 312
DEFAULT_HISTORY_MAX = 200
DEFAULT_DAY_MAX = 1024

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

SUMMARY_PROMPT = (
    "你是对话记忆压缩器。请对下面的当天对话原文做**全量压缩摘要**（不要逐条罗列，"
    "合并重复信息）：保留关键事实、用户偏好/决策、否定与改口、待办与最近进展、"
    "重要专名与数字。只输出摘要正文，不要任何前后缀说明。\n"
    "要求：中文，不超过 {max_tokens} token。\n"
    "{previous_section}"
    "=== 当天全部原文 ===\n{raw}\n=== 原文结束 ==="
)

HISTORY_PROMPT = (
    "你是对话记忆压缩器。下面是长期滚动历史摘要与当天摘要，请把二者合并成一份新的滚动历史摘要，"
    "保留长期稳定的事实/偏好/决策与关键项目进展，丢弃寒暄与重复。只输出摘要正文。\n"
    "要求：中文，不超过 {max_tokens} token。\n"
    "=== 已有历史摘要 ===\n{old}\n=== 历史摘要结束 ===\n"
    "=== 当天摘要 ===\n{new}\n=== 当天摘要结束 ==="
)


# --------------------------------------------------------------------------- #
# 配置 / 通用小工具
# --------------------------------------------------------------------------- #
def cfg_get(config: Any, path: str, default: Any = None) -> Any:
    """兼容 base.Config / 普通 dict / None 的配置读取。"""
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


def _simple_summarize(text: str, max_tokens: int) -> str:
    """确定性简易摘要：保留开头要点 + 结尾最近进展，截断到 token 上限。"""
    text = (text or "").strip()
    if not text:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return truncate_to_tokens(text, max_tokens)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return truncate_to_tokens(text, max_tokens)
    half = max(1, len(lines) // 2)
    head_budget = max(1, int(max_tokens * 0.62))
    tail_budget = max(1, max_tokens - head_budget - 2)
    head = truncate_to_tokens("\n".join(lines[:half]), head_budget).strip()
    tail = truncate_to_tokens("\n".join(lines[-half:]), tail_budget).strip()
    out = (head + "\n...\n" + tail).strip() if head and tail else (head or tail)
    return truncate_to_tokens(out, max_tokens)


def _simple_merge_history(old: str, new: str, max_tokens: int) -> str:
    """确定性历史合并：旧历史前半 + 当日摘要后半（保证近期信息在）。"""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        return truncate_to_tokens(new, max_tokens)
    if not new:
        return truncate_to_tokens(old, max_tokens)
    old_part = truncate_to_tokens(old, max(1, int(max_tokens * 0.5)))
    new_budget = max(1, max_tokens - estimate_tokens(old_part) - 2)
    new_part = truncate_to_tokens(new, new_budget)
    out = (old_part + "\n...\n" + new_part).strip()
    return truncate_to_tokens(out, max_tokens)


def _normalize_summary(text: Any, max_tokens: int) -> str:
    if not isinstance(text, str):
        text = str(text or "")
    text = text.strip()
    if not text:
        return ""
    return truncate_to_tokens(text, max_tokens).strip()


def _query_terms(query: str) -> list[str]:
    """关键词兜底用的 query 词项：英文词 + CJK 1/2-gram。"""
    q = (query or "").lower()
    terms: list[str] = list(_WORD_RE.findall(q))
    cjk = _CJK_RE.findall(q)
    for n in (2, 1):
        for i in range(0, max(0, len(cjk) - n + 1)):
            terms.append("".join(cjk[i : i + n]))
    return list(dict.fromkeys(term for term in terms if term))


class DayKeeper:
    """单 scope 的日缓冲 / day_context 装配器（一个实例只承载一个 scope）。"""

    def __init__(
        self,
        data_home: str,
        scope: str,
        config: Any,
        graph: Any = None,
        llm: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.data_home = os.path.abspath(str(data_home))
        self.scope = scope
        self.config = config
        self.graph = graph
        self.llm = llm
        self.clock = clock
        self.days_dir = os.path.join(self.data_home, DAYS_DIR)
        self.state_path = os.path.join(self.data_home, DAY_STATE_FILE)
        self.day_summary_path = os.path.join(self.days_dir, DAY_SUMMARY_FILE)
        self.history_path = os.path.join(self.days_dir, HISTORY_SUMMARY_FILE)
        self._state = self._load_state()
        # 并发保护：service 可能在后台 task 里并发调用 rollover_if_needed / maybe_compress，
        # 同一实例的「写状态」路径必须串行化（锁按当前事件循环惰性创建，兼容多次 asyncio.run）。
        self._state_lock: Any = None
        self._state_lock_loop: Any = None

    # ------------------------------------------------------------------ #
    # 路径 / 状态持久化
    # ------------------------------------------------------------------ #
    def _turns_path(self, date: str) -> str:
        return os.path.join(self.days_dir, date, "turns.jsonl")

    def _empty_state(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "date": None,
            "turns": 0,
            "last_compress_at": None,
            "last_compress_turn": 0,
            "summary_tokens": 0,
            "last_turn_id": None,
            "updated_at": None,
        }

    def _load_state(self) -> dict[str, Any]:
        raw = read_json(self.state_path, {})
        state = self._empty_state()
        if isinstance(raw, dict):
            for key in state:
                if key in raw:
                    state[key] = raw[key]
        state["scope"] = self.scope
        state["turns"] = int(state.get("turns") or 0)
        state["last_compress_turn"] = int(state.get("last_compress_turn") or 0)
        state["summary_tokens"] = int(state.get("summary_tokens") or 0)
        return state

    def _save_state(self) -> None:
        write_json_atomic(self.state_path, dict(self._state))

    def _coerce_ts(self, value: Any) -> float:
        if value is None:
            return now_ts(self.clock)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return now_ts(self.clock)

    # ------------------------------------------------------------------ #
    # 并发保护（service 后台 task 可能并发调用 rollover/maybe_compress）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lock_timeout_seconds() -> float:
        return 2.0

    def _get_state_lock(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._state_lock is None or self._state_lock_loop is not loop:
            self._state_lock = asyncio.Lock()
            self._state_lock_loop = loop
        return self._state_lock

    async def _acquire_state_lock(self) -> Any:
        """超时内拿到锁；拿不到返回 None（调用方返回 busy，绝不阻塞对话）。"""
        lock = self._get_state_lock()
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self._lock_timeout_seconds())
        except (asyncio.TimeoutError, TimeoutError):
            return None
        return lock

    # ------------------------------------------------------------------ #
    # 原始回合 / 当日摘要 / 历史摘要 读取
    # ------------------------------------------------------------------ #
    def _turns(self, date: str) -> list[dict[str, Any]]:
        rows = read_jsonl(self._turns_path(date))
        return [row for row in rows if isinstance(row, dict)]

    def _read_day_summary(self) -> dict[str, Any]:
        raw = read_json(self.day_summary_path, {})
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return {"text": raw}
        return {}

    def _write_day_summary(self, record: dict[str, Any]) -> None:
        write_json_atomic(self.day_summary_path, record)

    def _read_history_record(self) -> dict[str, Any]:
        raw = read_json(self.history_path, {})
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return {"text": raw}
        return {}

    def _read_history_text(self) -> str:
        record = self._read_history_record()
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            text = record.get("summary") if isinstance(record.get("summary"), str) else ""
        return (text or "").strip()

    def _write_history(self, text: str, ts: float, merged_date: Optional[str] = None) -> dict[str, Any]:
        """R8：历史摘要永远只有一份（原子覆盖同一文件）。"""
        record = self._read_history_record()
        days = record.get("days") if isinstance(record.get("days"), list) else []
        days = [str(d) for d in days if isinstance(d, str)]
        if merged_date and merged_date not in days:
            days.append(merged_date)
        payload = {
            "version": 1,
            "scope": self.scope,
            "text": text,
            "tokens": estimate_tokens(text),
            "updated_at": ts,
            "days": days[-90:],
        }
        write_json_atomic(self.history_path, payload)
        return payload

    def _reset_day(self, date: str, now: float) -> None:
        self._state.update(
            {
                "scope": self.scope,
                "date": date,
                "turns": 0,
                "last_compress_at": None,
                "last_compress_turn": 0,
                "summary_tokens": 0,
                "last_turn_id": None,
                "updated_at": now,
            }
        )
        self._write_day_summary(
            {"date": date, "text": "", "tokens": 0, "recent": [], "source_turns": 0, "updated_at": now}
        )
        self._save_state()

    async def _ensure_today(self, now: float) -> str:
        date = today_str(now)
        state_date = self._state.get("date")
        if state_date is None:
            self._reset_day(date, now)
        elif state_date != date:
            await self._rollover_impl(now)
        return today_str(now)

    # ------------------------------------------------------------------ #
    # 写入：追加 turn（§2.5）
    # ------------------------------------------------------------------ #
    async def append_turn(self, text: str, turn_id: str, ts: Any = None) -> dict[str, Any]:
        """追加一条当天原文；写 `days/<date>/turns.jsonl` 并更新 `day_state.json`。

        `ts` 兼容服务端传入的 epoch 秒；若误传 scope 字符串则忽略（契约 §4 两种签名都接受）。
        并发安全：与 rollover / maybe_compress 共用状态锁，避免跨天时重复合并历史。
        """
        if isinstance(ts, str) and ts in SCOPES:
            ts = None
        now = self._coerce_ts(ts)
        lock = await self._acquire_state_lock()
        try:
            return await self._append_turn_impl(text, turn_id, now, allow_rollover=lock is not None)
        finally:
            if lock is not None:
                lock.release()

    async def _append_turn_impl(
        self, text: str, turn_id: str, now: float, *, allow_rollover: bool = True
    ) -> dict[str, Any]:
        text = str(text or "")
        turn_id = str(turn_id or "")
        date = today_str(now)
        state_date = self._state.get("date")
        deferred_rollover = False
        if state_date is None:
            self._reset_day(date, now)
        elif state_date != date:
            if allow_rollover:
                # 跨天安全网：先把旧的一天合并进单份历史摘要，再开始新的一天
                await self._rollover_impl(now)
            else:
                # 拿不到锁（后台 task 正持有）：先落盘新一天原文，状态由下一次 rollover 推进
                deferred_rollover = True
        path = self._turns_path(date)
        existing = self._turns(date)
        duplicated = any(str(row.get("turn_id") or "") == turn_id for row in existing)
        if not duplicated:
            record = {"turn_id": turn_id, "text": text, "ts": float(now), "date": date}
            append_jsonl(path, record)
            existing.append(record)
        if not deferred_rollover:
            self._state.update(
                {
                    "date": date,
                    "turns": len(existing),
                    "last_turn_id": turn_id,
                    "updated_at": now,
                }
            )
            self._save_state()
        result: dict[str, Any] = {
            "appended": not duplicated,
            "date": date,
            "turn_id": turn_id,
            "turns": len(existing),
            "tokens": estimate_tokens(text),
        }
        if deferred_rollover:
            result["deferred_rollover"] = True
        return result

    # ------------------------------------------------------------------ #
    # 全量压缩（§2.5）
    # ------------------------------------------------------------------ #
    async def maybe_compress(self, scope: Optional[str] = None) -> dict[str, Any]:
        """当天原文超限时做全量压缩（含限流）；返回 {compressed, summary_tokens, reason}。

        并发/重入：同一实例可被后台 task 并发调用；串行化后第二次会命中限流而跳过，
        不会重复压缩。拿不到状态锁 → 返回 busy（不阻塞）。
        """
        lock = await self._acquire_state_lock()
        if lock is None:
            return {
                "compressed": False,
                "summary_tokens": int(self._state.get("summary_tokens") or 0),
                "reason": "busy",
                "date": self._state.get("date"),
            }
        try:
            return await self._maybe_compress_impl(scope)
        finally:
            lock.release()

    async def _maybe_compress_impl(self, scope: Optional[str] = None) -> dict[str, Any]:
        now = now_ts(self.clock)
        date = await self._ensure_today(now)
        turns = self._turns(date)
        if not turns:
            return {"compressed": False, "summary_tokens": 0, "reason": "no_turns", "date": date}
        raw = "\n".join(str(turn.get("text") or "") for turn in turns)
        raw_tokens = estimate_tokens(raw)
        raw_limit = int(cfg_get(self.config, "day.rawTokenLimit", 512))
        if raw_tokens <= raw_limit:
            return {
                "compressed": False,
                "summary_tokens": int(self._state.get("summary_tokens") or 0),
                "reason": "under_limit",
                "date": date,
                "raw_tokens": raw_tokens,
            }
        min_interval = float(cfg_get(self.config, "day.compressMinIntervalSeconds", 1800))
        min_turns = int(cfg_get(self.config, "day.compressMinTurns", 20))
        last_at = self._state.get("last_compress_at")
        last_turn = int(self._state.get("last_compress_turn") or 0)
        turns_since = len(turns) - last_turn
        if last_at is not None and (now - float(last_at)) < min_interval:
            return {
                "compressed": False,
                "summary_tokens": int(self._state.get("summary_tokens") or 0),
                "reason": "throttled_interval",
                "date": date,
                "seconds_since_last": round(now - float(last_at), 3),
            }
        if turns_since < min_turns:
            return {
                "compressed": False,
                "summary_tokens": int(self._state.get("summary_tokens") or 0),
                "reason": "throttled_turns",
                "date": date,
                "turns_since": turns_since,
            }
        summary_max = int(cfg_get(self.config, "day.summaryMaxTokens", 360))
        keep_recent = int(cfg_get(self.config, "day.keepRecentTurns", 2))
        previous = ""
        record = self._read_day_summary()
        if isinstance(record, dict) and str(record.get("date") or "") == date:
            candidate = record.get("text")
            if isinstance(candidate, str) and candidate.strip():
                previous = truncate_to_tokens(candidate.strip(), summary_max)
        summary = await self._summarize(raw, summary_max, previous=previous)
        summary = _normalize_summary(summary, summary_max) or _simple_summarize(raw, summary_max)
        summary = truncate_to_tokens(summary, summary_max)
        summary_tokens = estimate_tokens(summary)
        recent = [turn for turn in turns[-keep_recent:]] if keep_recent > 0 else []
        self._write_day_summary(
            {
                "date": date,
                "text": summary,
                "tokens": summary_tokens,
                "recent": [
                    {"turn_id": str(turn.get("turn_id") or ""), "text": str(turn.get("text") or "")}
                    for turn in recent
                ],
                "source_turns": len(turns),
                "updated_at": now,
            }
        )
        self._state.update(
            {
                "last_compress_at": now,
                "last_compress_turn": len(turns),
                "summary_tokens": summary_tokens,
                "updated_at": now,
            }
        )
        self._save_state()
        return {
            "compressed": True,
            "summary_tokens": summary_tokens,
            "reason": "ok",
            "date": date,
            "raw_tokens": raw_tokens,
            "source_turns": len(turns),
            "recent_turns": len(recent),
        }

    async def _summarize(self, raw: str, max_tokens: int, previous: str = "") -> str:
        """LLM 全量压缩；任何失败/不可用 → 确定性降级。"""
        mode = _llm_mode(self.llm)
        if self.llm is not None and mode not in ("offline",) and raw.strip():
            previous_section = f"=== 已有当日摘要 ===\n{previous}\n=== 已有当日摘要结束 ===\n" if previous else ""
            prompt = SUMMARY_PROMPT.format(max_tokens=max_tokens, previous_section=previous_section, raw=raw)
            try:
                text = await _llm_call(self.llm, prompt, max_tokens=max_tokens, purpose="summarize")
                normalized = _normalize_summary(text, max_tokens)
                if normalized:
                    return normalized
                log("warning", "daykeeper: llm summary empty; fallback to offline rules", scope=self.scope)
            except Exception as exc:  # noqa: BLE001 — LLM 失败必须降级
                log("warning", "daykeeper: llm summary failed; fallback to offline rules", scope=self.scope, error=str(exc))
        return self._offline_summary(raw, max_tokens)

    def _offline_summary(self, text: str, max_tokens: int) -> str:
        try:
            from base import import_offline_rules  # noqa: PLC0415

            rules = import_offline_rules()
            summarize = getattr(rules, "summarize", None)
            if callable(summarize):
                try:
                    result = summarize(text, max_tokens)
                except TypeError:
                    result = summarize(text, max_tokens=max_tokens)
                normalized = _normalize_summary(result, max_tokens)
                if normalized:
                    return normalized
        except Exception:  # noqa: BLE001 — extract.py 可能尚未就绪/导入失败
            pass
        return _simple_summarize(text, max_tokens)

    # ------------------------------------------------------------------ #
    # 跨天 rollover / 历史滚动摘要（R8）
    # ------------------------------------------------------------------ #
    async def _ensure_day_summary(self, date: str, turns: list[dict[str, Any]], now: float) -> str:
        summary_max = int(cfg_get(self.config, "day.summaryMaxTokens", 360))
        record = self._read_day_summary()
        if isinstance(record, dict) and str(record.get("date") or "") == date:
            existing = record.get("text")
            if isinstance(existing, str) and existing.strip():
                return truncate_to_tokens(existing.strip(), summary_max)
        if not turns:
            return ""
        raw = "\n".join(str(turn.get("text") or "") for turn in turns)
        summary = await self._summarize(raw, summary_max, previous="")
        summary = _normalize_summary(summary, summary_max) or _simple_summarize(raw, summary_max)
        summary = truncate_to_tokens(summary, summary_max)
        keep_recent = int(cfg_get(self.config, "day.keepRecentTurns", 2))
        recent = turns[-keep_recent:] if keep_recent > 0 else []
        self._write_day_summary(
            {
                "date": date,
                "text": summary,
                "tokens": estimate_tokens(summary),
                "recent": [
                    {"turn_id": str(turn.get("turn_id") or ""), "text": str(turn.get("text") or "")}
                    for turn in recent
                ],
                "source_turns": len(turns),
                "updated_at": now,
            }
        )
        return summary

    async def _merge_history(self, old_history: str, new_summary: str, max_tokens: int) -> str:
        mode = _llm_mode(self.llm)
        if self.llm is not None and mode not in ("offline",) and (old_history or new_summary):
            prompt = HISTORY_PROMPT.format(max_tokens=max_tokens, old=old_history or "（无）", new=new_summary or "（无）")
            try:
                text = await _llm_call(self.llm, prompt, max_tokens=max_tokens, purpose="summarize")
                normalized = _normalize_summary(text, max_tokens)
                if normalized:
                    return normalized
            except Exception as exc:  # noqa: BLE001
                log("warning", "daykeeper: history merge llm failed; fallback", scope=self.scope, error=str(exc))
        return _simple_merge_history(old_history, new_summary, max_tokens)

    async def _do_rollover(self, old_date: str, target_date: str, now: float) -> dict[str, Any]:
        history_max = int(cfg_get(self.config, "injection.historyMaxTokens", DEFAULT_HISTORY_MAX))
        old_turns = self._turns(old_date)
        day_summary = await self._ensure_day_summary(old_date, old_turns, now)
        old_history = self._read_history_text()
        merged = await self._merge_history(old_history, day_summary, history_max)
        merged = truncate_to_tokens((merged or "").strip(), history_max)
        payload = self._write_history(merged, now, merged_date=old_date)
        self._reset_day(target_date, now)
        return {
            "rolled": True,
            "from_date": old_date,
            "to_date": target_date,
            "history_tokens": int(payload.get("tokens") or 0),
            "history_text": merged,
            "day_summary_tokens": estimate_tokens(day_summary),
            "history_days": len(payload.get("days") or []),
        }

    async def rollover(self, now: Any = None, today: Optional[str] = None) -> dict[str, Any]:
        """把当前天的摘要合并进唯一的历史摘要文件，清空当天重来（R8）。"""
        lock = await self._acquire_state_lock()
        if lock is None:
            return {
                "rolled": False,
                "busy": True,
                "history_tokens": estimate_tokens(self._read_history_text()),
            }
        try:
            return await self._rollover_impl(now, today, force=True)
        finally:
            lock.release()

    async def rollover_if_needed(self, now: Any = None, today: Optional[str] = None) -> dict[str, Any]:
        """跨天时 rollover；同一天则快速 no-op（契约 §4 的 (scope, today) 形式也兼容）。

        并发/重入安全：两个后台 task 同时调用时只有第一个真正合并，第二个看到 date 已推进 → no-op；
        拿不到状态锁 → 返回 busy（调用方下次重试即可）。
        """
        lock = await self._acquire_state_lock()
        if lock is None:
            target = today if isinstance(today, str) and today else today_str(now_ts(self.clock))
            return {
                "rolled": False,
                "busy": True,
                "history_tokens": estimate_tokens(self._read_history_text()),
                "today": target,
            }
        try:
            return await self._rollover_impl(now, today, force=False)
        finally:
            lock.release()

    async def _rollover_impl(
        self, now: Any = None, today: Optional[str] = None, *, force: bool = False
    ) -> dict[str, Any]:
        """持锁版 rollover 实现（force=False 时同一天 no-op）。"""
        if isinstance(now, str) and now in SCOPES:
            now = None
        ts = self._coerce_ts(now)
        target = today if isinstance(today, str) and today else today_str(ts)
        old_date = self._state.get("date")
        if old_date is None:
            self._reset_day(target, ts)
            return {"rolled": False, "history_tokens": estimate_tokens(self._read_history_text()), "today": target}
        if not force and str(old_date) == target:
            return {"rolled": False, "history_tokens": estimate_tokens(self._read_history_text()), "today": target}
        return await self._do_rollover(str(old_date), target, ts)

    # ------------------------------------------------------------------ #
    # day_pack（§2.5 / R1 / R8）
    # ------------------------------------------------------------------ #
    def _day_text_for(
        self, date: str, turns: list[dict[str, Any]], raw_tokens: int
    ) -> tuple[str, bool]:
        """返回 (day_text, compressed)。≤512 token 给原文，否则给当日摘要 + 最近原文保底。"""
        raw_limit = int(cfg_get(self.config, "day.rawTokenLimit", 512))
        day_cap = int(cfg_get(self.config, "injection.dayTextMaxTokens", DEFAULT_DAY_TEXT_MAX))
        raw = "\n".join(str(turn.get("text") or "") for turn in turns)
        if raw_tokens <= raw_limit:
            return truncate_to_tokens(raw, day_cap), False
        record = self._read_day_summary()
        if isinstance(record, dict) and str(record.get("date") or "") == date:
            summary_text = record.get("text")
            summary_text = str(summary_text).strip() if isinstance(summary_text, str) else ""
            if summary_text:
                summary_max = int(cfg_get(self.config, "day.summaryMaxTokens", 360))
                summary_text = truncate_to_tokens(summary_text, summary_max)
                keep_recent = int(cfg_get(self.config, "day.keepRecentTurns", 2))
                recent = record.get("recent")
                recent_texts: list[str] = []
                if isinstance(recent, list):
                    recent_texts = [
                        str(item.get("text") or "")
                        for item in recent
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    ]
                if not recent_texts and keep_recent > 0:
                    recent_texts = [str(turn.get("text") or "") for turn in turns[-keep_recent:]]
                day_text = summary_text
                if recent_texts:
                    sep = "\n"
                    available = day_cap - estimate_tokens(day_text) - estimate_tokens(sep)
                    if available > 0:
                        day_text = (day_text + sep + truncate_to_tokens("\n".join(recent_texts), available)).strip()
                return truncate_to_tokens(day_text, day_cap), True
        # 摘要尚未生成（限流/尚未压缩）：先给截断原文兜底，保证预算恒定
        return truncate_to_tokens(raw, day_cap), False

    def _is_covered(self, text: str, day_text: str, chosen: list[str]) -> bool:
        text = (text or "").strip()
        if not text:
            return True
        if text in day_text:
            return True
        for item in chosen:
            if text in item or item in text:
                return True
        return False

    def _graph_chunks_for_day(self, query: str, date: str) -> list[str]:
        if self.graph is None:
            return []
        top_k = int(cfg_get(self.config, "day.detailTopK", 3))
        fetch = max(top_k * 3, 8)
        pairs: list[Any] = []
        try:
            pairs = self.graph.search_chunks(query, top_k=fetch, day=date)
        except TypeError:
            try:
                pairs = self.graph.search_chunks(self.scope, query, top_k=fetch, day=date)
            except TypeError:
                try:
                    pairs = self.graph.search_chunks(query, top_k=fetch)
                except Exception:  # noqa: BLE001
                    pairs = []
            except Exception:  # noqa: BLE001
                pairs = []
        except Exception:  # noqa: BLE001
            pairs = []
        texts: list[str] = []
        for pair in pairs or []:
            try:
                chunk_id = pair[0] if isinstance(pair, (list, tuple)) else getattr(pair, "id", "")
            except Exception:  # noqa: BLE001
                continue
            chunk = None
            getter = getattr(self.graph, "get_chunk", None)
            if callable(getter):
                try:
                    chunk = getter(str(chunk_id))
                except Exception:  # noqa: BLE001
                    chunk = None
            if not isinstance(chunk, dict):
                continue
            chunk_date = chunk.get("date")
            if chunk_date and str(chunk_date) != date:
                continue
            kind = chunk.get("kind")
            if kind and str(kind) != "turn":
                continue
            text = str(chunk.get("text") or "").strip()
            if text:
                texts.append(text)
        return texts

    def _fallback_detail(self, query: str, turns: list[dict[str, Any]], need: int) -> list[str]:
        terms = _query_terms(query)
        query_vec = embed_sparse(query) if query else {}
        scored: list[tuple[float, int, str]] = []
        for index, turn in enumerate(turns):
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            score = 0.0
            if query_vec:
                try:
                    score = sparse_cosine(query_vec, embed_sparse(text))
                except Exception:  # noqa: BLE001
                    score = 0.0
            lowered = text.lower()
            hits = sum(1 for term in terms if term and term in lowered)
            if hits:
                score += min(0.6, 0.2 * hits)
            if score <= 0:
                continue
            scored.append((score, index, text))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        out: list[str] = []
        for _score, _index, text in scored:
            if len(out) >= need:
                break
            if text not in out:
                out.append(text)
        return out

    def _detail_text(self, query: str, date: str, turns: list[dict[str, Any]], day_text: str) -> str:
        top_k = int(cfg_get(self.config, "day.detailTopK", 3))
        detail_cap = int(cfg_get(self.config, "injection.detailMaxTokens", DEFAULT_DETAIL_MAX))
        chosen: list[str] = []
        candidates = self._graph_chunks_for_day(query, date) if query else []
        for text in candidates:
            if len(chosen) >= top_k:
                break
            if self._is_covered(text, day_text, chosen):
                continue
            chosen.append(text)
        if len(chosen) < top_k and query:
            for text in self._fallback_detail(query, turns, top_k - len(chosen)):
                if len(chosen) >= top_k:
                    break
                if self._is_covered(text, day_text, chosen):
                    continue
                chosen.append(text)
        if not chosen:
            return ""
        return truncate_to_tokens("\n".join(chosen), detail_cap)

    async def day_pack(self, query: str, scope: Optional[str] = None) -> dict[str, Any]:
        """返回恒定预算的 {day_text, detail_text, history_text, tokens}。"""
        if not isinstance(query, str):
            query = str(query or "")
        now = now_ts(self.clock)
        date = today_str(now)
        turns = self._turns(date)
        raw = "\n".join(str(turn.get("text") or "") for turn in turns)
        raw_tokens = estimate_tokens(raw)
        day_text, compressed = self._day_text_for(date, turns, raw_tokens)
        day_text = truncate_to_tokens(day_text, int(cfg_get(self.config, "injection.dayTextMaxTokens", DEFAULT_DAY_TEXT_MAX)))
        detail_text = self._detail_text(query, date, turns, day_text)
        history_text = truncate_to_tokens(
            self._read_history_text(), int(cfg_get(self.config, "injection.historyMaxTokens", DEFAULT_HISTORY_MAX))
        )
        day_max = int(cfg_get(self.config, "injection.dayMaxTokens", DEFAULT_DAY_MAX))
        tokens = {
            "day": estimate_tokens(day_text),
            "detail": estimate_tokens(detail_text),
            "history": estimate_tokens(history_text),
        }
        tokens["total"] = tokens["day"] + tokens["detail"] + tokens["history"]
        if tokens["total"] > day_max:
            # 防御性兜底：各段已分别受 512/312/200 限制，正常不会触发
            overflow = tokens["total"] - day_max
            day_text = truncate_to_tokens(day_text, max(0, tokens["day"] - overflow))
            tokens["day"] = estimate_tokens(day_text)
            tokens["total"] = tokens["day"] + tokens["detail"] + tokens["history"]
        return {
            "day_text": day_text,
            "detail_text": detail_text,
            "history_text": history_text,
            "tokens": tokens,
            "date": date,
            "raw_tokens": raw_tokens,
            "compressed": compressed,
            "history_days": len(self._read_history_record().get("days") or []),
        }

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        now = now_ts(self.clock)
        date = today_str(now)
        turns = self._turns(date)
        raw = "\n".join(str(turn.get("text") or "") for turn in turns)
        raw_tokens = estimate_tokens(raw)
        day_text, compressed = self._day_text_for(date, turns, raw_tokens)
        history_text = self._read_history_text()
        state_turns = int(self._state.get("turns") or 0)
        return {
            "scope": self.scope,
            "date": self._state.get("date"),
            "today": date,
            "turns": len(turns),
            "day_tokens": estimate_tokens(day_text),
            "raw_tokens": raw_tokens,
            "compressed": compressed,
            "summary_tokens": int(self._state.get("summary_tokens") or 0),
            "history_tokens": estimate_tokens(history_text),
            "history_days": len(self._read_history_record().get("days") or []),
            "last_compress_at": self._state.get("last_compress_at"),
            "last_compress_turn": int(self._state.get("last_compress_turn") or 0),
            "state_turns": state_turns,
            "last_turn_id": self._state.get("last_turn_id"),
            "updated_at": self._state.get("updated_at"),
        }
