"""LightRAG HTTP 后端适配器（生产用；与 embedded ``graph.GraphStore`` 同接口 — duck-typing）。

协议映射（本插件 → LightRAG 网关）::

    POST {base}/insert                                     写原文 chunk
    POST {base}/query          {query, mode:"mix", top_k:20, only_need_context:true}
    POST {base}/graph/entity/create                        新建实体
    POST {base}/graph/entity/edit                          更新实体
    POST {base}/graph/entity/delete                        删除实体
    POST {base}/graph/relation/create|edit|delete          关系维护
    POST {base}/graph/entities/merge                       实体合并
    GET  {base}/graph/label/list                           实体名列表（读取扩展）
    GET  {base}/graph/entity/exists                        按名存在性（读取扩展）
    GET  {base}/graph/entities/<id>                        按 id 读取（读取扩展，404→None）
    GET  {base}/graphs?label=<name>                        邻接读取（读取扩展）

关键契约：
- R2：远端图谱是唯一真相；本类的 ``_nodes/_relations/_chunks`` 只是写穿透的**只读派生缓存**
  （id↔name 映射与回显），不是第二份可写副本。原文 chunk 同时按契约落本地 ``chunks/chunks.jsonl``
  （原文缓存允许，不是图副本）。
- R3：``description`` 只写描述；权重/时间/pin 等元数据只进 ``node_data.properties``。
- R6：LightRAG 无独立向量更新端点，``edit_entity`` 依次执行 edit → delete → create 强制后端
  对本条实体重嵌入，保证旧说法检索不再命中旧值（delete+create 的代价与理由见方法 docstring）。
- 不可达语义：连接失败/超时/非 2xx/非法 JSON 一律抛 ``GraphBackendUnavailable``（不泄漏
  urllib 异常、不崩溃进程）；调用方（service/retrieval）可降级到 embedded 或空结果。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Optional

from base import (
    GraphBackendUnavailable,
    GraphMemoryError,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    append_jsonl,
    jdumps,
    log,
    make_chunk,
    make_entity,
    new_id,
    normalize_space,
    now_ts,
    read_jsonl,
)

CHUNKS_FILE = os.path.join("chunks", "chunks.jsonl")
DEFAULT_QUERY_MODE = "mix"
DEFAULT_QUERY_TOP_K = 20
RELATION_DISCOUNT = 0.5
_ENTITY_ID_RE = re.compile(r"ent_[0-9a-f]{16}")


def _name_key(name: str) -> str:
    return normalize_space(name or "").lower()


def _props(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    props = node.get("properties")
    return props if isinstance(props, dict) else {}


class LightRAGStore:
    """单 scope 的 LightRAG 图适配器（同 embedded GraphStore 方法签名）。"""

    backend = "lightrag"

    def __init__(
        self,
        data_home: str,
        scope: str,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.data_home = os.path.abspath(data_home)
        self.scope = scope
        if not base_url:
            raise GraphBackendUnavailable("lightrag base_url is not configured", code="lightrag_not_configured")
        self.base_url = str(base_url).rstrip("/")
        self.api_key = api_key or ""
        self.timeout = max(0.1, float(timeout))
        self.clock = clock
        # 写穿透只读派生缓存（R2：不是可写影子副本）
        self._nodes: dict[str, dict[str, Any]] = {}
        self._relations: dict[str, dict[str, Any]] = {}
        self._chunks: dict[str, dict[str, Any]] = {}
        self._name_index: dict[str, str] = {}
        self.chunks = self._chunks  # service 的 getattr(graph, "chunks", {}) 兜底用
        self._load_chunks()

    # ------------------------------------------------------------------ #
    # HTTP 基础设施
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        timeout: Optional[float] = None,
        allow_404: bool = False,
    ) -> Any:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        data: Optional[bytes] = None
        if payload is not None:
            data = jdumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            if allow_404 and exc.code == 404:
                return None
            raise GraphBackendUnavailable(
                f"lightrag {method} {path} failed: HTTP {exc.code} {body}".strip(),
                code="lightrag_http_error",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GraphBackendUnavailable(
                f"lightrag {method} {path} unreachable: {exc}", code="lightrag_unreachable"
            ) from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphBackendUnavailable(
                f"lightrag {method} {path} returned invalid JSON", code="lightrag_bad_json"
            ) from exc

    # ------------------------------------------------------------------ #
    # 节点缓存 / 载荷
    # ------------------------------------------------------------------ #
    def _cache_node(self, node: dict[str, Any]) -> dict[str, Any]:
        self._nodes[node["id"]] = node
        key = _name_key(node.get("name", ""))
        if key:
            self._name_index[key] = node["id"]
        return node

    def _node_payload(self, node: dict[str, Any], *, include_description: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entity_name": node.get("name", ""),
            "entity_type": node.get("type") or "Other",
            "node_data": {
                "id": node.get("id"),
                "scope": node.get("scope", self.scope),
                "properties": dict(_props(node)),
            },
        }
        if include_description:
            payload["description"] = node.get("description") or ""
        return payload

    def _resolve_entity_ref(self, ref: Any) -> Optional[str]:
        if not ref:
            return None
        text = str(ref)
        if text in self._nodes:
            return text
        return self._name_index.get(_name_key(text))

    def _name_for(self, entity_id: str) -> Optional[str]:
        node = self._nodes.get(entity_id)
        if node is None:
            node = self._read_node(entity_id)
        return node.get("name") if node else None

    def _node_from_remote(self, data: Any) -> Optional[dict[str, Any]]:
        if not isinstance(data, dict):
            return None
        name = data.get("name") or data.get("entity_name") or data.get("label")
        if not name:
            return None
        node_data = data.get("node_data") if isinstance(data.get("node_data"), dict) else {}
        properties = node_data.get("properties")
        if not isinstance(properties, dict):
            properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
        entity_id = data.get("id") or node_data.get("id") or new_id("ent")
        node = {
            "id": str(entity_id),
            "name": normalize_space(str(name))[:120],
            "type": data.get("type") or data.get("entity_type") or "Other",
            "description": str(data.get("description") or "").strip(),
            "scope": data.get("scope") or node_data.get("scope") or self.scope,
            "properties": dict(properties),
        }
        node["properties"].setdefault("w", 1.0)
        node["properties"].setdefault("pinned", False)
        node["properties"].setdefault("status", STATUS_ACTIVE)
        return node

    def _read_node(self, entity_id: str) -> Optional[dict[str, Any]]:
        """按 id 读取（读取扩展端点；404 视为不存在，网络错误抛 GraphBackendUnavailable）。"""
        data = self._request(
            "GET",
            f"/graph/entities/{urllib.parse.quote(str(entity_id), safe='')}",
            allow_404=True,
        )
        node = self._node_from_remote(data)
        if node is not None:
            self._cache_node(node)
        return node

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
        """新建或按 name 合并（与 GraphStore 语义一致），返回 (id, created)。"""
        ts = now_ts(self.clock)
        node = make_entity(
            name,
            etype,
            description,
            scope or self.scope,
            created_at=ts,
            properties=properties,
        )
        existing_id = self._name_index.get(_name_key(node["name"]))
        if existing_id and existing_id in self._nodes:
            existing = self._nodes[existing_id]
            merged_description = existing.get("description", "")
            if description and description not in merged_description:
                merged_description = f"{merged_description}\n{description}".strip()[:2000]
            merged_props = dict(_props(existing))
            for key, value in (properties or {}).items():
                if key in ("source_turn_ids", "source_chunk_ids"):
                    merged_props[key] = list(dict.fromkeys([*(merged_props.get(key) or []), *value]))
                elif merged_props.get(key) is None:
                    merged_props[key] = value
            self.edit_entity(existing_id, description=merged_description, properties=merged_props)
            return existing_id, False
        self._request("POST", "/graph/entity/create", self._node_payload(node))
        self._cache_node(node)
        return node["id"], True

    def edit_entity(
        self,
        entity_id: str,
        description: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """更新实体（R6：后端无向量更新端点，故 edit → delete → create 强制重嵌入）。

        delete+create 的代价：该实体的一条重嵌入，换取“旧说法检索不得命中旧值”的 R6 保证；
        这是 LightRAG HTTP 层唯一可靠的向量失效方式。
        """
        node = self._nodes.get(entity_id) or self._read_node(entity_id)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        updated = {
            **node,
            "properties": dict(_props(node)),
            "description": node.get("description", ""),
        }
        if description is not None:
            updated["description"] = description.strip()
        if properties:
            updated["properties"].update(properties)
        updated["properties"]["updated_at"] = now_ts(self.clock)
        updated["properties"]["embed_version"] = int(updated["properties"].get("embed_version") or 1) + 1
        updated["scope"] = node.get("scope", self.scope)
        # ① 更新描述/元数据
        self._request(
            "POST",
            "/graph/entity/edit",
            {
                "entity_name": updated["name"],
                "updated_data": {
                    "description": updated["description"],
                    "node_data": self._node_payload(updated)["node_data"],
                },
            },
        )
        # ② R6：delete + create 强制后端重嵌入（LightRAG 无独立向量更新端点）
        self._request("POST", "/graph/entity/delete", {"entity_name": updated["name"]})
        self._request("POST", "/graph/entity/create", self._node_payload(updated))
        self._cache_node(updated)
        return dict(updated)

    def update_properties(self, entity_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """只改属性（权重/激活/状态）：后端无向量变化，仅 edit 不重嵌入。"""
        node = self._nodes.get(entity_id) or self._read_node(entity_id)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        updated = {**node, "properties": {**_props(node), **dict(patch or {})}}
        self._request(
            "POST",
            "/graph/entity/edit",
            {
                "entity_name": updated["name"],
                "updated_data": {
                    "description": updated.get("description", ""),
                    "node_data": self._node_payload(updated)["node_data"],
                },
            },
        )
        self._cache_node(updated)
        return dict(updated)

    def delete_entity(self, entity_id: str) -> dict[str, Any]:
        node = self._nodes.get(entity_id) or self._read_node(entity_id)
        if node is None:
            raise GraphMemoryError(f"entity not found: {entity_id}", code="entity_not_found")
        self._request("POST", "/graph/entity/delete", {"entity_name": node.get("name", "")})
        removed_relations = [
            rid
            for rid, rel in self._relations.items()
            if rel.get("source") == entity_id or rel.get("target") == entity_id
        ]
        for rid in removed_relations:
            self._relations.pop(rid, None)
        self._nodes.pop(entity_id, None)
        key = _name_key(node.get("name", ""))
        if self._name_index.get(key) == entity_id:
            self._name_index.pop(key, None)
        return {"id": entity_id, "removed_relations": removed_relations}

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        node = self._nodes.get(entity_id)
        if node is not None:
            return dict(node)
        node = self._read_node(entity_id)
        return dict(node) if node is not None else None

    def find_by_name(self, name: str, scope: Optional[str] = None) -> Optional[dict[str, Any]]:
        entity_id = self._name_index.get(_name_key(name))
        return self.get_entity(entity_id) if entity_id else None

    def _remote_names(self) -> list[str]:
        data = self._request("GET", "/graph/label/list")
        return _extract_names(data)

    def list_entities(
        self,
        scope: Optional[str] = None,
        *,
        status: Optional[str] = None,
        include_archived: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        try:
            names = self._remote_names()
        except GraphMemoryError as exc:
            log("warning", "lightrag label/list unavailable; use local cache", error=str(exc))
            names = [node.get("name", "") for node in self._nodes.values()]
        nodes: list[dict[str, Any]] = []
        for name in names:
            entity_id = self._name_index.get(_name_key(name))
            node = self._nodes.get(entity_id) if entity_id else None
            if node is None:
                node = {
                    "id": entity_id or name,
                    "name": name,
                    "type": "Other",
                    "description": "",
                    "scope": self.scope,
                    "properties": {"status": STATUS_ACTIVE},
                }
            node = dict(node)
            props = _props(node)
            if scope is not None and node.get("scope") != scope:
                continue
            if not include_archived and props.get("status") == STATUS_ARCHIVED:
                continue
            if status is not None and props.get("status") != status:
                continue
            nodes.append(node)
        nodes.sort(key=lambda item: _props(item).get("updated_at") or 0, reverse=True)
        if limit:
            nodes = nodes[: int(limit)]
        return nodes

    def count(self, scope: Optional[str] = None, include_archived: bool = False) -> int:
        try:
            names = self._remote_names()
        except GraphMemoryError as exc:
            log("warning", "lightrag count unavailable; use local cache", error=str(exc))
            names = [node.get("name", "") for node in self._nodes.values()]
        if scope is None and include_archived:
            return len(names)
        matched = 0
        for name in names:
            entity_id = self._name_index.get(_name_key(name))
            node = self._nodes.get(entity_id) if entity_id else None
            if node is None:
                matched += 1  # 远端有、缓存未知：按活跃计入
                continue
            props = _props(node)
            if scope is not None and node.get("scope") != scope:
                continue
            if not include_archived and props.get("status") == STATUS_ARCHIVED:
                continue
            matched += 1
        return matched

    def relation_count(self) -> int:
        return len(self._relations)

    def neighbors(self, entity_id: str) -> list[str]:
        out: list[str] = []
        for rel in self._relations.values():
            if rel.get("source") == entity_id:
                other = rel.get("target")
            elif rel.get("target") == entity_id:
                other = rel.get("source")
            else:
                continue
            if other and other not in out:
                out.append(other)
        if out:
            return out
        # 读取扩展：远端邻接（失败则降级为空，不阻塞检索）
        node = self._nodes.get(entity_id)
        if node is None:
            return []
        try:
            data = self._request(
                "GET", f"/graphs?label={urllib.parse.quote(str(node.get('name')), safe='')}"
            )
        except GraphMemoryError as exc:
            log("warning", "lightrag /graphs unavailable; neighbors degraded", error=str(exc))
            return []
        for edge in _extract_edges(data):
            source, target = edge
            if _name_key(source) == _name_key(node.get("name", "")):
                other_name = target
            elif _name_key(target) == _name_key(node.get("name", "")):
                other_name = source
            else:
                continue
            other_id = self._resolve_entity_ref(other_name) or str(other_name)
            if other_id != entity_id and other_id not in out:
                out.append(other_id)
        return out

    def relations_of(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            dict(rel)
            for rel in self._relations.values()
            if rel.get("source") == entity_id or rel.get("target") == entity_id
        ]

    def expand_related(self, seed_ids: Iterable[str], top_k: int = 5) -> list[tuple[str, float]]:
        out: dict[str, float] = {}
        for sid in seed_ids:
            for nid in self.neighbors(sid):
                if nid == sid or nid in out:
                    continue
                out[nid] = RELATION_DISCOUNT
        return sorted(out.items(), key=lambda item: item[1], reverse=True)[:top_k]

    # ------------------------------------------------------------------ #
    # 关系写入
    # ------------------------------------------------------------------ #
    def _relation_pair(self, source: str, target: str) -> Optional[dict[str, Any]]:
        for rel in self._relations.values():
            if (rel.get("source") == source and rel.get("target") == target) or (
                rel.get("source") == target and rel.get("target") == source
            ):
                return rel
        return None

    def upsert_relation(
        self,
        source: str,
        target: str,
        description: str,
        scope: Optional[str] = None,
        keywords: str = "",
        properties: Optional[dict[str, Any]] = None,
    ) -> str:
        existing = self._relation_pair(source, target)
        if existing is not None:
            merged_description = existing.get("description", "")
            if description and description not in merged_description:
                merged_description = f"{merged_description}\n{description}".strip()[:2000]
            merged_props = {**_props_rel(existing), **dict(properties or {})}
            self.edit_relation(existing["id"], description=merged_description, properties=merged_props)
            return existing["id"]
        source_name = self._name_for(source)
        target_name = self._name_for(target)
        if not source_name or not target_name:
            raise GraphMemoryError(
                "relation endpoints must exist before creating relation", code="entity_not_found"
            )
        ts = now_ts(self.clock)
        rel = {
            "id": new_id("rel"),
            "source": source,
            "target": target,
            "description": (description or "").strip(),
            "keywords": keywords or "",
            "weight": 1.0,
            "properties": {"created_at": ts, "updated_at": ts, "weight": 1.0, **(properties or {})},
        }
        self._request(
            "POST",
            "/graph/relation/create",
            {
                "source_id": source_name,
                "target_id": target_name,
                "description": rel["description"],
                "keywords": rel["keywords"],
                "edge_data": {"properties": dict(rel["properties"])},
            },
        )
        self._relations[rel["id"]] = rel
        return rel["id"]

    def edit_relation(
        self,
        rel_id: str,
        description: Optional[str] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        rel = self._relations.get(rel_id)
        if rel is None:
            raise GraphMemoryError(f"relation not found: {rel_id}", code="relation_not_found")
        updated = {**rel, "properties": {**_props_rel(rel), **dict(properties or {})}}
        if description is not None:
            updated["description"] = description.strip()
        updated["properties"]["updated_at"] = now_ts(self.clock)
        source_name = self._name_for(rel["source"])
        target_name = self._name_for(rel["target"])
        if not source_name or not target_name:
            raise GraphMemoryError(
                f"relation endpoints unavailable: {rel_id}", code="entity_not_found"
            )
        self._request(
            "POST",
            "/graph/relation/edit",
            {
                "source_id": source_name,
                "target_id": target_name,
                "updated_data": {
                    "description": updated.get("description", ""),
                    "keywords": updated.get("keywords", ""),
                    "edge_data": {"properties": dict(updated["properties"])},
                },
            },
        )
        self._relations[rel_id] = updated
        return dict(updated)

    def delete_relation(self, rel_id: str) -> None:
        rel = self._relations.pop(rel_id, None)
        if rel is None:
            return
        source_name = self._name_for(rel.get("source", ""))
        target_name = self._name_for(rel.get("target", ""))
        if not source_name or not target_name:
            return
        self._request(
            "POST",
            "/graph/relation/delete",
            {"source_id": source_name, "target_id": target_name},
        )

    def merge_entities(self, ids: list[str], keep_id: str) -> dict[str, Any]:
        keep = self._nodes.get(keep_id) or self._read_node(keep_id)
        if keep is None:
            raise GraphMemoryError(f"keep entity not found: {keep_id}", code="entity_not_found")
        keep = dict(keep)
        keep["properties"] = dict(_props(keep))
        change_names: list[str] = []
        max_w = float(keep["properties"].get("w") or 1.0)
        last_activated = float(keep["properties"].get("last_activated_at") or 0.0)
        turn_ids = list(keep["properties"].get("source_turn_ids") or [])
        chunk_ids = list(keep["properties"].get("source_chunk_ids") or [])
        for other_id in ids:
            if other_id == keep_id:
                continue
            other = self._nodes.get(other_id) or self._read_node(other_id)
            if other is None:
                continue
            change_names.append(str(other.get("name", "")))
            other_props = _props(other)
            max_w = max(max_w, float(other_props.get("w") or 0.0))
            last_activated = max(last_activated, float(other_props.get("last_activated_at") or 0.0))
            turn_ids = list(dict.fromkeys([*turn_ids, *(other_props.get("source_turn_ids") or [])]))
            chunk_ids = list(dict.fromkeys([*chunk_ids, *(other_props.get("source_chunk_ids") or [])]))
            self._nodes.pop(other_id, None)
            key = _name_key(other.get("name", ""))
            if self._name_index.get(key) == other_id:
                self._name_index.pop(key, None)
            for rel in self._relations.values():
                if rel.get("source") == other_id:
                    rel["source"] = keep_id
                if rel.get("target") == other_id:
                    rel["target"] = keep_id
        self._request(
            "POST",
            "/graph/entities/merge",
            {"entities_to_change": change_names, "entity_to_keep": keep.get("name", "")},
        )
        keep["properties"]["w"] = max_w
        keep["properties"]["last_activated_at"] = max(last_activated, float(keep["properties"].get("last_activated_at") or 0.0))
        keep["properties"]["source_turn_ids"] = turn_ids
        keep["properties"]["source_chunk_ids"] = chunk_ids
        keep["properties"]["updated_at"] = now_ts(self.clock)
        self._cache_node(keep)
        return dict(keep)

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def _query_payload(self, query: str, *, top_k: int) -> dict[str, Any]:
        return {
            "query": query,
            "mode": DEFAULT_QUERY_MODE,
            "top_k": max(int(top_k), DEFAULT_QUERY_TOP_K),
            "only_need_context": True,
        }

    def _parse_entity_results(
        self,
        data: Any,
        *,
        top_k: int,
        include_archived: bool,
        statuses: Optional[Iterable[str]],
    ) -> list[tuple[str, float]]:
        raw_items: list[Any] = []
        if isinstance(data, dict):
            for key in ("entities", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    raw_items = value
                    break
            if not raw_items:
                context = data.get("response") or data.get("context")
                if isinstance(context, str) and context:
                    # 只有 context 文本时：按已知实体名/id 做匹配，给中性分
                    for name in self._name_index:
                        node = self._nodes.get(self._name_index[name])
                        if node and node.get("name") and node["name"] in context:
                            raw_items.append({"entity_name": node["name"], "score": 0.5})
                    for match in _ENTITY_ID_RE.findall(context):
                        if match in self._nodes:
                            raw_items.append({"id": match, "score": 0.5})
        elif isinstance(data, list):
            raw_items = data
        allowed = set(statuses) if statuses else None
        scored: dict[str, float] = {}
        for item in raw_items:
            if isinstance(item, str):
                ref: Any = item
                sim = 0.5
            elif isinstance(item, dict):
                ref = item.get("id") or item.get("entity_id") or item.get("entity_name") or item.get("name")
                sim = _to_float(item.get("score") or item.get("similarity") or item.get("weight"), 0.0)
            else:
                continue
            entity_id = self._resolve_entity_ref(ref)
            if not entity_id:
                continue
            node = self._nodes.get(entity_id)
            if node is not None:
                node_props = _props(node)
                if node_props.get("status") == STATUS_ARCHIVED and not include_archived:
                    continue
                if allowed is not None and node_props.get("status") not in allowed:
                    continue
            if sim <= 0:
                continue
            scored[entity_id] = max(scored.get(entity_id, 0.0), sim)
        ordered = sorted(scored.items(), key=lambda item: item[1], reverse=True)
        return ordered[: max(1, int(top_k))]

    def search_entities(
        self,
        query: str,
        scope: Optional[str] = None,
        top_k: int = 20,
        *,
        include_archived: bool = False,
        statuses: Optional[Iterable[str]] = None,
    ) -> list[tuple[str, float]]:
        data = self._request("POST", "/query", self._query_payload(query, top_k=top_k))
        return self._parse_entity_results(
            data,
            top_k=top_k,
            include_archived=include_archived,
            statuses=statuses,
        )

    def _parse_chunk_results(self, data: Any, *, top_k: int, day: Optional[str], kind: Optional[str]) -> list[tuple[str, float]]:
        raw_items: list[Any] = []
        if isinstance(data, dict):
            for key in ("chunks", "sources", "chunk_items"):
                value = data.get(key)
                if isinstance(value, list):
                    raw_items = value
                    break
        elif isinstance(data, list):
            raw_items = data
        scored: dict[str, float] = {}
        for item in raw_items:
            if isinstance(item, str):
                chunk_id, sim = item, 0.5
            elif isinstance(item, dict):
                chunk_id = item.get("id") or item.get("chunk_id")
                sim = _to_float(item.get("score") or item.get("similarity"), 0.0)
            else:
                continue
            chunk = self._chunks.get(str(chunk_id)) if chunk_id else None
            if chunk is None:
                continue
            if day is not None and chunk.get("date") != day:
                continue
            if kind is not None and chunk.get("kind") != kind:
                continue
            if sim <= 0:
                continue
            scored[str(chunk_id)] = max(scored.get(str(chunk_id), 0.0), sim)
        ordered = sorted(scored.items(), key=lambda item: item[1], reverse=True)
        return ordered[: max(1, int(top_k))]

    def search_chunks(
        self,
        query: str,
        top_k: int = 3,
        *,
        day: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[tuple[str, float]]:
        data = self._request("POST", "/query", self._query_payload(query, top_k=top_k))
        results = self._parse_chunk_results(data, top_k=top_k, day=day, kind=kind)
        if results:
            return results
        # 本地原文缓存兜底（chunks 非图副本，允许）；仅做包含匹配
        query_key = normalize_space(query or "").lower()
        if not query_key:
            return []
        fallback: list[tuple[str, float]] = []
        for chunk_id, chunk in self._chunks.items():
            if day is not None and chunk.get("date") != day:
                continue
            if kind is not None and chunk.get("kind") != kind:
                continue
            if query_key in normalize_space(chunk.get("text", "")).lower():
                fallback.append((chunk_id, 0.5))
        return fallback[: max(1, int(top_k))]

    # ------------------------------------------------------------------ #
    # 原文 chunk
    # ------------------------------------------------------------------ #
    def _chunks_path(self) -> str:
        return os.path.join(self.data_home, CHUNKS_FILE)

    def _load_chunks(self) -> None:
        for chunk in read_jsonl(self._chunks_path()):
            if isinstance(chunk, dict) and chunk.get("id"):
                self._chunks[chunk["id"]] = chunk

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
        try:
            self._request(
                "POST",
                "/insert",
                {
                    "text": chunk["text"],
                    "chunk_id": chunk["id"],
                    "metadata": {
                        "scope": chunk["scope"],
                        "turn_id": chunk["turn_id"],
                        "date": chunk["date"],
                        "kind": chunk["kind"],
                    },
                },
            )
        except GraphMemoryError:
            raise
        self._chunks[chunk["id"]] = chunk
        append_jsonl(self._chunks_path(), chunk)
        return chunk["id"]

    def get_chunk(self, chunk_id: str) -> Optional[dict[str, Any]]:
        chunk = self._chunks.get(chunk_id)
        return dict(chunk) if chunk else None

    def list_chunks(
        self,
        *,
        day: Optional[str] = None,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        chunks = [dict(chunk) for chunk in self._chunks.values()]
        if day is not None:
            chunks = [chunk for chunk in chunks if chunk.get("date") == day]
        if kind is not None:
            chunks = [chunk for chunk in chunks if chunk.get("kind") == kind]
        chunks.sort(key=lambda chunk: chunk.get("created_at") or 0)
        if limit:
            chunks = chunks[-int(limit):]
        return chunks

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def save(self) -> None:
        """后端是真相，无需刷盘（local chunk 每次 add_chunk 已追加落盘）。"""
        return None

    def rebuild_vectors(self) -> None:
        """向量由 LightRAG 后端持有；本适配器通过 edit 的 delete+create 保证单条重嵌入，
        故无需（也无法）全量重建 —— 保留同接口的空实现（不崩溃、不产生额外协议调用）。"""
        log("info", "lightrag backend owns vectors; rebuild_vectors is a no-op")

    def stats(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "backend": self.backend,
            "base_url": self.base_url,
            "nodes": len(self._nodes),
            "edges": len(self._relations),
            "chunks": len(self._chunks),
            "data_home": self.data_home,
        }

    def close(self) -> None:
        self.save()


# --------------------------------------------------------------------------- #
# 响应解析辅助
# --------------------------------------------------------------------------- #
def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _props_rel(rel: Any) -> dict[str, Any]:
    if not isinstance(rel, dict):
        return {}
    props = rel.get("properties")
    return props if isinstance(props, dict) else {}


def _extract_names(data: Any) -> list[str]:
    values: list[Any] = []
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        for key in ("labels", "names", "entities", "data"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
    names: list[str] = []
    for item in values:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("entity_name") or item.get("label")
        else:
            continue
        if name and str(name) not in names:
            names.append(str(name))
    return names


def _extract_edges(data: Any) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return edges
    raw = data.get("edges") or data.get("relations") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return edges
    for edge in raw:
        if not isinstance(edge, dict):
            continue
        source = edge.get("source") or edge.get("source_id") or edge.get("from")
        target = edge.get("target") or edge.get("target_id") or edge.get("to")
        if source and target:
            edges.append((str(source), str(target)))
    return edges
