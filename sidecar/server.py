"""dsh-graph-memory sidecar 入口：stdio JSON-RPC 服务端 + 面板 + 空闲退出。

用法：
  python sidecar/server.py --user-home <dir> --workspace-home <dir> --workspace-root <dir> \
      [--config <path-or-inline-json>] [--panel-port 9629]

协议见 docs/CONTRACT.md §3（行分隔 JSON-RPC 2.0；支持反向 host/llm 调用）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from base import GraphMemoryError, RpcError, estimate_tokens, log, now_ts, read_json, write_json_atomic
from config import build_config
from service import MemoryService

RUNTIME_FILE = "runtime.json"


class StdioPeer:
    """行分隔 JSON-RPC 双向 peer：宿主请求 → service，service 反向请求 → 宿主。"""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._inbox: Optional[asyncio.Queue] = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self.last_activity = time.time()
        self.eof = False

    # ----------------------------- IO ------------------------------ #
    def _read_loop(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            assert self.loop is not None and self._inbox is not None
            self.loop.call_soon_threadsafe(self._inbox.put_nowait, message)
        self.eof = True
        assert self.loop is not None and self._inbox is not None
        self.loop.call_soon_threadsafe(self._inbox.put_nowait, {"__eof__": True})

    def write(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                sys.stdout.write(data + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                self.eof = True

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_notification(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self.notify(method, params)

    # --------------------------- reverse RPC ------------------------ #
    async def send_request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: float = 60.0) -> Any:
        assert self.loop is not None
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = self.loop.create_future()
        self._pending[request_id] = future
        try:
            self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    # ----------------------------- run ----------------------------- #
    async def run(self, dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]]) -> None:
        self.loop = asyncio.get_running_loop()
        self._inbox = asyncio.Queue()
        self._reader.start()
        tasks: set[asyncio.Task] = set()
        while True:
            message = await self._inbox.get()
            if message.get("__eof__"):
                break
            self.last_activity = time.time()
            if "method" not in message:
                self._resolve_response(message)
                continue
            if message.get("id") is None:
                continue  # 宿主通知：本 sidecar 不需要
            task = asyncio.ensure_future(self._handle_request(message, dispatch))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                RpcError(str(error.get("message") or "host rpc error"), int(error.get("code") or -32603), error.get("data"))
            )
        else:
            future.set_result(message.get("result"))

    async def _handle_request(self, message: dict[str, Any], dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        request_id = message.get("id")
        try:
            result = await dispatch(method, params)
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.rpc_code, "message": str(exc), "data": exc.data}}
        except GraphMemoryError as exc:
            rpc_code = -32602 if exc.code in ("invalid_params", "invalid_request") else -32000
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": rpc_code, "message": str(exc), "data": {"kind": exc.code}}}
        except Exception as exc:  # noqa: BLE001
            log("error", "rpc handler failed", method=method, error=repr(exc))
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"internal error: {exc}"}}
        self.write(payload)


class TcpPeer:
    """Loopback TCP transport（沙箱内 Node 无法用 piped stdio 启动子进程时的自动降级通道）。

    协议与 stdio 完全相同（行分隔 JSON-RPC 2.0），仅传输层不同；首个消息必须是
    {"method":"auth","params":{"token":...}}。只绑定 127.0.0.1 随机端口，端口与 token
    写入 `<workspace_home>/sidecar.runtime.json` 供宿主发现。
    """

    def __init__(self, token: str) -> None:
        self.token = token or ""
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._writer: Optional[Any] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self.last_activity = time.time()
        self.host = "127.0.0.1"
        self.port: Optional[int] = None
        self.runtime_path: Optional[str] = None
        self._event: Optional[asyncio.Event] = None
        self._dispatch: Optional[Callable[[str, dict[str, Any]], Awaitable[Any]]] = None
        self._tasks: set[asyncio.Task] = set()
        self._server: Optional[Any] = None

    # ----------------------------- IO ------------------------------ #
    def write(self, payload: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        except (ConnectionError, OSError, RuntimeError):
            self._writer = None

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_notification(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self.notify(method, params)

    async def send_request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: float = 60.0) -> Any:
        assert self.loop is not None
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = self.loop.create_future()
        self._pending[request_id] = future
        try:
            self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    def _resolve_response(self, message: dict[str, Any]) -> None:
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                RpcError(str(error.get("message") or "host rpc error"), int(error.get("code") or -32603), error.get("data"))
            )
        else:
            future.set_result(message.get("result"))

    async def _handle_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        request_id = message.get("id")
        try:
            assert self._dispatch is not None
            result = await self._dispatch(method, params)
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.rpc_code, "message": str(exc), "data": exc.data}}
        except GraphMemoryError as exc:
            rpc_code = -32602 if exc.code in ("invalid_params", "invalid_request") else -32000
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": rpc_code, "message": str(exc), "data": {"kind": exc.code}}}
        except Exception as exc:  # noqa: BLE001
            log("error", "tcp rpc handler failed", method=method, error=repr(exc))
            payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"internal error: {exc}"}}
        self.write(payload)

    async def _handle_client(self, reader: Any, writer: Any) -> None:
        try:
            first = await asyncio.wait_for(reader.readline(), timeout=10)
            first_message = json.loads(first.decode("utf-8").strip() or "{}")
            if (
                str(first_message.get("method") or "") != "auth"
                or str((first_message.get("params") or {}).get("token") or "") != self.token
            ):
                writer.write(json.dumps({"jsonrpc": "2.0", "id": first_message.get("id"), "error": {"code": -32001, "message": "auth failed"}}).encode() + b"\n")
                await writer.drain()
                return
            writer.write(json.dumps({"jsonrpc": "2.0", "id": first_message.get("id"), "result": {"ok": True}}).encode() + b"\n")
            await writer.drain()
            self._writer = writer
            self.notify("ready", {"pid": os.getpid(), "version": "0.1.0", "transport": "tcp"})
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.last_activity = time.time()
                try:
                    message = json.loads(line.decode("utf-8").strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if "method" in message:
                    if message.get("id") is None:
                        continue
                    task = asyncio.ensure_future(self._handle_request(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    self._resolve_response(message)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            self._writer = None
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            # 关键：客户端断开（例如 start.sh 的 wait-ready 探针、ctl ping）不能关闭 sidecar。
            # TcpPeer 允许顺序/并发客户端重连，只在 shutdown RPC / 信号 / 空闲超时时退出。
            self._fail_pending("tcp client disconnected")

    def _fail_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(message))
        self._pending.clear()

    async def run(self, dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]]) -> None:
        self.loop = asyncio.get_running_loop()
        self._dispatch = dispatch
        self._event = asyncio.Event()
        server = await asyncio.start_server(self._handle_client, self.host, 0)
        self._server = server
        sockets = server.sockets or []
        self.port = int(sockets[0].getsockname()[1]) if sockets else None
        if self.runtime_path and self.port:
            write_json_atomic(
                self.runtime_path,
                {
                    "transport": "tcp",
                    "host": self.host,
                    "port": self.port,
                    "token": self.token,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                },
            )
            log("info", "tcp transport ready", port=self.port)
        try:
            # 该 event 永远等待：退出由 SidecarApp 的 shutdown 标志 / 空闲监视器取消本任务完成，
            # 客户端连接/断开不影响 sidecar 生命周期（支持顺序探针与控制命令重连）。
            await self._event.wait()
        finally:
            if self._server is not None:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
            for task in list(self._tasks):
                task.cancel()


class SidecarApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = build_config(
            user_home=args.user_home,
            workspace_home=args.workspace_home,
            workspace_root=os.path.abspath(args.workspace_root or os.getcwd()),
            raw_override=args.config,
            panel_port=args.panel_port,
        )
        self.transport = str(getattr(args, "transport", "stdio") or "stdio").lower()
        self.token = str(getattr(args, "token", "") or "")
        if self.transport == "tcp":
            self.peer: Any = TcpPeer(self.token)
        else:
            self.peer = StdioPeer()
        self.service = MemoryService(self.config, peer=self.peer)
        if self.transport == "tcp" and hasattr(self.peer, "runtime_path"):
            self.peer.runtime_path = os.path.join(self.service.paths["workspace"], "sidecar.runtime.json")
        self.panel = None
        self._method_map: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "ping": self._ping,
            "retrieve": self.service.retrieve,
            "ingest": self.service.ingest,
            "activate": self.service.activate,
            "update": self.service.update,
            "compress": self.service.compress,
            "prune": self.service.prune,
            "day_pack": self.service.day_pack,
            "journal_list": self.service.journal_list,
            "journal_rollback": self.service.journal_rollback,
            "stats": self.service.stats,
            "panel_state": self.service.panel_state,
            "round_tick": self.service.round_tick,
            "maintenance": self.service.maintenance,
            "forget": self.service.forget,
            "set_weight": self.service.set_weight,
            "conflicts_sweep": self.service.conflicts_sweep,
            "injection_record": self.service.injection_record,
            "budget": self.service.budget_state,
            "set_budget": self.service.set_budget,
            "tokens": self.service.tokens,
            "dead_letter_list": self.service.dead_letter_list,
            "dead_letter_replay": self.service.dead_letter_replay,
            "debug_set_now": self.service.debug_set_now,
            "debug_seed_entities": self.service.debug_seed_entities,
            "debug_entity_set": self.service.debug_entity_set,
            "debug_state": self.service.debug_state,
            "calibrate": self.service.calibrate,
            "shutdown": self._shutdown_rpc,
        }

    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "pong": True,
            "version": "0.1.0",
            "pid": os.getpid(),
            "started_at": self.service.started_at,
            "llm_mode": self.service.llm.mode,
            "backend": self.config.get("graph.backend") or "embedded",
        }

    async def _shutdown_rpc(self, params: dict[str, Any]) -> dict[str, Any]:
        asyncio.get_running_loop().call_soon(setattr, self, "_shutdown_requested", True)
        return {"ok": True}

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handler = self._method_map.get(method)
        if handler is None:
            raise RpcError(f"method not found: {method}", rpc_code=-32601)
        return await handler(params)

    def _start_panel(self) -> None:
        if not self.config.get("panel.enabled", True):
            return
        try:
            from panel import start_panel

            host = self.config.get("panel.host") or "127.0.0.1"
            port = int(self.config.get("panel.port") or 9629)
            self.panel = start_panel(self.service, host=host, port=port)
            actual = getattr(self.panel, "port", port)
            log("info", "panel ready", url=f"http://{host}:{actual}")
        except Exception as exc:  # noqa: BLE001
            log("warning", "panel unavailable", error=str(exc))

    def _write_runtime(self) -> None:
        if self.transport == "tcp":
            return  # TcpPeer 在 server 端口绑定后自行写 runtime
        try:
            path = os.path.join(self.service.paths["workspace"], RUNTIME_FILE)
            write_json_atomic(
                path,
                {
                    "pid": os.getpid(),
                    "started_at": self.service.started_at,
                    "pid_started": now_ts(),
                    "panel_port": getattr(self.panel, "port", None),
                    "panel_host": self.config.get("panel.host"),
                    "llm_mode": self.service.llm.mode,
                    "transport": "stdio",
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _remove_runtime(self) -> None:
        for name in (RUNTIME_FILE, "sidecar.runtime.json"):
            try:
                os.unlink(os.path.join(self.service.paths["workspace"], name))
            except OSError:
                pass

    async def run(self) -> int:
        self._start_panel()
        self._write_runtime()
        if self.transport != "tcp":
            self.peer.notify("ready", {"pid": os.getpid(), "version": "0.1.0", "transport": "stdio"})

        async def _idle_monitor(stop: asyncio.Event) -> None:
            idle_exit = int(self.config.get("sidecar.idleExitSeconds") or 0)
            if idle_exit <= 0:
                # 0/负数 = 永不空闲退出（`start.sh` 一键启动的常驻面板模式依赖此语义）
                log("info", "idle exit disabled (sidecar.idleExitSeconds <= 0)")
                return
            while not stop.is_set():
                await asyncio.sleep(min(5.0, max(1.0, idle_exit / 10)))
                if stop.is_set():
                    return
                busy = bool(self.service._background) or bool(self.peer._pending)
                if not busy and time.time() - self.peer.last_activity >= idle_exit:
                    log("info", "idle timeout, exiting", idle_seconds=idle_exit)
                    stop.set()
                    return

        stop_event = asyncio.Event()
        monitor = asyncio.ensure_future(_idle_monitor(stop_event))
        run_task = asyncio.ensure_future(self.peer.run(self.dispatch))
        stopper = asyncio.ensure_future(stop_event.wait())
        shutdown_requested = asyncio.ensure_future(self._wait_shutdown_flag())
        try:
            await asyncio.wait(
                {run_task, stopper, shutdown_requested},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            monitor.cancel()
            for task in (run_task, stopper, shutdown_requested):
                if not task.done():
                    task.cancel()
            await self.service.close()
            if self.panel is not None:
                try:
                    self.panel.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._remove_runtime()
        return 0

    async def _wait_shutdown_flag(self) -> None:
        while not getattr(self, "_shutdown_requested", False):
            await asyncio.sleep(0.1)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="dsh-graph-memory sidecar")
    parser.add_argument("--user-home", default=None)
    parser.add_argument("--workspace-home", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--config", default=None, help="JSON file path or inline JSON")
    parser.add_argument("--panel-port", type=int, default=None)
    parser.add_argument("--transport", choices=["stdio", "tcp"], default="stdio")
    parser.add_argument("--token", default="")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    app = SidecarApp(args)

    def _signal_handler(_signum: int, _frame: Any) -> None:
        app._shutdown_requested = True  # type: ignore[attr-defined]

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, OSError):
        pass
    try:
        return asyncio.run(app.run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
