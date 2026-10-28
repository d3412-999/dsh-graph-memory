"""验收证据收集器：test 方法内 `record_evidence(key, value)`，run_acceptance 汇总写 JSON/报告。"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_EVIDENCE: dict[str, dict[str, Any]] = {}
_CURRENT: dict[str, str] = {}


def set_current(test_id: str) -> None:
    """run_acceptance 在测试开始前写入当前 test id（unittest 没有 per-test hook）。"""
    with _LOCK:
        _CURRENT["id"] = test_id


def current_test_id() -> str:
    with _LOCK:
        return _CURRENT.get("id", "")


def record(key: str, value: Any) -> None:
    test_id = current_test_id()
    with _LOCK:
        bucket = _EVIDENCE.setdefault(test_id, {})
        bucket[key] = value


def get(test_id: str) -> dict[str, Any]:
    with _LOCK:
        return dict(_EVIDENCE.get(test_id, {}))


def all_evidence() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {key: dict(value) for key, value in _EVIDENCE.items()}


def reset() -> None:
    with _LOCK:
        _EVIDENCE.clear()
        _CURRENT.clear()
