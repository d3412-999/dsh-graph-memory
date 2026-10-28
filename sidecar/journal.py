"""Journal：SQLite 唯一合法用途（docs/CONTRACT.md §2.7）。

表结构（契约 §2.7，全部为审计元数据，绝不存图谱节点 — R2）::

    changes(id, ts, scope, target_id, kind, before, after, confidence, source_turn_id, rolled_back)
    activations(ts, scope, node_id, reason, w_before, w_after)
    ingests(...)、prune_reports(...)、day_events(...)、alarms(...)
    dead_letter(id, ts, scope, payload, attempts, last_error, replayed)
    injections(ts, session_id, turn, tokens_json, injected_ids)

关键契约：
- ``changes.before/after`` 统一存 JSON ``{"description": str|None, "properties": dict}``，
  ``rollback`` 用 before 恢复 description+properties，并**必须**通过
  ``graph.edit_entity(target_id, description=..., properties=...)`` 完成，从而同步重算
  向量索引（R6：回滚后旧说法检索不再命中旧值）。
- ``rollback`` 仅允许：7 天内、未回滚、kind ∈
  {supersede, update, weight, pin, forget, prune_archive}；返回
  ``{"restored": {"id", "before", "after"}, "change_id", "kind", "rolled_back_at"}``，并写回滚记录。
- 死信：ingest 失败重试 2 次（0.2s/0.8s 退避）后由 service 调用 ``enqueue_dead_letter`` 落库；
  面板通过 ``dead_letters()`` 列出并用 ``mark_dead_letter_replayed`` 标记重放。
- 本模块只 import ``base`` 与标准库；DB 连接线程安全（RLock + ``check_same_thread=False``）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Callable, Optional

from base import (
    SECONDS_PER_DAY,
    GraphMemoryError,
    jdumps,
    now_ts,
)

# 允许回滚的 change kind（契约 §2.7）
ROLLBACK_KINDS = frozenset({"supersede", "update", "weight", "pin", "forget", "prune_archive"})
DEFAULT_RETENTION_DAYS = 7.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    target_id TEXT,
    kind TEXT NOT NULL,
    before TEXT,
    after TEXT,
    confidence REAL,
    source_turn_id TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_scope_target ON changes(scope, target_id);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes(ts);

CREATE TABLE IF NOT EXISTS activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    node_id TEXT NOT NULL,
    reason TEXT,
    w_before REAL,
    w_after REAL
);
CREATE INDEX IF NOT EXISTS idx_activations_scope_node ON activations(scope, node_id, ts);

CREATE TABLE IF NOT EXISTS ingests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    turn_id TEXT,
    status TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingests_scope_ts ON ingests(scope, ts);

CREATE TABLE IF NOT EXISTS prune_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_prune_reports_scope_ts ON prune_reports(scope, ts);

CREATE TABLE IF NOT EXISTS day_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_day_events_scope_ts ON day_events(scope, ts);

CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarms_scope_ts ON alarms(scope, ts);

CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    payload TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    replayed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dead_letter_scope_ts ON dead_letter(scope, ts);

CREATE TABLE IF NOT EXISTS injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    turn INTEGER,
    tokens_json TEXT,
    injected_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_injections_ts ON injections(ts);
"""


# --------------------------------------------------------------------------- #
# 状态编解码：changes.before/after 的 JSON 规范
# --------------------------------------------------------------------------- #
def _encode_state(value: Any) -> dict[str, Any]:
    """把 description/快照规范化为 ``{"description": str|None, "properties": dict}``。

    支持输入：
    - ``{"description": ..., "properties": {...}}``（标准形态，原样规范化）；
    - ``"新描述"``（supersede 的 before/after 常用字符串）；
    - ``{"w": 1.0}`` 这类纯属性快照 → 视为 properties；
    - ``None`` → 空状态。
    """
    if isinstance(value, str):
        return {"description": value, "properties": {}}
    if isinstance(value, dict):
        if "description" in value or "properties" in value:
            description = value.get("description")
            raw_props = value.get("properties")
            properties = dict(raw_props) if isinstance(raw_props, dict) else {}
            return {
                "description": None if description is None else str(description),
                "properties": properties,
            }
        return {"description": None, "properties": dict(value)}
    if value is None:
        return {"description": None, "properties": {}}
    return {"description": str(value), "properties": {}}


def _decode_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _decode_state(raw: Any) -> dict[str, Any]:
    value = _decode_json(raw, {})
    if isinstance(value, dict):
        description = value.get("description")
        properties = value.get("properties")
        return {
            "description": None if description is None else str(description),
            "properties": dict(properties) if isinstance(properties, dict) else {},
        }
    return {"description": None, "properties": {}}


class SQLiteJournal:
    """单 scope 一个实例（与每 scope 独立 journal.db 的目录布局一致，契约 §0）。"""

    def __init__(
        self,
        db_path: str,
        clock: Optional[Callable[[], float]] = None,
        *,
        retention_days: float = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.db_path = os.path.abspath(db_path)
        self.clock = clock
        self.retention_days = float(retention_days)
        self._lock = threading.RLock()
        self._closed = False
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 生命周期 / 时间
    # ------------------------------------------------------------------ #
    def now(self) -> float:
        return now_ts(self.clock)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    @property
    def retention_seconds(self) -> float:
        return self.retention_days * SECONDS_PER_DAY

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    # ------------------------------------------------------------------ #
    # changes / rollback（R6）
    # ------------------------------------------------------------------ #
    def log_change(
        self,
        scope: str,
        target_id: Optional[str],
        kind: str,
        before: Any,
        after: Any,
        confidence: Optional[float] = None,
        source_turn_id: Optional[str] = None,
    ) -> int:
        """追加 change 审计；before/after 规范为 {description, properties} JSON。"""
        cursor = self._execute(
            "INSERT INTO changes (ts, scope, target_id, kind, before, after, confidence, source_turn_id, rolled_back)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                self.now(),
                scope,
                target_id,
                str(kind),
                jdumps(_encode_state(before)),
                jdumps(_encode_state(after)),
                None if confidence is None else float(confidence),
                source_turn_id,
            ),
        )
        return int(cursor.lastrowid)

    def _row_to_change(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "ts": float(row["ts"]),
            "scope": row["scope"],
            "target_id": row["target_id"],
            "kind": row["kind"],
            "before": _decode_state(row["before"]),
            "after": _decode_state(row["after"]),
            "confidence": row["confidence"],
            "source_turn_id": row["source_turn_id"],
            "rolled_back": bool(row["rolled_back"]),
        }

    def get_change(self, change_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM changes WHERE id = ?", (int(change_id),)).fetchone()
        return self._row_to_change(row) if row is not None else None

    def list_entries(
        self,
        scope: str,
        target_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按 scope（可选 target_id）列 change 审计，最新在前。"""
        sql = "SELECT * FROM changes WHERE scope = ?"
        params: list[Any] = [scope]
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_change(row) for row in rows]

    def rollback(self, change_id: int, graph: Any, now: Optional[float] = None) -> dict[str, Any]:
        """回滚一个 change：7 天内 / 未回滚 / 合法 kind；恢复 before 并同步向量（R6）。

        ``graph`` 是单 scope 的 GraphStore（duck-typed，至少实现 ``edit_entity``）。
        回滚期间不改变 ``rolled_back`` 之外的图谱状态；成功后写一条 kind=rollback 的审计。
        """
        entry = self.get_change(change_id)
        if entry is None:
            raise GraphMemoryError(f"change not found: {change_id}", code="change_not_found")
        if entry["rolled_back"]:
            raise GraphMemoryError(f"change already rolled back: {change_id}", code="already_rolled_back")
        kind = entry["kind"]
        if kind not in ROLLBACK_KINDS:
            raise GraphMemoryError(f"change kind not rollback-able: {kind}", code="rollback_not_allowed")
        now_value = float(now if now is not None else self.now())
        if now_value - float(entry["ts"]) > self.retention_seconds:
            raise GraphMemoryError(
                f"change {change_id} is older than {self.retention_days:g} days",
                code="rollback_expired",
            )
        target_id = entry["target_id"]
        if not target_id:
            raise GraphMemoryError(f"change {change_id} has no target_id", code="rollback_invalid")
        before: dict[str, Any] = entry["before"]
        after: dict[str, Any] = entry["after"]
        description = before.get("description")
        properties = before.get("properties") or {}
        try:
            # R6：edit_entity 内部会重算实体向量，保证旧说法检索不再命中旧值。
            graph.edit_entity(target_id, description=description, properties=properties)
        except GraphMemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 — 后端异常统一转业务错误
            raise GraphMemoryError(f"rollback failed for {target_id}: {exc}", code="rollback_failed") from exc
        with self._lock:
            self._conn.execute("UPDATE changes SET rolled_back = 1 WHERE id = ?", (int(change_id),))
            self._conn.commit()
        # 回滚记录（kind=rollback 不在可回滚集合内，避免递归回滚）
        self.log_change(entry["scope"], target_id, "rollback", after, before, None, None)
        return {
            "restored": {"id": target_id, "before": before, "after": after},
            # 平铺别名（防御性兼容）：service.journal_rollback 目前包装为 {"restored": rollback(...)}，
            # 平铺 id/before/after 让 JSON-RPC 结果 {restored:{id,before,after}} 两种读法都可用。
            "id": target_id,
            "before": before,
            "after": after,
            "change_id": int(change_id),
            "kind": kind,
            "rolled_back_at": now_value,
        }

    # ------------------------------------------------------------------ #
    # activations（R5：只记录注入激活；剪枝冷区判定依据）
    # ------------------------------------------------------------------ #
    def log_activation(
        self,
        scope: str,
        node_id: str,
        reason: str,
        w_before: float,
        w_after: float,
    ) -> None:
        self._execute(
            "INSERT INTO activations (ts, scope, node_id, reason, w_before, w_after) VALUES (?, ?, ?, ?, ?, ?)",
            (self.now(), scope, node_id, reason, float(w_before), float(w_after)),
        )

    def activations(
        self,
        scope: Optional[str] = None,
        *,
        node_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM activations WHERE 1=1"
        params: list[Any] = []
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if node_id is not None:
            sql += " AND node_id = ?"
            params.append(node_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(float(since))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def recently_activated_ids(self, scope: str, since: float) -> set[str]:
        """[since, now] 内被激活过的节点 id 集合（剪枝冷区/免疫判定用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT node_id FROM activations WHERE scope = ? AND ts >= ?",
                (scope, float(since)),
            ).fetchall()
        return {str(row["node_id"]) for row in rows}

    # ------------------------------------------------------------------ #
    # injections
    # ------------------------------------------------------------------ #
    def log_injection(
        self,
        session_id: Optional[str],
        turn: Optional[int],
        tokens: dict[str, Any],
        injected_ids: list[str],
    ) -> int:
        cursor = self._execute(
            "INSERT INTO injections (ts, session_id, turn, tokens_json, injected_ids) VALUES (?, ?, ?, ?, ?)",
            (
                self.now(),
                session_id,
                None if turn is None else int(turn),
                jdumps(tokens or {}),
                jdumps(list(injected_ids or [])),
            ),
        )
        return int(cursor.lastrowid)

    def injection_stats(self, scope: Optional[str] = None) -> dict[str, Any]:
        """注入统计（供 stats/panel_state）。injections 表无 scope 列（契约 §2.7 定表），
        故 ``scope`` 参数仅为接口兼容：全局统计返回；按 scope 的注入归因由 service 汇总。
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM injections ORDER BY id DESC").fetchall()
        if not rows:
            return {
                "count": 0,
                "last_ts": None,
                "last_injection": None,
                "last_tokens": {},
                "last_injected_ids": [],
                "total_memory_tokens": 0,
            }
        last = rows[0]
        last_tokens = _decode_json(last["tokens_json"], {})
        if not isinstance(last_tokens, dict):
            last_tokens = {}
        total_memory = 0
        for row in rows:
            tokens = _decode_json(row["tokens_json"], {})
            if isinstance(tokens, dict):
                try:
                    total_memory += int(tokens.get("memory") or 0)
                except (TypeError, ValueError):
                    continue
        return {
            "count": len(rows),
            "last_ts": float(last["ts"]),
            "last_injection": float(last["ts"]),
            "last_tokens": last_tokens,
            "last_injected_ids": _decode_json(last["injected_ids"], []),
            "total_memory_tokens": total_memory,
        }

    # ------------------------------------------------------------------ #
    # ingests / prune_reports / day_events / alarms
    # ------------------------------------------------------------------ #
    def log_ingest(
        self,
        scope: str,
        turn_id: Optional[str],
        status: str,
        node_count: int,
        edge_count: int,
        reason: str = "",
    ) -> None:
        self._execute(
            "INSERT INTO ingests (ts, scope, turn_id, status, node_count, edge_count, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.now(), scope, turn_id, str(status), int(node_count), int(edge_count), reason or ""),
        )

    def log_prune_report(self, scope: str, kind: str, result: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO prune_reports (ts, scope, kind, result) VALUES (?, ?, ?, ?)",
            (self.now(), scope, str(kind), jdumps(result or {})),
        )

    def prune_history(self, scope: str, limit: int = 50) -> list[dict[str, Any]]:
        """剪枝/压缩报告（最新在前），result 已解码。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prune_reports WHERE scope = ? ORDER BY id DESC LIMIT ?",
                (scope, max(0, int(limit))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "ts": float(row["ts"]),
                "scope": row["scope"],
                "kind": row["kind"],
                "result": _decode_json(row["result"], {}),
            }
            for row in rows
        ]

    def log_day_event(self, scope: str, kind: str, detail: Any = None) -> None:
        self._execute(
            "INSERT INTO day_events (ts, scope, kind, detail) VALUES (?, ?, ?, ?)",
            (self.now(), scope, str(kind), jdumps(detail)),
        )

    def day_events(self, scope: str, days: float = 7.0, limit: int = 200) -> list[dict[str, Any]]:
        since = self.now() - float(days) * SECONDS_PER_DAY
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM day_events WHERE scope = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
                (scope, since, max(0, int(limit))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "ts": float(row["ts"]),
                "scope": row["scope"],
                "kind": row["kind"],
                "detail": _decode_json(row["detail"], None),
            }
            for row in rows
        ]

    def log_alarm(self, scope: str, kind: str, detail: Any) -> None:
        self._execute(
            "INSERT INTO alarms (ts, scope, kind, detail) VALUES (?, ?, ?, ?)",
            (self.now(), scope, str(kind), jdumps(detail)),
        )

    def recent_alarms(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alarms WHERE scope = ? ORDER BY id DESC LIMIT ?",
                (scope, max(0, int(limit))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "ts": float(row["ts"]),
                "scope": row["scope"],
                "kind": row["kind"],
                "detail": _decode_json(row["detail"], None),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # dead_letter（失败 ingest 死信 + 面板重放）
    # ------------------------------------------------------------------ #
    def enqueue_dead_letter(self, scope: str, payload: dict[str, Any], error: str) -> int:
        """ingest 重试 2 次仍失败后落库；返回 dead_letter id（面板可重放）。"""
        cursor = self._execute(
            "INSERT INTO dead_letter (ts, scope, payload, attempts, last_error, replayed)"
            " VALUES (?, ?, ?, 1, ?, 0)",
            (self.now(), scope, jdumps(payload or {}), error or ""),
        )
        return int(cursor.lastrowid)

    def _row_to_dead_letter(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = _decode_json(row["payload"], {})
        return {
            "id": int(row["id"]),
            "ts": float(row["ts"]),
            "scope": row["scope"],
            "payload": payload if isinstance(payload, dict) else {},
            "attempts": int(row["attempts"]),
            "last_error": row["last_error"],
            "replayed": bool(row["replayed"]),
        }

    def get_dead_letter(self, dead_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dead_letter WHERE id = ?", (int(dead_id),)).fetchone()
        return self._row_to_dead_letter(row) if row is not None else None

    def dead_letters(
        self,
        scope: Optional[str] = None,
        include_replayed: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dead_letter WHERE 1=1"
        params: list[Any] = []
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if not include_replayed:
            sql += " AND replayed = 0"
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_dead_letter(row) for row in rows]

    def mark_dead_letter_replayed(self, dead_id: int) -> bool:
        cursor = self._execute(
            "UPDATE dead_letter SET replayed = 1 WHERE id = ?",
            (int(dead_id),),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    def stats(self, scope: Optional[str] = None) -> dict[str, Any]:
        """面板/统计用的各表计数（scope=None 为全局）。"""
        tables = (
            "changes",
            "activations",
            "ingests",
            "prune_reports",
            "day_events",
            "alarms",
            "dead_letter",
            "injections",
        )
        out: dict[str, Any] = {"db": self.db_path}
        with self._lock:
            for table in tables:
                if scope is None or table == "injections":
                    # injections 无 scope 列（契约定表）
                    count = self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                else:
                    count = self._conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE scope = ?", (scope,)
                    ).fetchone()["c"]
                out[table] = int(count)
            if scope is not None:
                out["dead_letter_pending"] = int(
                    self._conn.execute(
                        "SELECT COUNT(*) AS c FROM dead_letter WHERE scope = ? AND replayed = 0",
                        (scope,),
                    ).fetchone()["c"]
                )
        out["db_size"] = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return out
