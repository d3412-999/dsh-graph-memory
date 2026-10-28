#!/usr/bin/env python3
"""Sidecar 控制小工具：wait-ready / status / ping / stop / call。

供 `start.sh`、`stop.sh` 与人工调试使用；只依赖 Python 标准库，通过
`<workspaceDataHome>/sidecar.runtime.json` 发现 loopback TCP 端口与一次性 token。

用法（仓库根目录，默认 workspace 数据目录 = ./.dsh/graph-memory）：
  py -3 scripts/sidecar_ctl.py status
  py -3 scripts/sidecar_ctl.py wait-ready --timeout 20
  py -3 scripts/sidecar_ctl.py ping
  py -3 scripts/sidecar_ctl.py call stats
  py -3 scripts/sidecar_ctl.py call retrieve '{"query":"包管理器","scope":["user","workspace"]}'
  py -3 scripts/sidecar_ctl.py stop
  py -3 scripts/sidecar_ctl.py log   # 打印 sidecar.log 尾部
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKSPACE_HOME = os.path.join(ROOT, ".dsh", "graph-memory")
RUNTIME_FILE = "sidecar.runtime.json"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


configure_stdio()


class RpcClient:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.buffer = b""
        self.next_id = 0
        self.token = token
        self._auth()

    def _send(self, payload: dict) -> None:
        self.sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")

    def _readline(self) -> dict:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("sidecar closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def _auth(self) -> None:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": "auth", "params": {"token": self.token}})
        while True:
            message = self._readline()
            if "method" in message and "id" in message:  # 忽略 sidecar 反向请求（start 场景不会出现）
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": {"text": ""}})
                continue
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise PermissionError(f"auth failed: {message['error']}")
                return

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self.sock.settimeout(timeout)
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        while True:
            message = self._readline()
            if "method" in message and "id" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": {"text": ""}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message.get("result")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def runtime_path(workspace_home: str) -> str:
    return os.path.join(workspace_home, RUNTIME_FILE)


def load_runtime(workspace_home: str) -> dict:
    path = runtime_path(workspace_home)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("transport") != "tcp":
        raise RuntimeError(f"runtime file is not a tcp sidecar: {path}")
    return data


def try_connect(workspace_home: str, timeout: float = 2.0) -> RpcClient | None:
    try:
        runtime = load_runtime(workspace_home)
    except (OSError, json.JSONDecodeError, RuntimeError):
        return None
    try:
        client = RpcClient(str(runtime.get("host") or "127.0.0.1"), int(runtime["port"]), str(runtime["token"]))
        client.sock.settimeout(timeout)
        return client
    except (OSError, PermissionError):
        return None


def cmd_wait_ready(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout
    last_error = "runtime file not found"
    while time.time() < deadline:
        client = try_connect(args.workspace_home)
        if client is not None:
            try:
                pong = client.call("ping")
                print(
                    json.dumps(
                        {
                            "ready": True,
                            "pid": pong.get("pid"),
                            "llm_mode": pong.get("llm_mode"),
                            "backend": pong.get("backend"),
                            "runtime": runtime_path(args.workspace_home),
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            except (OSError, RuntimeError) as exc:
                last_error = str(exc)
            finally:
                client.close()
        time.sleep(0.2)
    print(f"sidecar not ready within {args.timeout}s: {last_error}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    try:
        runtime = load_runtime(args.workspace_home)
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(json.dumps({"running": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    client = try_connect(args.workspace_home)
    if client is None:
        print(json.dumps({**runtime, "running": False}, ensure_ascii=False))
        return 1
    try:
        pong = client.call("ping")
        print(json.dumps({**runtime, "running": True, "ping": pong}, ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


def cmd_ping(args: argparse.Namespace) -> int:
    client = try_connect(args.workspace_home)
    if client is None:
        print("sidecar not running", file=sys.stderr)
        return 1
    try:
        print(json.dumps(client.call("ping"), ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


def cmd_call(args: argparse.Namespace) -> int:
    client = try_connect(args.workspace_home)
    if client is None:
        print("sidecar not running", file=sys.stderr)
        return 1
    try:
        params = json.loads(args.params) if args.params else {}
        result = client.call(args.method, params, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        client.close()


def cmd_stop(args: argparse.Namespace) -> int:
    client = try_connect(args.workspace_home)
    if client is None:
        print("sidecar not running (nothing to stop)")
        return 0
    runtime = load_runtime(args.workspace_home)
    pid = int(runtime.get("pid") or 0)
    try:
        client.call("shutdown", {}, timeout=3.0)
        print(f"shutdown requested (pid={pid})")
    except (OSError, RuntimeError) as exc:
        print(f"shutdown rpc failed: {exc}", file=sys.stderr)
    finally:
        client.close()
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0) if os.name != "nt" else None
        except OSError:
            break
        if not os.path.isfile(runtime_path(args.workspace_home)) and os.name != "nt":
            break
        time.sleep(0.2)
        if not os.path.isfile(runtime_path(args.workspace_home)):
            break
    print("sidecar stopped")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    path = os.path.join(args.workspace_home, "sidecar.log")
    if not os.path.isfile(path):
        print(f"no log file: {path}", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    tail = lines[-args.lines :] if args.lines > 0 else lines
    sys.stdout.write("".join(tail))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="dsh-graph-memory sidecar control")
    parser.add_argument("--workspace-home", default=DEFAULT_WORKSPACE_HOME, help="workspace 数据目录（含 sidecar.runtime.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    wait = sub.add_parser("wait-ready")
    wait.add_argument("--timeout", type=float, default=20.0)
    wait.set_defaults(func=cmd_wait_ready)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    ping = sub.add_parser("ping")
    ping.set_defaults(func=cmd_ping)

    call = sub.add_parser("call")
    call.add_argument("method")
    call.add_argument("params", nargs="?", default="")
    call.add_argument("--timeout", type=float, default=30.0)
    call.set_defaults(func=cmd_call)

    stop = sub.add_parser("stop")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.set_defaults(func=cmd_stop)

    log = sub.add_parser("log")
    log.add_argument("--lines", type=int, default=40)
    log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
