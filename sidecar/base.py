"""dsh-graph-memory sidecar — 共享基础类型、常量、工具函数。

本模块是各 sidecar 子模块的唯一公共依赖：任何模块都只 import 本模块与标准库。
契约见 docs/CONTRACT.md（团队实现唯一依据）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# 版本 / 常量
# ---------------------------------------------------------------------------
SIDECAR_VERSION = "0.1.0"
SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
SCOPES = (SCOPE_USER, SCOPE_WORKSPACE)

STATUS_ACTIVE = "active"
STATUS_COMPRESSED = "compressed"
STATUS_ARCHIVED = "archived"
STATUS_PENDING_FORGET = "pending_forget"

ENTITY_TYPES = (
    "Preference",
    "Decision",
    "Concept",
    "Tool",
    "Project",
    "Person",
    "Organization",
    "Location",
    "Event",
    "Workflow",
    "Data",
    "Other",
)

SECONDS_PER_DAY = 86400.0
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY


# --------------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------------
class GraphMemoryError(Exception):
    """所有 sidecar 业务错误的基类。"""

    def __init__(self, message: str, code: str = "graph_memory_error") -> None:
        super().__init__(message)
        self.code = code


class GraphBackendUnavailable(GraphMemoryError):
    """LightRAG 后端不可达；调用方可降级到 embedded。"""


class LlmUnavailable(GraphMemoryError):
    """LLM 不可用；调用方必须走确定性降级而不是失败。"""


class RpcError(GraphMemoryError):
    """JSON-RPC 请求错误（携带 JSON-RPC 错误码）。"""

    def __init__(self, message: str, rpc_code: int = -32000, data: Any = None) -> None:
        super().__init__(message, code="rpc_error")
        self.rpc_code = rpc_code
        self.data = data


# --------------------------------------------------------------------------- #
# 时间
# ---------------------------------------------------------------------------
def now_ts(clock: Optional[Callable[[], float]] = None) -> float:
    """当前 epoch 秒；可注入伪时钟供测试模拟 90 天/30 天。"""
    return float(clock() if clock is not None else time.time())


def today_str(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def iso_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


# --------------------------------------------------------------------------- #
# token 计量（与 TS budgeter.estimateTokens 必须逐字节一致）
# ---------------------------------------------------------------------------
def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def estimate_tokens(text: Optional[str]) -> int:
    """确定性 token 估算：CJK 字符 1 token/字，非 CJK 4 字符 1 token（向上取整）。

    TS 侧 src/budgeter.ts 必须实现同一公式；tests/python/test_tokens.py 做跨语言一致性验证。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def truncate_to_tokens(text: Optional[str], limit: int) -> str:
    """按 token 上限截断文本，尽量不切断 CJK 字；返回原文的子串。"""
    if not text or limit <= 0:
        return "" if limit <= 0 else (text or "")
    if estimate_tokens(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        cost = 1 if _is_cjk(ch) else 0.25
        if used + cost > limit:
            break
        used += cost
        out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- #
# 确定性 embedding（派生向量索引，可重建）
# ---------------------------------------------------------------------------
EMBEDDING_DIM = 384
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", re.UNICODE)


def _hash_index(token: str, dim: int) -> int:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "little") % dim


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """确定性字符 n-gram + 词袋哈希 embedding（L2 归一化）。

    - CJK：1/2/3-gram；ASCII 单词：小写整词 + 4-gram；整体 token 参与。
    - 纯标准库、跨平台稳定，用于 embedded 后端的语义相似度（LightRAG 后端用其自身向量库）。
    """
    vec = [0.0] * dim
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    if not normalized.strip():
        return vec
    tokens: list[str] = []
    tokens.extend(_WORD_RE.findall(normalized))
    cjk_chars = [ch for ch in normalized if _is_cjk(ch)]
    for n in (1, 2, 3):
        for i in range(0, max(0, len(cjk_chars) - n + 1)):
            tokens.append("cjk:" + "".join(cjk_chars[i : i + n]))
    for word in _WORD_RE.findall(normalized):
        if word.isascii():
            w = word.lower()
            for n in (4,):
                for i in range(0, max(0, len(w) - n + 1)):
                    tokens.append("g:" + w[i : i + n])
    for tok in tokens:
        h = _hash_index(tok, dim)
        vec[h] += 1.0
    # L2 归一化
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _terms(text: str) -> list[str]:
    """embedding 使用的 token 列表（dense 与 sparse 共用，保证同一向量语义）。"""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    tokens: list[str] = list(_WORD_RE.findall(normalized))
    cjk_chars = [ch for ch in normalized if _is_cjk(ch)]
    for n in (1, 2, 3):
        for i in range(0, max(0, len(cjk_chars) - n + 1)):
            tokens.append("cjk:" + "".join(cjk_chars[i : i + n]))
    for word in _WORD_RE.findall(normalized):
        if word.isascii():
            w = word.lower()
            for i in range(0, max(0, len(w) - 4 + 1)):
                tokens.append("g:" + w[i : i + 4])
    return tokens


def embed_sparse(text: str, dim: int = EMBEDDING_DIM) -> dict[int, float]:
    """稀疏哈希向量（L2 归一化）：{维度: 权重}。

    embedded 图谱的向量索引使用稀疏表示：倒排表 + 共享维度点积，
    50k 节点下仍能把召回 P95 控制在预算内（无第三方依赖）。
    """
    weights: dict[int, float] = {}
    for tok in _terms(text):
        h = _hash_index(tok, dim)
        weights[h] = weights.get(h, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in weights.values()))
    if norm <= 0:
        return {}
    return {k: v / norm for k, v in weights.items()}


def sparse_cosine(a: dict[int, float], b: dict[int, float]) -> float:
    """两个稀疏单位向量的点积（= cosine）。迭代较短的一方。"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return float(sum(v * b.get(k, 0.0) for k, v in a.items()))


def tokenize_for_index(text: str, dim: int = EMBEDDING_DIM) -> list[int]:
    """倒排索引键：稀疏向量的维度列表（去重，按维度升序）。"""
    return sorted({_hash_index(tok, dim) for tok in _terms(text)})


# --------------------------------------------------------------------------- #
# JSON / 原子写
# ---------------------------------------------------------------------------
def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_jsonl(path: str, record: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
        fh.flush()


def read_jsonl(path: str) -> list[Any]:
    rows: list[Any] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return rows
    return rows


def new_id(prefix: str) -> str:
    return f"{prefix}_{os.urandom(8).hex()}"


def jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# --------------------------------------------------------------------------- #
# 日志
# ---------------------------------------------------------------------------
def log(level: str, message: str, **fields: Any) -> None:
    payload = {"ts": round(time.time(), 3), "level": level, "msg": message}
    if fields:
        payload.update(fields)
    line = "[graph-memory] " + jdumps(payload) + "\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    log_file = os.environ.get("GRAPH_MEMORY_LOG_FILE")
    if log_file:
        try:
            directory = os.path.dirname(os.path.abspath(log_file))
            os.makedirs(directory, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 数据模型（dict 规范，禁止 dataclass 直接落盘以保持 JSON 可读）
# ---------------------------------------------------------------------------
PROPERTY_KEYS = (
    "w",
    "last_activated_at",
    "created_at",
    "updated_at",
    "valid_from",
    "pinned",
    "status",
    "protect_until",
    "compressed_at",
    "archived_at",
    "workspace_hit",
    "source_turn_ids",
    "source_chunk_ids",
    "embed_version",
)


def make_entity(
    name: str,
    etype: str,
    description: str,
    scope: str,
    *,
    created_at: float,
    properties: Optional[dict[str, Any]] = None,
    entity_id: Optional[str] = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "w": 1.0,
        "last_activated_at": created_at,
        "created_at": created_at,
        "updated_at": created_at,
        "valid_from": today_str(created_at),
        "pinned": False,
        "status": STATUS_ACTIVE,
        "protect_until": None,
        "compressed_at": None,
        "archived_at": None,
        "workspace_hit": False,
        "source_turn_ids": [],
        "source_chunk_ids": [],
        "embed_version": 1,
    }
    if properties:
        props.update(properties)
    return {
        "id": entity_id or new_id("ent"),
        "name": normalize_space(name)[:120],
        "type": etype if etype in ENTITY_TYPES else "Other",
        "description": (description or "").strip(),
        "scope": scope,
        "properties": props,
    }


def make_relation(
    source: str,
    target: str,
    description: str,
    *,
    created_at: float,
    keywords: str = "",
    properties: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "created_at": created_at,
        "updated_at": created_at,
        "weight": 1.0,
    }
    if properties:
        props.update(properties)
    return {
        "id": new_id("rel"),
        "source": source,
        "target": target,
        "description": (description or "").strip(),
        "keywords": keywords or "",
        "weight": 1.0,
        "properties": props,
    }


def make_chunk(
    text: str,
    scope: str,
    turn_id: str,
    date: str,
    *,
    created_at: float,
    kind: str = "turn",
    chunk_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id or new_id("chk"),
        "scope": scope,
        "turn_id": turn_id,
        "date": date,
        "text": text,
        "kind": kind,
        "created_at": created_at,
    }


# --------------------------------------------------------------------------- #
# 关键词检查（改口/否定词表；只作加权信号，最终由 LLM 判定 — 契约 §2.4）
# ---------------------------------------------------------------------------
NEGATION_MARKERS = (
    "不",
    "不再",
    "别",
    "取消",
    "换成",
    "改回",
    "之前说错",
    "从现在起",
    "改成",
    "改为",
    "no longer",
    "not anymore",
    "instead of",
    "rather than",
    "revert",
    "used to",
    "correction",
)


def has_negation_marker(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in NEGATION_MARKERS)


# --------------------------------------------------------------------------- #
# 配置访问辅助
# --------------------------------------------------------------------------- #
class Config:
    """只读配置视图：cfg.get('a.b.c', default)。"""

    def __init__(self, data: dict[str, Any], *, base_dir: Optional[str] = None) -> None:
        self.data = data or {}
        self.base_dir = base_dir or os.getcwd()

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, path: str) -> "Config":
        value = self.get(path, {})
        return Config(value if isinstance(value, dict) else {}, base_dir=self.base_dir)


# --------------------------------------------------------------------------- #
# 供 llm_bridge 使用的离线规则接口（extract.py 实现；避免循环 import）
# ---------------------------------------------------------------------------
OfflineRules = Any  # runtime object with: extract(text, scope) -> list[dict]; summarize(text, max_tokens) -> str; judge(before, after, context) -> dict


def import_offline_rules() -> OfflineRules:
    from extract import offline_rules  # type: ignore  # noqa: PLC0415  (sidecar 运行目录在 sys.path)

    return offline_rules
