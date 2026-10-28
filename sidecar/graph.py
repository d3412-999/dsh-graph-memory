"""写时持久化的 embedded 图谱（LightRAG-compatible 语义，图谱是唯一真相源 — R2）。

- 实体/关系 JSON 文件是唯一真相；向量索引是派生结构，可随时重建（不是影子副本）。
- 权重/时间等元数据只存 `properties`（R3）；`description` 永不写入元数据。
- `edit_entity` / `merge_entities` 会同步更新向量索引（R6）。
- 所有 mutation 只改内存 + 标脏；由调用方在请求末尾调用 `save()`（避免 O(N^2) 重写）。
"""
from __future__ import annotations

import copy
import math
import os
import re
import struct
from typing import Any, Iterable, Optional

from base import (
    EMBEDDING_DIM,
    ENTITY_TYPES,
    PROPERTY_KEYS,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    GraphMemoryError,
    append_jsonl,
    embed_sparse,
    log,
    make_chunk,
    make_entity,
    make_relation,
    new_id,
    normalize_space,
    now_ts,
    read_json,
    read_jsonl,
    sparse_cosine,
    tokenize_for_index,
    write_json_atomic,
)

GRAPH_DIR = "graph"
ENTITIES_FILE = "entities.json"
RELATIONS_FILE = "relations.json"
CHUNKS_FILE = os.path.join("chunks", "chunks.jsonl")
VECTOR_DIR = "vectors"
VECTOR_MAGIC = b"GMVEC1\x00"

STATUS_RECALLABLE = (STATUS_ACTIVE, "compressed", "pending_forget", STATUS_ARCHIVED)


class GraphStore:
    """单 scope 的 embedded 图存储。一个实例只承载一个 scope 的图谱。"""

    def __init__(
        self,
        data_home: str,
        scope: str,
        *,
        embedding_dim: int = EMBEDDING_DIM,
        clock: Optional[Any] = None,
    ) -> None:
        self.data_home = os.path.abspath(data_home)
        self.scope = scope
        self.dim = int(embedding_dim)
        self.clock = clock
        self.entities: dict[str, dict[str, Any]] = {}
        self.relations: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.entity_vectors: dict[str, dict[int, float]] = {}
        self.chunk_vectors: dict[str, dict[int, float]] = {}
        self.entity_inverted: dict[int, set[str]] = {}
        self.chunk_inverted: dict[int, set[str]] = {}
        self._entity_name_index: dict[str, str] = {}
        self._name_token_index: dict[str, set[str]] = {}
        self._name_token_index_built = False
        self._dirty = False
        self._vectors_loaded = False
        self._load()

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def graph_dir(self) -> str:
        return os.path.join(self.data_home, GRAPH_DIR)

    @property
    def entities_path(self) -> str:
        return os.path.join(self.graph_dir, ENTITIES_FILE)

    @property
    def relations_path(self) -> str:
        return os.path.join(self.graph_dir, RELATIONS_FILE)

    @property
    def chunks_path(self) -> str:
        return os.path.join(self.data_home, CHUNKS_FILE)

    def _vector_paths(self, kind: str) -> tuple[str, str]:
        base = os.path.join(self.data_home, GRAPH_DIR, VECTOR_DIR)
        return (
            os.path.join(base, f"{kind}.ids.json"),
            os.path.join(base, f"{kind}.vec.bin"),
        )

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        raw = read_json(self.entities_path, {})
        entities = raw.get("entities") if isinstance(raw, dict) else None
        if isinstance(entities, dict):
            for eid, node in entities.items():
                if not isinstance(node, dict):
                    continue
                node.setdefault("properties", {})
                for key in PROPERTY_KEYS:
                    node["properties"].setdefault(key, None)
                node["properties"].setdefault("w", 1.0)
                node["properties"].setdefault("pinned", False)
                node["properties"].setdefault("status", STATUS_ACTIVE)
                self.entities[eid] = node
        raw_rel = read_json(self.relations_path, {})
        relations = raw_rel.get("relations") if isinstance(raw_rel, dict) else None
        if isinstance(relations, dict):
            for rid, rel in relations.items():
                if isinstance(rel, dict):
                    rel.setdefault("properties", {})
                    self.relations[rid] = rel
        for chunk in read_jsonl(self.chunks_path):
            if isinstance(chunk, dict) and chunk.get("id"):
                self.chunks[chunk["id"]] = chunk
        self._rebuild_name_index()
        self._dirty = False

    def _rebuild_name_index(self) -> None:
        self._entity_name_index = {}
        self._name_token_index = {}
        self._name_token_index_built = False
        for eid, node in self.entities.items():
            key = self._name_key(node.get("name", ""))
            if key and key not in self._entity_name_index:
                self._entity_name_index[key] = eid

    @staticmethod
    def _name_key(name: str) -> str:
        return normalize_space((name or "").lower())

    def save(self) -> None:
        """原子写图谱文件；向量索引只在首次需要时保存。"""
        if not self._dirty:
            return
        os.makedirs(self.graph_dir, exist_ok=True)
        write_json_atomic(
            self.entities_path,
            {"version": 1, "scope": self.scope, "entities": self.entities},
        )
        write_json_atomic(
            self.relations_path,
            {"version": 1, "scope": self.scope, "relations": self.relations},
        )
        self._dirty = False

    def _ensure_vectors(self) -> None:
        if self._vectors_loaded:
            return
        self._vectors_loaded = True
        ids_path, vec_path = self._vector_paths("entities")
        chunk_ids_path, chunk_vec_path = self._vector_paths("chunks")
        loaded = self._load_vectors(ids_path, vec_path, self.entities, self.entity_vectors, self.entity_inverted)
        loaded_chunks = self._load_vectors(
            chunk_ids_path, chunk_vec_path, self.chunks, self.chunk_vectors, self.chunk_inverted
        )
        if not loaded:
            for eid, node in self.entities.items():
                self._index_entity(eid, node)
        if not loaded_chunks:
            for cid, chunk in self.chunks.items():
                self._index_chunk(cid, chunk)

    def _load_vectors(
        self,
        ids_path: str,
        vec_path: str,
        expected: dict[str, Any],
        vectors: dict[str, dict[int, float]],
        inverted: dict[int, set[str]],
    ) -> bool:
        try:
            ids = read_json(ids_path, None)
            if not isinstance(ids, list):
                return False
            with open(vec_path, "rb") as fh:
                magic = fh.read(len(VECTOR_MAGIC))
                if magic != VECTOR_MAGIC:
                    return False
                (count,) = struct.unpack("<I", fh.read(4))
                if count != len(ids):
                    return False
                payloads = list(ids)
                for item_id in payloads:
                    (nnz,) = struct.unpack("<I", fh.read(4))
                    vec: dict[int, float] = {}
                    for _ in range(nnz):
                        dim, weight = struct.unpack("<If", fh.read(8))
                        vec[int(dim)] = float(weight)
                    if item_id not in expected:
                        continue
                    vectors[item_id] = vec
                    for dim in vec:
                        inverted.setdefault(dim, set()).add(item_id)
            return True
        except (OSError, ValueError, struct.error):
            vectors.clear()
            inverted.clear()
            return False

    def _save_vectors(self) -> None:
        self._ensure_vectors()
        base = os.path.join(self.data_home, GRAPH_DIR, VECTOR_DIR)
        os.makedirs(base, exist_ok=True)
        for kind, vectors in (("entities", self.entity_vectors), ("chunks", self.chunk_vectors)):
            ids_path, vec_path = self._vector_paths(kind)
            ids = list(vectors.keys())
            write_json_atomic(ids_path, ids)
            tmp = vec_path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(VECTOR_MAGIC)
                fh.write(struct.pack("<I", len(ids)))
                for item_id in ids:
                    vec = vectors[item_id]
                    fh.write(struct.pack("<I", len(vec)))
                    for dim, weight in vec.items():
                        fh.write(struct.pack("<If", int(dim), float(weight)))
            os.replace(tmp, vec_path)

    def rebuild_vectors(self) -> None:
        self.entity_vectors.clear()
        self.chunk_vectors.clear()
        self.entity_inverted.clear()
        self.chunk_inverted.clear()
        self._name_token_index.clear()
        self._name_token_index_built = False
        self._vectors_loaded = True
        for eid, node in self.entities.items():
            self._index_entity(eid, node)
        for cid, chunk in self.chunks.items():
            self._index_chunk(cid, chunk)
        self._save_vectors()

    # ------------------------------------------------------------------ #
    # 向量索引
    # ------------------------------------------------------------------ #
    def _unindex(self, item_id: str, vectors: dict[str, dict[int, float]], inverted: dict[int, set[str]]) -> None:
        vec = vectors.pop(item_id, None)
        if not vec:
            return
        for dim in vec:
            bucket = inverted.get(dim)
            if bucket is not None:
                bucket.discard(item_id)
                if not bucket:
                    inverted.pop(dim, None)

    def _index_doc(
        self,
        item_id: str,
        text: str,
        vectors: dict[str, dict[int, float]],
        inverted: dict[int, set[str]],
    ) -> None:
        self._unindex(item_id, vectors, inverted)
        vec = embed_sparse(text, self.dim)
        if not vec:
            return
        vectors[item_id] = vec
        for dim in vec:
            inverted.setdefault(dim, set()).add(item_id)

    def _index_entity(self, eid: str, node: dict[str, Any]) -> None:
        text = f"{node.get('name', '')}\n{node.get('description', '')}"
        self._index_doc(eid, text, self.entity_vectors, self.entity_inverted)

    def _index_chunk(self, cid: str, chunk: dict[str, Any]) -> None:
        self._index_doc(cid, f"{chunk.get('turn_id', '')}\n{chunk.get('text', '')}", self.chunk_vectors, self.chunk_inverted)

    # ------------------------------------------------------------------ #
    # 基础读取
    # ------------------------------------------------------------------ #
    def get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        node = self.entities.get(entity_id)
        # 深拷贝：调用方可以安全持有快照（Journal rollback / supersede before/after 依赖这一点）
        return copy.deepcopy(node) if node else None

    def find_by_name(self, name: str, scope: Optional[str] = None) -> Optional[dict[str, Any]]:
        eid = self._entity_name_index.get(self._name_key(name))
        return self.get_entity(eid) if eid else None

    def list_entities(
        self,
        scope: Optional[str] = None,
        *,
        status: Optional[str] = None,
        include_archived: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        nodes = [copy.deepcopy(n) for n in self.entities.values()]
        if not include_archived:
            nodes = [n for n in nodes if n.get("properties", {}).get("status") != STATUS_ARCHIVED]
        if status is not None:
            nodes = [n for n in nodes if n.get("properties", {}).get("status") == status]
        nodes.sort(key=lambda n: n["properties"].get("updated_at") or 0, reverse=True)
        if limit:
            nodes = nodes[:limit]
        return nodes

    def count(self, scope: Optional[str] = None, include_archived: bool = False) -> int:
        if include_archived:
            return len(self.entities)
        return sum(
            1 for n in self.entities.values() if n.get("properties", {}).get("status") != STATUS_ARCHIVED
        )

    def relation_count(self) -> int:
        return len(self.relations)

    def neighbors(self, entity_id: str) -> list[str]:
        out: list[str] = []
        for rel in self.relations.values():
            if rel.get("source") == entity_id:
                out.append(rel.get("target", ""))
            elif rel.get("target") == entity_id:
                out.append(rel.get("source", ""))
        return [x for x in out if x]

    def relations_of(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(rel)
            for rel in self.relations.values()
            if rel.get("source") == entity_id or rel.get("target") == entity_id
        ]

    # ------------------------------------------------------------------ #
    # 实体写入
    # ------------------------------------------------------------------ #
    def upsert_entity(
        self,
        name: str,
        etype: str,
        description: str,
        scope: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> str:
        entity_id, _created = self.upsert_entity_ex(name, etype, description, scope, properties)
        return entity_id

    def upsert_entity_ex(
        self,
        name: str,
        etype: str,
        description: str,
        scope: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> tuple[str, bool]:
        ts = now_ts(self.clock)
        node = make_entity(
            name,
            etype,
            description,
            scope or self.scope,
            created_at=ts,
            properties=properties,
        )
        existing_id = self._entity_name_index.get(self._name_key(node["name"]))
        if existing_id and existing_id in self.entities:
            existing = self.entities[existing_id]
            props = existing.setdefault("properties", {})
            if description and description not in existing.get("description", ""):
                merged = f"{existing.get('description', '')}\n{description}".strip()
                existing["description"] = merged[:2000]
            if etype in ENTITY_TYPES:
                existing["type"] = etype
            props["updated_at"] = ts
            for key, value in (properties or {}).items():
                if key in ("source_turn_ids", "source_chunk_ids"):
                    current = props.get(key) or []
                    merged = list(dict.fromkeys([*current, *value]))
                    props[key] = merged
                elif key not in props or props.get(key) is None:
                    props[key] = value
            self._index_entity(existing_id, existing)
            self._index_name(existing_id, str(existing.get("name") or ""))
            self._dirty = True
            return existing_id, False
        self.entities[node["id"]] = node
        self._entity_name_index[self._name_key(node["name"])] = node["id"]
        self._index_entity(node["id"], node)
        self._index_name(node["id"], str(node.get("name") or ""))
        self._dirty = True
        return node["id"], True

    def edit_entity(
        self,
        entity_id: str,
        description: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        node = self.entities.get(entity_id)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        props = node.setdefault("properties", {})
        if description is not None:
            node["description"] = description.strip()
        if properties:
            for key, value in properties.items():
                props[key] = value
        props["updated_at"] = now_ts(self.clock)
        node["properties"]["embed_version"] = int(node["properties"].get("embed_version") or 1) + 1
        # R6：向量库条目同步改写
        self._index_entity(entity_id, node)
        self._dirty = True
        return dict(node)

    def update_properties(self, entity_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """只改属性（权重/激活/状态），不触发向量重算。"""
        node = self.entities.get(entity_id)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        node.setdefault("properties", {}).update(patch)
        self._dirty = True
        return dict(node)

    def delete_entity(self, entity_id: str) -> dict[str, Any]:
        node = self.entities.pop(entity_id, None)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        self._entity_name_index.pop(self._name_key(node.get("name", "")), None)
        self._unindex_name(entity_id, str(node.get("name") or ""))
        self._unindex(entity_id, self.entity_vectors, self.entity_inverted)
        removed_relations: list[str] = []
        for rid, rel in list(self.relations.items()):
            if rel.get("source") == entity_id or rel.get("target") == entity_id:
                self.relations.pop(rid, None)
                removed_relations.append(rid)
        self._dirty = True
        return {"id": entity_id, "removed_relations": removed_relations}

    def merge_entities(self, ids: list[str], keep_id: str) -> dict[str, Any]:
        keep = self.entities.get(keep_id)
        if keep is None:
            raise GraphMemoryError(f"keep entity not found: {keep_id}", code="entity_not_found")
        keep_props = keep.setdefault("properties", {})
        max_w = float(keep_props.get("w") or 1.0)
        last_act = float(keep_props.get("last_activated_at") or 0.0)
        turn_ids = list(keep_props.get("source_turn_ids") or [])
        chunk_ids = list(keep_props.get("source_chunk_ids") or [])
        for other_id in ids:
            if other_id == keep_id:
                continue
            other = self.entities.pop(other_id, None)
            if other is None:
                continue
            self._unindex(other_id, self.entity_vectors, self.entity_inverted)
            self._unindex_name(other_id, str(other.get("name") or ""))
            other_props = other.get("properties", {})
            max_w = max(max_w, float(other_props.get("w") or 0.0))
            last_act = max(last_act, float(other_props.get("last_activated_at") or 0.0))
            turn_ids = list(dict.fromkeys([*turn_ids, *(other_props.get("source_turn_ids") or [])]))
            chunk_ids = list(dict.fromkeys([*chunk_ids, *(other_props.get("source_chunk_ids") or [])]))
            # 关系重连到 keep
            for rel in self.relations.values():
                if rel.get("source") == other_id:
                    rel["source"] = keep_id
                if rel.get("target") == other_id:
                    rel["target"] = keep_id
            self._entity_name_index.pop(self._name_key(other.get("name", "")), None)
        keep_props["w"] = max_w
        keep_props["last_activated_at"] = max(last_act, float(keep_props.get("last_activated_at") or 0.0))
        keep_props["source_turn_ids"] = turn_ids
        keep_props["source_chunk_ids"] = chunk_ids
        keep_props["updated_at"] = now_ts(self.clock)
        self._dirty = True
        return dict(keep)

    # ------------------------------------------------------------------ #
    # 关系写入
    # ------------------------------------------------------------------ #
    def upsert_relation(
        self,
        source: str,
        target: str,
        description: str,
        scope: Optional[str] = None,
        keywords: str = "",
        properties: Optional[dict[str, Any]] = None,
    ) -> str:
        ts = now_ts(self.clock)
        for rid, rel in self.relations.items():
            same = (rel.get("source") == source and rel.get("target") == target) or (
                rel.get("source") == target and rel.get("target") == source
            )
            if same:
                if description and description not in rel.get("description", ""):
                    rel["description"] = f"{rel.get('description', '')}\n{description}".strip()[:2000]
                if keywords:
                    rel["keywords"] = keywords
                rel.setdefault("properties", {})["updated_at"] = ts
                if properties:
                    rel["properties"].update(properties)
                self._dirty = True
                return rid
        rel = make_relation(source, target, description, created_at=ts, keywords=keywords, properties=properties)
        self.relations[rel["id"]] = rel
        self._dirty = True
        return rel["id"]

    def edit_relation(self, rel_id: str, description: Optional[str] = None, properties: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        rel = self.relations.get(rel_id)
        if rel is None:
            raise GraphMemoryError(f"relation not found: {rel_id}", code="relation_not_found")
        if description is not None:
            rel["description"] = description.strip()
        if properties:
            rel.setdefault("properties", {}).update(properties)
        rel.setdefault("properties", {})["updated_at"] = now_ts(self.clock)
        self._dirty = True
        return dict(rel)

    def delete_relation(self, rel_id: str) -> None:
        if self.relations.pop(rel_id, None) is not None:
            self._dirty = True

    # ------------------------------------------------------------------ #
    # 检索（embedded 双层的本地等价：实体层 + 关联扩展 + chunk 层）
    # ------------------------------------------------------------------ #
    def _candidate_ids(self, query_vec: dict[int, float], inverted: dict[int, set[str]], cap: int = 5000) -> set[str]:
        """哈希维度倒排候选：遍历**全部** query 维度（按桶从小到大），而不是只取前 8 个。

        50k 节点时几乎每个维度桶都很大，只取前几个桶会把候选全部交给噪声节点；
        按桶从小到大遍历并在达到 cap 后停止，能让稀有维度命中的节点优先进入候选。
        """
        if not query_vec:
            return set()
        dims = sorted(query_vec.keys(), key=lambda d: len(inverted.get(d, ())))
        candidates: set[str] = set()
        for dim in dims:
            candidates.update(inverted.get(dim, ()))
            if len(candidates) >= cap:
                break
        return set(list(candidates)[:cap])

    @staticmethod
    def _name_tokens(text: str) -> list[str]:
        """名字级倒排键：ASCII 词 + CJK 2-gram（名字短，索引规模可控）。

        与哈希向量互补：哈希维度在 20k 节点时几乎全部碰撞，名字词法倒排能保证
        “名字/关键词与 query 有交集”的高相似节点一定进入候选（D3 硬指标）。
        """
        normalized = normalize_space(text or "").lower()
        tokens: list[str] = []
        ascii_tokens = [t for t in re.split(r"[^a-z0-9_]+", normalized) if t]
        tokens.extend("w:" + token for token in ascii_tokens)
        cjk_chars = [ch for ch in normalized if "\u4e00" <= ch <= "\u9fff"]
        if len(cjk_chars) <= 6:
            tokens.extend("c:" + ch for ch in cjk_chars)
        for i in range(0, max(0, len(cjk_chars) - 1)):
            tokens.append("c:" + "".join(cjk_chars[i : i + 2]))
        return list(dict.fromkeys(tokens))

    def _ensure_name_token_index(self) -> None:
        if self._name_token_index_built:
            return
        self._name_token_index_built = True
        self._name_token_index = {}
        for eid, node in self.entities.items():
            for token in self._name_tokens(str(node.get("name") or "")):
                self._name_token_index.setdefault(token, set()).add(eid)

    def _index_name(self, entity_id: str, name: str) -> None:
        if not self._name_token_index_built:
            return
        for token in self._name_tokens(name):
            self._name_token_index.setdefault(token, set()).add(entity_id)

    def _unindex_name(self, entity_id: str, name: str) -> None:
        if not self._name_token_index_built:
            return
        for token in self._name_tokens(name):
            bucket = self._name_token_index.get(token)
            if bucket is not None:
                bucket.discard(entity_id)
                if not bucket:
                    self._name_token_index.pop(token, None)

    def search_entities(
        self,
        query: str,
        scope: Optional[str] = None,
        top_k: int = 20,
        *,
        include_archived: bool = False,
        statuses: Optional[Iterable[str]] = None,
    ) -> list[tuple[str, float]]:
        self._ensure_vectors()
        self._ensure_name_token_index()
        qvec = embed_sparse(query, self.dim)
        if not qvec:
            return []
        allowed_statuses = set(statuses) if statuses else None
        # 混合候选：名字词法倒排（保证强相关不丢） ∪ 哈希维度倒排（覆盖描述相似）。
        # 20k+ 节点时哈希维度几乎全碰撞，纯维度候选可能把强相关节点挤掉（D3 回归）。
        name_candidates: set[str] = set()
        for token in sorted(self._name_tokens(query), key=lambda t: len(self._name_token_index.get(t, ()))):
            name_candidates.update(self._name_token_index.get(token, ()))
            if len(name_candidates) >= 4000:
                break
        dim_candidates = self._candidate_ids(qvec, self.entity_inverted, cap=3000)
        candidates = name_candidates | dim_candidates
        if not candidates:
            candidates = set(self.entities)
        scored: list[tuple[str, float]] = []
        for eid in candidates:
            node = self.entities.get(eid)
            if node is None:
                continue
            status = node.get("properties", {}).get("status")
            if allowed_statuses is not None:
                if status not in allowed_statuses:
                    continue
            elif not include_archived and status == STATUS_ARCHIVED:
                continue
            vec = self.entity_vectors.get(eid)
            if not vec:
                continue
            score = sparse_cosine(qvec, vec)
            if score > 0:
                scored.append((eid, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def expand_related(self, seed_ids: Iterable[str], top_k: int = 5) -> list[tuple[str, float]]:
        """一层关系扩展：返回 (neighbor_id, 折扣分)。"""
        out: dict[str, float] = {}
        for sid in seed_ids:
            for nid in self.neighbors(sid):
                if nid == sid or nid in out:
                    continue
                out[nid] = 0.5
        return sorted(out.items(), key=lambda item: item[1], reverse=True)[:top_k]

    def add_chunk(
        self,
        text: str,
        scope: Optional[str] = None,
        turn_id: str = "",
        date: str = "",
        *,
        kind: str = "turn",
        chunk_id: Optional[str] = None,
    ) -> str:
        ts = now_ts(self.clock)
        chunk = make_chunk(
            text,
            scope or self.scope,
            turn_id,
            date or "",
            created_at=ts,
            kind=kind,
            chunk_id=chunk_id,
        )
        self.chunks[chunk["id"]] = chunk
        append_jsonl(self.chunks_path, chunk)
        self._ensure_vectors()
        self._index_chunk(chunk["id"], chunk)
        return chunk["id"]

    def get_chunk(self, chunk_id: str) -> Optional[dict[str, Any]]:
        chunk = self.chunks.get(chunk_id)
        return dict(chunk) if chunk else None

    def list_chunks(self, *, day: Optional[str] = None, kind: Optional[str] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
        chunks = [dict(c) for c in self.chunks.values()]
        if day is not None:
            chunks = [c for c in chunks if c.get("date") == day]
        if kind is not None:
            chunks = [c for c in chunks if c.get("kind") == kind]
        chunks.sort(key=lambda c: c.get("created_at") or 0)
        if limit:
            chunks = chunks[-limit:]
        return chunks

    def search_chunks(
        self,
        query: str,
        top_k: int = 3,
        *,
        day: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[tuple[str, float]]:
        self._ensure_vectors()
        qvec = embed_sparse(query, self.dim)
        if not qvec:
            return []
        scored: list[tuple[str, float]] = []
        for cid in self._candidate_ids(qvec, self.chunk_inverted):
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            if day is not None and chunk.get("date") != day:
                continue
            if kind is not None and chunk.get("kind") != kind:
                continue
            score = sparse_cosine(qvec, self.chunk_vectors.get(cid, {}))
            if score > 0:
                scored.append((cid, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------ #
    # 维护辅助
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for node in self.entities.values():
            status = node.get("properties", {}).get("status") or STATUS_ACTIVE
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "scope": self.scope,
            "nodes": self.count(include_archived=True),
            "nodes_live": self.count(),
            "edges": len(self.relations),
            "chunks": len(self.chunks),
            "statuses": statuses,
            "data_home": self.data_home,
        }

    def close(self) -> None:
        self.save()
        self._save_vectors()
