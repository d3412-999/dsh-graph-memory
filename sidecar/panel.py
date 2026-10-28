"""本地只读面板 HTTP 服务（127.0.0.1:9629，仅回环）。

- 静态资源：panel/ 目录（原生 JS，无构建链）
- JSON API：/api/panel_state /api/graph /api/search /api/journal /api/injection /api/action
- 所有对 MemoryService 的访问都调度回主事件循环，避免跨线程读写图谱。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from base import log

PANEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "panel")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "dsh-graph-memory-panel/0.1.0"
    panel: "PanelServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log("debug", "panel http: " + fmt % args)

    # ------------------------------ 工具 ------------------------------ #
    def _allowed(self) -> bool:
        address = self.client_address[0] if self.client_address else ""
        return address in LOOPBACK or address.startswith("127.")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _call(self, coro: Any, timeout: float = 5.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.panel.loop)
        return future.result(timeout)

    # ------------------------------ 路由 ------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed():
            self._json({"error": "loopback only"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/") :])
            elif path == "/healthz":
                self._json({"ok": True})
            elif path == "/api/panel_state":
                self._json(self._call(self.panel.service.panel_state({})))
            elif path == "/api/stats":
                self._json(self._call(self.panel.service.stats({})))
            elif path == "/api/graph":
                self._json(self._call(self.panel._graph_state(query.get("scope", ["workspace"])[0])))
            elif path == "/api/search":
                q = (query.get("q") or [""])[0]
                scope = (query.get("scope") or ["workspace"])[0]
                self._json(self._call(self.panel._search(q, scope)))
            elif path == "/api/journal":
                scope = (query.get("scope") or ["workspace"])[0]
                target = (query.get("target_id") or [None])[0]
                self._json(self._call(self.panel._journal(scope, target)))
            elif path == "/api/injection":
                self._json(self._call(self.panel._injection()))
            elif path == "/api/budget":
                self._json(self._call(self.panel.service.budget_state({})))
            elif path == "/api/prune":
                scope = (query.get("scope") or ["workspace"])[0]
                self._json(self._call(self.panel._prune(scope)))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed():
            self._json({"error": "loopback only"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        try:
            if parsed.path == "/api/action":
                self._json(self._call(self.panel._action(payload)))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def _static(self, relative: str) -> None:
        safe = os.path.normpath(relative).lstrip("\\/")
        target = os.path.join(PANEL_DIR, safe)
        if not os.path.abspath(target).startswith(os.path.abspath(PANEL_DIR)):
            self._json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(target):
            self._json({"error": "not found"}, 404)
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, content_type)


class PanelServer:
    def __init__(self, service: Any, *, host: str = "127.0.0.1", port: int = 9629) -> None:
        self.service = service
        self.loop = asyncio.get_running_loop()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port = port
        for attempt in range(20):
            try:
                handler = type("PanelHandler", (_Handler,), {"panel": self})
                httpd = ThreadingHTTPServer((host, port + attempt), handler)
                self.port = port + attempt
                self.httpd = httpd
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError(f"panel: no free port starting at {port}")
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="graph-memory-panel", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ----------------------- 主循环内执行的只读/控制逻辑 ----------------------- #
    async def _graph_state(self, scope: str) -> dict[str, Any]:
        scope = scope if scope in self.service.scopes else "workspace"
        graph = self.service.graphs[scope]
        nodes = graph.list_entities(scope)
        total = len(nodes)
        # 图谱视图只画得动上百个节点：按权重/新鲜度截断，否则大图谱会把浏览器拖死
        # （5000 节点 ≈ 2.7MB JSON + 数千 DOM/SVG 元素）。总量仍通过 total/counts 返回。
        limit = int(self.service.config.get("panel.graphMaxNodes") or 140)
        if limit > 0 and total > limit:
            nodes = sorted(
                nodes,
                key=lambda node: (
                    float((node.get("properties") or {}).get("w") or 0.0),
                    float((node.get("properties") or {}).get("updated_at") or 0.0),
                ),
                reverse=True,
            )[:limit]
        keep = {str(node.get("id")) for node in nodes}
        relations = [
            dict(rel)
            for rel in graph.relations.values()
            if str(rel.get("source")) in keep and str(rel.get("target")) in keep
        ]
        return {
            "scope": scope,
            "nodes": nodes,
            "relations": relations,
            "counts": graph.stats(),
            "total": total,
            "shown": len(nodes),
            "truncated": total > len(nodes),
        }

    async def _search(self, query: str, scope: str) -> dict[str, Any]:
        if not query:
            return {"items": []}
        result = await self.service.retrieve({"query": query, "scope": [scope], "explicit": True})
        return {"items": result.get("items") or [], "top1_sim": result.get("top1_sim")}

    async def _journal(self, scope: str, target_id: Optional[str]) -> dict[str, Any]:
        return await self.service.journal_list({"scope": scope, "target_id": target_id})

    async def _injection(self) -> dict[str, Any]:
        return {"injection_last": self.service.last_injection}

    async def _prune(self, scope: str) -> dict[str, Any]:
        journal = self.service.journals.get(scope)
        return {"history": journal.prune_history(scope) if journal else []}

    async def _action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        scope = str(payload.get("scope") or "workspace")
        entity_id = payload.get("id")
        if action == "pin":
            node = self.service.graphs[scope].get_entity(entity_id) if entity_id else None
            if node is None:
                return {"error": "not found"}
            self.service.graphs[scope].update_properties(
                entity_id,
                {"pinned": True, "w": max(float(node["properties"].get("w") or 1.0), 2.0), "updated_at": self.service.now()},
            )
            self.service.graphs[scope].save()
            return {"ok": True, "id": entity_id}
        if action == "unpin":
            self.service.graphs[scope].update_properties(entity_id, {"pinned": False})
            self.service.graphs[scope].save()
            return {"ok": True, "id": entity_id}
        if action == "set_weight":
            return await self.service.set_weight({"id": entity_id, "w": payload.get("w"), "scope": scope})
        if action == "forget":
            return await self.service.forget({"id": entity_id, "scope": scope})
        if action == "update":
            return await self.service.update(
                {"query": payload.get("query") or "", "new_value": payload.get("new_value") or "", "scope": scope}
            )
        if action == "rollback":
            return await self.service.journal_rollback({"change_id": payload.get("change_id")})
        if action == "set_budget":
            # 预算调整：面板滑杆/输入框直接改注入上限（[3000, 2_000_000]，落盘 budget.json）
            return await self.service.set_budget(
                {"max_tokens": payload.get("max_tokens", payload.get("value"))}
            )
        if action == "replay_dead_letter":
            return await self.service.dead_letter_replay({"id": payload.get("id")})
        return {"error": f"unknown action: {action}"}


def start_panel(service: Any, *, host: str = "127.0.0.1", port: int = 9629) -> PanelServer:
    return PanelServer(service, host=host, port=port)
