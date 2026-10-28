"""graph_lightrag.py 单测：LightRAG HTTP 协议映射、R2/R3/R6 约束、不可达异常。

没有真实 LightRAG 服务：用标准库 ``http.server`` 起 fake 服务器，断言
- 端点/载荷映射（/graph/entity/create|edit|delete、relation/*、entities/merge、/insert、/query）；
- properties 进 node_data、description 不写元数据（R3）；
- edit 用 edit → delete → create 强制重嵌入（R6）；
- 远端图谱唯一真相、本地缓存仅派生（R2 语义）；
- 连接失败/HTTP 错误/非法 JSON 一律抛 GraphBackendUnavailable（不崩溃进程）。
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import base  # noqa: E402,F401  (契约常量/异常来自 base)
import graph_lightrag  # noqa: E402
from base import GraphBackendUnavailable, GraphMemoryError  # noqa: E402
from graph_lightrag import LightRAGStore  # noqa: E402

T0 = 1_700_000_000.0

_TMP_ROOT = os.path.join(ROOT, ".tmp", "tests-py")


def make_temp_dir(tag: str) -> str:
    """仓库 .tmp/ 下的唯一目录（sandbox 下 tempfile.mkdtemp 的子项不可写）。"""
    os.makedirs(_TMP_ROOT, exist_ok=True)
    path = os.path.join(_TMP_ROOT, f"{tag}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(path, exist_ok=False)
    return path


# --------------------------------------------------------------------------- #
# Fake LightRAG HTTP 服务器
# --------------------------------------------------------------------------- #
STATE: dict = {"requests": [], "responder": None}


def default_responder(method: str, path: str, body: object) -> tuple[int, object]:
    if method == "GET" and path.startswith("/graph/entities/"):
        return 404, {"detail": "not found"}
    if method == "GET" and path.startswith("/graph/label/list"):
        return 200, {"labels": []}
    if method == "GET" and path.startswith("/graphs"):
        return 200, {"nodes": [], "edges": []}
    if path == "/query":
        return 200, {"entities": [], "chunks": []}
    return 200, {"status": "ok"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: object = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        STATE["requests"].append(
            {"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers)}
        )
        responder = STATE.get("responder") or default_responder
        status, payload = responder(self.command, self.path, body)
        if isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *_args: object) -> None:  # 静默测试日志
        pass


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class LightRAGTestCase(unittest.TestCase):
    server: http.server.ThreadingHTTPServer | None = None
    port: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = int(cls.server.server_address[1])
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()

    def setUp(self) -> None:
        STATE["requests"] = []
        STATE["responder"] = default_responder
        self.tmpdir = make_temp_dir("gm-lightrag")
        self.store = LightRAGStore(
            self.tmpdir,
            "user",
            base_url=f"http://127.0.0.1:{self.port}",
            api_key="test-secret",
            timeout=2.0,
            clock=lambda: T0,
        )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------------------------------------------------------------- #
    # 辅助
    # ---------------------------------------------------------------- #
    def respond(self, responder) -> None:
        STATE["responder"] = responder

    def requests(self) -> list[dict]:
        return list(STATE["requests"])

    def paths(self) -> list[str]:
        return [request["path"] for request in STATE["requests"]]

    def records(self, path: str) -> list[dict]:
        return [request for request in STATE["requests"] if request["path"] == path]

    def last_body(self, path: str) -> dict:
        records = self.records(path)
        self.assertTrue(records, f"no request to {path}; got {self.paths()}")
        return records[-1]["body"]


class EntityProtocolTests(LightRAGTestCase):
    def test_upsert_entity_maps_to_entity_create_with_node_data_properties(self) -> None:
        entity_id = self.store.upsert_entity(
            "包管理器",
            "Preference",
            "用户偏好使用 pnpm 管理依赖",
            "user",
            properties={"w": 2.0, "pinned": True, "valid_from": "2026-10-28"},
        )
        self.assertTrue(entity_id.startswith("ent_"))
        self.assertEqual(len(self.records("/graph/entity/create")), 1)
        body = self.last_body("/graph/entity/create")
        self.assertEqual(body["entity_name"], "包管理器")
        self.assertEqual(body["entity_type"], "Preference")
        self.assertEqual(body["description"], "用户偏好使用 pnpm 管理依赖")
        # R3：description 不含权重/时间/pin 元数据
        self.assertNotIn("w=", body["description"])
        self.assertNotIn("pinned", body["description"])
        node_data = body["node_data"]
        self.assertEqual(node_data["properties"]["w"], 2.0)
        self.assertEqual(node_data["properties"]["pinned"], True)
        self.assertEqual(node_data["properties"]["valid_from"], "2026-10-28")
        self.assertEqual(node_data["scope"], "user")
        # API key 以 Bearer 头传递
        headers = self.records("/graph/entity/create")[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer test-secret")
        # 本地只读缓存可回读
        cached = self.store.get_entity(entity_id)
        assert cached is not None
        self.assertEqual(cached["description"], "用户偏好使用 pnpm 管理依赖")

    def test_upsert_same_name_merges_via_edit(self) -> None:
        first = self.store.upsert_entity("包管理器", "Preference", "使用 pnpm", "user")
        second = self.store.upsert_entity("包管理器", "Preference", "也用于 monorepo", "user")
        self.assertEqual(first, second)
        self.assertIn("/graph/entity/edit", self.paths())
        # GraphStore 语义：描述合并，不新建重复实体
        cached = self.store.get_entity(first)
        assert cached is not None
        self.assertIn("使用 pnpm", cached["description"])
        self.assertIn("也用于 monorepo", cached["description"])

    def test_edit_entity_forces_delete_create_for_reembed(self) -> None:
        entity_id = self.store.upsert_entity("包管理器", "Preference", "使用 pnpm", "user")
        STATE["requests"] = []  # 只看 edit 阶段
        updated = self.store.edit_entity(entity_id, description="使用 npm", properties={"w": 3.0})
        # R6：edit → delete → create（后端无独立向量更新端点，强制重嵌入）
        self.assertEqual(
            self.paths(),
            ["/graph/entity/edit", "/graph/entity/delete", "/graph/entity/create"],
        )
        edit_body = self.last_body("/graph/entity/edit")
        self.assertEqual(edit_body["entity_name"], "包管理器")
        self.assertEqual(edit_body["updated_data"]["description"], "使用 npm")
        self.assertEqual(edit_body["updated_data"]["node_data"]["properties"]["w"], 3.0)
        self.assertEqual(self.last_body("/graph/entity/delete")["entity_name"], "包管理器")
        self.assertEqual(self.last_body("/graph/entity/create")["description"], "使用 npm")
        self.assertEqual(updated["description"], "使用 npm")
        cached = self.store.get_entity(entity_id)
        assert cached is not None
        self.assertEqual(cached["description"], "使用 npm")
        self.assertGreaterEqual(int(cached["properties"]["embed_version"]), 2)

    def test_edit_unknown_entity_raises_business_error(self) -> None:
        with self.assertRaises(GraphMemoryError) as ctx:
            self.store.edit_entity("ent_0000000000000000", description="新描述")
        self.assertEqual(ctx.exception.code, "entity_not_found")

    def test_delete_entity(self) -> None:
        entity_id = self.store.upsert_entity("待删实体", "Concept", "临时概念", "user")
        STATE["requests"] = []
        result = self.store.delete_entity(entity_id)
        self.assertEqual(result["id"], entity_id)
        self.assertEqual(self.last_body("/graph/entity/delete")["entity_name"], "待删实体")
        self.assertIsNone(self.store.get_entity(entity_id))
        with self.assertRaises(GraphMemoryError):
            self.store.delete_entity("ent_0000000000000000")

    def test_update_properties_uses_edit_without_reembed(self) -> None:
        entity_id = self.store.upsert_entity("激活实体", "Concept", "概念", "user")
        STATE["requests"] = []
        updated = self.store.update_properties(entity_id, {"pinned": True, "w": 7.0})
        self.assertEqual(self.paths(), ["/graph/entity/edit"])
        body = self.last_body("/graph/entity/edit")
        self.assertEqual(body["updated_data"]["node_data"]["properties"]["pinned"], True)
        self.assertEqual(body["updated_data"]["node_data"]["properties"]["w"], 7.0)
        self.assertEqual(updated["properties"]["pinned"], True)


class RelationProtocolTests(LightRAGTestCase):
    def _two_entities(self) -> tuple[str, str]:
        a = self.store.upsert_entity("实体A", "Concept", "A 的描述", "user")
        b = self.store.upsert_entity("实体B", "Concept", "B 的描述", "user")
        return a, b

    def test_relation_create_edit_delete(self) -> None:
        a, b = self._two_entities()
        rel_id = self.store.upsert_relation(
            a, b, "A 依赖 B", "user", keywords="depends", properties={"weight": 0.8}
        )
        body = self.last_body("/graph/relation/create")
        # 关系端点使用实体 name（LightRAG 图谱以 name 为节点键）
        self.assertEqual(body["source_id"], "实体A")
        self.assertEqual(body["target_id"], "实体B")
        self.assertEqual(body["description"], "A 依赖 B")
        self.assertEqual(body["keywords"], "depends")
        self.assertEqual(body["edge_data"]["properties"]["weight"], 0.8)
        self.assertEqual(self.store.relation_count(), 1)

        edited = self.store.edit_relation(rel_id, description="A 强依赖 B", properties={"weight": 1.0})
        body = self.last_body("/graph/relation/edit")
        self.assertEqual(body["source_id"], "实体A")
        self.assertEqual(body["target_id"], "实体B")
        self.assertEqual(body["updated_data"]["description"], "A 强依赖 B")
        self.assertEqual(edited["properties"]["weight"], 1.0)

        self.store.delete_relation(rel_id)
        body = self.last_body("/graph/relation/delete")
        self.assertEqual(body["source_id"], "实体A")
        self.assertEqual(body["target_id"], "实体B")
        self.assertEqual(self.store.relation_count(), 0)

    def test_upsert_relation_merges_duplicate_pair(self) -> None:
        a, b = self._two_entities()
        first = self.store.upsert_relation(a, b, "关系一", "user")
        second = self.store.upsert_relation(a, b, "关系二", "user")
        self.assertEqual(first, second)
        self.assertEqual(self.store.relation_count(), 1)

    def test_relation_to_unknown_entity_raises(self) -> None:
        a, _b = self._two_entities()
        with self.assertRaises(GraphMemoryError):
            self.store.upsert_relation(a, "ent_0000000000000000", "悬空关系", "user")

    def test_neighbors_and_expand_related(self) -> None:
        a, b = self._two_entities()
        self.store.upsert_relation(a, b, "A→B", "user")
        self.assertEqual(self.store.neighbors(a), [b])
        self.assertEqual(self.store.neighbors(b), [a])
        expanded = self.store.expand_related([a], top_k=5)
        self.assertEqual(expanded, [(b, graph_lightrag.RELATION_DISCOUNT)])

    def test_merge_entities(self) -> None:
        a = self.store.upsert_entity("实体A", "Concept", "A 的描述", "user", properties={"w": 1.0})
        b = self.store.upsert_entity("实体B", "Concept", "B 的描述", "user", properties={"w": 4.0})
        STATE["requests"] = []
        merged = self.store.merge_entities([a, b], keep_id=a)
        body = self.last_body("/graph/entities/merge")
        self.assertEqual(body["entities_to_change"], ["实体B"])
        self.assertEqual(body["entity_to_keep"], "实体A")
        self.assertEqual(float(merged["properties"]["w"]), 4.0)
        self.assertIsNone(self.store.get_entity(b))


class QueryProtocolTests(LightRAGTestCase):
    def test_search_entities_query_mapping(self) -> None:
        entity_id = self.store.upsert_entity("包管理器", "Preference", "使用 pnpm 管理依赖", "user")

        def responder(method, path, body):
            if path == "/query":
                return 200, {"entities": [{"entity_name": "包管理器", "score": 0.93}]}
            return default_responder(method, path, body)

        self.respond(responder)
        hits = self.store.search_entities("pnpm", top_k=5)
        self.assertEqual(hits, [(entity_id, 0.93)])
        body = self.last_body("/query")
        self.assertEqual(body["query"], "pnpm")
        self.assertEqual(body["mode"], "mix")
        self.assertEqual(body["top_k"], 20)  # 契约：top_k=20
        self.assertTrue(body["only_need_context"])

    def test_search_entities_accepts_entity_id_results(self) -> None:
        entity_id = self.store.upsert_entity("包管理器", "Preference", "使用 pnpm", "user")

        def responder(method, path, body):
            if path == "/query":
                return 200, {"items": [{"id": entity_id, "similarity": 0.77}]}
            return default_responder(method, path, body)

        self.respond(responder)
        self.assertEqual(self.store.search_entities("q"), [(entity_id, 0.77)])

    def test_search_entities_context_text_fallback(self) -> None:
        entity_id = self.store.upsert_entity("包管理器", "Preference", "使用 pnpm", "user")

        def responder(method, path, body):
            if path == "/query":
                return 200, {"response": "相关实体：包管理器 与依赖管理"}
            return default_responder(method, path, body)

        self.respond(responder)
        self.assertEqual(self.store.search_entities("pnpm"), [(entity_id, 0.5)])

    def test_search_entities_hides_archived_unless_include_archived(self) -> None:
        entity_id = self.store.upsert_entity("归档实体", "Concept", "已归档", "user")
        self.store.update_properties(entity_id, {"status": "archived"})

        def responder(method, path, body):
            if path == "/query":
                return 200, {"entities": [{"entity_name": "归档实体", "score": 0.9}]}
            return default_responder(method, path, body)

        self.respond(responder)
        self.assertEqual(self.store.search_entities("q"), [])
        self.assertEqual(self.store.search_entities("q", include_archived=True), [(entity_id, 0.9)])

    def test_add_chunk_and_search_chunks(self) -> None:
        chunk_id = self.store.add_chunk("用户偏好使用 pnpm 管理依赖", "user", "turn_1", "2026-10-28")
        self.assertTrue(chunk_id.startswith("chk_"))
        self.assertEqual(self.last_body("/insert")["text"], "用户偏好使用 pnpm 管理依赖")
        self.assertEqual(self.last_body("/insert")["chunk_id"], chunk_id)

        def responder(method, path, body):
            if path == "/query":
                return 200, {"chunks": [{"id": chunk_id, "score": 0.7}]}
            return default_responder(method, path, body)

        self.respond(responder)
        self.assertEqual(self.store.search_chunks("pnpm", top_k=3), [(chunk_id, 0.7)])
        self.assertEqual(self.store.search_chunks("pnpm", day="2026-10-28"), [(chunk_id, 0.7)])
        self.assertEqual(self.store.search_chunks("pnpm", day="2026-01-01"), [])
        stored = self.store.get_chunk(chunk_id)
        assert stored is not None
        self.assertEqual(stored["turn_id"], "turn_1")

    def test_chunks_persist_locally_across_instances(self) -> None:
        chunk_id = self.store.add_chunk("原文一", "user", "turn_1", "2026-10-28")
        self.store.close()
        reopened = LightRAGStore(
            self.tmpdir,
            "user",
            base_url=f"http://127.0.0.1:{self.port}",
            api_key="test-secret",
            timeout=2.0,
            clock=lambda: T0,
        )
        try:
            chunk = reopened.get_chunk(chunk_id)
            assert chunk is not None
            self.assertEqual(chunk["text"], "原文一")
            self.assertEqual(len(reopened.list_chunks(day="2026-10-28")), 1)
        finally:
            reopened.close()

    def test_count_and_list_entities(self) -> None:
        self.store.upsert_entity("实体A", "Concept", "A", "user")

        def responder(method, path, body):
            if path == "/graph/label/list":
                return 200, {"labels": ["实体A", "远端实体B"]}
            return default_responder(method, path, body)

        self.respond(responder)
        self.assertEqual(self.store.count(), 2)
        nodes = self.store.list_entities()
        self.assertEqual([node["name"] for node in nodes], ["实体A", "远端实体B"])
        self.assertEqual(len(self.store.list_entities(limit=1)), 1)


class FailureModeTests(LightRAGTestCase):
    def test_constructor_requires_base_url(self) -> None:
        with self.assertRaises(GraphBackendUnavailable):
            LightRAGStore(self.tmpdir, "user", base_url="")

    def test_http_error_raises_backend_unavailable(self) -> None:
        def responder(method, path, body):
            return 500, {"detail": "internal boom"}

        self.respond(responder)
        with self.assertRaises(GraphBackendUnavailable):
            self.store.upsert_entity("实体", "Concept", "描述", "user")
        with self.assertRaises(GraphBackendUnavailable):
            self.store.search_entities("q")

    def test_invalid_json_raises_backend_unavailable(self) -> None:
        def responder(method, path, body):
            return 200, "这不是 JSON {{{"

        self.respond(responder)
        with self.assertRaises(GraphBackendUnavailable):
            self.store.upsert_entity("实体", "Concept", "描述", "user")

    def test_unreachable_server_raises_backend_unavailable(self) -> None:
        port = _free_port()
        store = LightRAGStore(
            self.tmpdir,
            "user",
            base_url=f"http://127.0.0.1:{port}",
            timeout=0.5,
            clock=lambda: T0,
        )
        with self.assertRaises(GraphBackendUnavailable):
            store.upsert_entity("实体", "Concept", "描述", "user")
        with self.assertRaises(GraphBackendUnavailable):
            store.search_entities("q")

    def test_no_api_key_omits_authorization_header(self) -> None:
        store = LightRAGStore(
            self.tmpdir,
            "user",
            base_url=f"http://127.0.0.1:{self.port}",
            timeout=2.0,
            clock=lambda: T0,
        )
        store.upsert_entity("实体", "Concept", "描述", "user")
        headers = self.records("/graph/entity/create")[-1]["headers"]
        self.assertNotIn("Authorization", headers)

    def test_save_and_rebuild_vectors_are_noops(self) -> None:
        STATE["requests"] = []
        self.store.save()
        self.store.rebuild_vectors()
        self.assertEqual(STATE["requests"], [])

    def test_lightrag_store_exposes_graphstore_attributes(self) -> None:
        # service.py 依赖的常见属性/方法存在（duck-typing，不要求 isinstance）
        for name in (
            "chunks",
            "save",
            "add_chunk",
            "get_chunk",
            "search_chunks",
            "list_chunks",
            "count",
            "relation_count",
            "update_properties",
            "get_entity",
            "find_by_name",
            "upsert_entity",
            "search_entities",
            "list_entities",
            "neighbors",
            "expand_related",
            "upsert_relation",
            "edit_relation",
            "delete_relation",
            "merge_entities",
            "delete_entity",
            "edit_entity",
            "rebuild_vectors",
        ):
            self.assertTrue(hasattr(self.store, name), f"missing {name}")
        self.assertIsInstance(self.store.chunks, dict)
        stats = self.store.stats()
        self.assertEqual(stats["backend"], "lightrag")


if __name__ == "__main__":
    unittest.main()
