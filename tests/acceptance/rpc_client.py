"""黑盒 JSON-RPC over stdio 客户端 + 测试夹具（验收测试唯一入口）。

设计要点（docs/CONTRACT.md §3 / §6）：
- spawn 真实 sidecar 子进程：`py -3 sidecar/server.py --user-home <tmp> --workspace-home <tmp>
  --workspace-root <tmp> --config <json>`，本模块只通过行分隔 JSON-RPC 2.0 通信，
  **不 import 任何 sidecar 产品模块**（独立验证）。
- 反向调用（sidecar → host，§3.3）：reader 线程识别 `host/llm`、`host/log` 请求并回
  脚本化响应；验收默认 `llm.mode=offline`（sidecar 不反向请求），scripted 模式读取
  `GRAPH_MEMORY_LLM_SCRIPT` 指向的 fixture JSON。
- 数据隔离：所有数据目录用 `tempfile.mkdtemp(prefix="gm-acc-", dir=<repo>/.tmp)`（平台
  临时目录在 DSH Windows 沙箱下可能不可写；`.tmp/` 已 gitignore），绝不写真实 `~/.dsh`。
- 伪时钟：`debug_set_now`（Lead 冻结的内部方法名）推进时间，测试不做真实 sleep。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Optional

import injection_model

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIDECAR_SERVER = os.path.join(REPO_ROOT, "sidecar", "server.py")
WORKSPACE_TMP = os.path.join(REPO_ROOT, ".tmp")

DEFAULT_REQUEST_TIMEOUT = 30.0
START_TIMEOUT = 45.0

# 伪时钟固定起点：验收用例只从「一个确定的本地时刻」向前推进，不继承真实 wall clock。
# 否则用例在 21:00 之后启动时，D1（200 轮 × 60s ≈ 3.3h）会跨过午夜触发跨天合并，
# 历史摘要计入 history 容器 → 稳态总量阶跃，得到与机器启动时间相关的假失败。
DEFAULT_CLOCK_BASE = time.mktime((2026, 3, 15, 9, 0, 0, 0, 0, -1))


def clock_base_ts() -> float:
    """验收统一时钟起点（可用 GM_ACC_CLOCK_BASE 覆盖为 epoch 秒）。"""
    raw = os.environ.get("GM_ACC_CLOCK_BASE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_CLOCK_BASE

# 缺少这些模块时 server 无法启动 → 验收侧 skip（产品仍在并行实现）
_REQUIRED_SIDECAR_MODULES = (
    "base.py",
    "config.py",
    "graph.py",
    "weight.py",
    "journal.py",
    "retrieval.py",
    "noise.py",
    "extract.py",
    "supersede.py",
    "daykeeper.py",
    "prune.py",
    "llm_bridge.py",
    "service.py",
    "server.py",
)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, *, method: str = "") -> None:
        super().__init__(f"[{code}] {message}" + (f" (method={method})" if method else ""))
        self.code = int(code)
        self.message = message
        self.data = data
        self.method = method


class RpcTimeout(Exception):
    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"JSON-RPC timeout after {timeout}s: {method}")
        self.method = method
        self.timeout = timeout


class SidecarDied(Exception):
    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


class SidecarStartError(Exception):
    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


def missing_sidecar_modules() -> list[str]:
    return [name for name in _REQUIRED_SIDECAR_MODULES if not os.path.isfile(os.path.join(REPO_ROOT, "sidecar", name))]


# --------------------------------------------------------------------------- #
# Python 解释器 / 临时目录
# --------------------------------------------------------------------------- #
def python_argv() -> list[str]:
    """优先 `py -3`（契约要求），否则回退当前解释器。"""
    override = os.environ.get("GM_ACC_PYTHON")
    if override:
        return [override]
    py = shutil.which("py")
    if py:
        return [py, "-3"]
    return [sys.executable]


def make_temp_dir(prefix: str = "gm-acc-") -> str:
    """创建测试专用临时目录：优先仓库 `.tmp/`（沙箱可写），回退系统临时目录。

    注意：不能使用 `tempfile.mkdtemp` —— 它用 `os.mkdir(path, 0o700)` 创建目录，
    而当前 DSH Windows 沙箱下 0o700 新建目录内的写入会被拒绝（WinError 5）；
    这里用默认权限的 `os.makedirs` + 随机名规避（目录内容与隔离性不变）。
    """
    last_error: Optional[BaseException] = None
    for base in (os.environ.get("GM_ACC_TMPDIR"), WORKSPACE_TMP, tempfile.gettempdir()):
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as exc:  # 平台临时目录在沙箱下可能不可写
            last_error = exc
            continue
        for _ in range(200):
            path = os.path.join(base, prefix + uuid.uuid4().hex[:10])
            try:
                os.makedirs(path)
                return path
            except FileExistsError:
                continue
            except OSError as exc:
                last_error = exc
                break
    raise SidecarStartError(f"cannot create temp dir: {last_error!r}")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# 底层客户端
# --------------------------------------------------------------------------- #
class _Slot:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Any = None
        self.error: Optional[Exception] = None


class SidecarClient:
    """行分隔 JSON-RPC 2.0 over stdio 客户端（线程安全）。"""

    def __init__(
        self,
        *,
        config_overrides: Optional[dict[str, Any]] = None,
        llm_mode: str = "offline",
        llm_script: Optional[dict[str, Any]] = None,
        reverse_handler: Optional[Callable[[str, dict[str, Any]], Any]] = None,
        workdir: Optional[str] = None,
        name: str = "",
        keep_temp: Optional[bool] = None,
        clock_base: Optional[float] = None,
    ) -> None:
        self.name = name or "sidecar"
        self._owns_workdir = workdir is None
        self.clock_base = clock_base_ts() if clock_base is None else float(clock_base)
        self.workdir = workdir or make_temp_dir()
        self.user_home = os.path.join(self.workdir, "user-home")
        self.workspace_home = os.path.join(self.workdir, "workspace-home")
        self.project_root = os.path.join(self.workdir, "project")
        for path in (self.user_home, self.workspace_home, self.project_root):
            os.makedirs(path, exist_ok=True)
        self.keep_temp = bool(int(os.environ.get("GM_ACC_KEEP", "0"))) if keep_temp is None else keep_temp

        self.config = _deep_merge(
            {
                "panel": {"enabled": False},
                "sidecar": {"idleExitSeconds": 600},
                "logging": {"level": "warning"},
            },
            config_overrides or {},
        )
        self.config.setdefault("llm", {})
        if llm_mode:
            self.config["llm"]["mode"] = llm_mode
        self.llm_mode = self.config["llm"].get("mode", "offline")

        self._llm_script_path: Optional[str] = None
        if llm_script is not None:
            self._llm_script_path = os.path.join(self.workdir, "llm_script.json")
            with open(self._llm_script_path, "w", encoding="utf-8") as fh:
                json.dump(llm_script, fh, ensure_ascii=False, indent=1)

        self._config_path = os.path.join(self.workdir, "sidecar-config.json")
        with open(self._config_path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False, indent=1)

        self._reverse_handler = reverse_handler or self._default_reverse_handler
        self._proc: Optional[subprocess.Popen] = None
        self._stdin_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _Slot] = {}
        self._next_id = 0
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_lines: deque[str] = deque(maxlen=800)
        self._stderr_log_path = os.path.join(self.workdir, "server-stderr.log")
        self.notifications: list[dict[str, Any]] = []
        self.reverse_calls: list[dict[str, Any]] = []
        self._ready = threading.Event()
        self._closed = False
        self._reader_alive = True
        self._start_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def stderr_tail(self, limit: int = 30) -> str:
        return "\n".join(list(self._stderr_lines)[-limit:])

    def start(self, *, timeout: float = START_TIMEOUT) -> "SidecarClient":
        if not os.path.isfile(SIDECAR_SERVER):
            raise SidecarStartError(f"sidecar/server.py missing: {SIDECAR_SERVER}")
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("GRAPH_MEMORY_USER_HOME", None)
        env.pop("GRAPH_MEMORY_WORKSPACE_HOME", None)
        env.pop("GRAPH_MEMORY_LLM_MODE", None)
        env.pop("GRAPH_MEMORY_WORKSPACE_ROOT", None)
        if self._llm_script_path:
            env["GRAPH_MEMORY_LLM_SCRIPT"] = self._llm_script_path
        cmd = [
            *python_argv(),
            SIDECAR_SERVER,
            "--user-home",
            self.user_home,
            "--workspace-home",
            self.workspace_home,
            "--workspace-root",
            self.project_root,
            "--config",
            self._config_path,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise SidecarStartError(f"failed to spawn sidecar: {exc}") from exc
        self._stdout_thread = threading.Thread(target=self._read_stdout, name=f"{self.name}-stdout", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"{self.name}-stderr", daemon=True)
        self._stderr_thread.start()
        self._wait_ready(timeout=timeout)
        self._pin_clock()
        return self

    def _pin_clock(self) -> None:
        """把 sidecar 伪时钟钉到统一起点，保证任意时刻运行结果一致。"""
        try:
            self.debug_set_now(ts=self.clock_base)
        except (RpcError, MethodMissing):
            # 老版本 sidecar 无 debug_set_now：退回真实时钟（用例可能受跨天影响）
            pass

    def _wait_ready(self, *, timeout: float) -> None:
        deadline = time.time() + timeout
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                self._drain_startup_stderr()
                raise SidecarStartError(
                    f"sidecar exited during startup (code={self._proc.returncode})",
                    stderr_tail=self.stderr_tail(),
                )
            try:
                result = self.request("ping", {}, timeout=3.0)
                if isinstance(result, dict) and result.get("pong"):
                    self._ready.set()
                    return
            except RpcTimeout as exc:
                last_error = exc
            except SidecarDied as exc:
                self._drain_startup_stderr()
                raise SidecarStartError(f"sidecar died during startup: {exc}", stderr_tail=self.stderr_tail()) from exc
            except RpcError as exc:  # 尚未就绪时的业务错误：重试
                last_error = exc
            time.sleep(0.05)
        self._drain_startup_stderr()
        raise SidecarStartError(
            f"sidecar did not answer ping within {timeout}s: {last_error!r}",
            stderr_tail=self.stderr_tail(),
        )

    def _drain_startup_stderr(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        time.sleep(0.05)

    def close(self, *, timeout: float = 8.0) -> None:
        if self._closed:
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # 先发 shutdown（注意：必须在 _closed=True 之前发，否则 request() 会拒发）
            try:
                self.request("shutdown", {}, timeout=3.0)
            except Exception:  # noqa: BLE001 — 关闭阶段任何错误都可忽略
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        self._closed = True
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=3)
        self._fail_pending(SidecarDied("sidecar closed"))
        if self._owns_workdir and not self.keep_temp:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "SidecarClient":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 线程：stdout / stderr / 反向请求
    # ------------------------------------------------------------------ #
    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            self._dispatch_message(message)
        self._reader_alive = False
        if not self._closed:
            self._fail_pending(SidecarDied("sidecar stdout closed", stderr_tail=self.stderr_tail()))

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            with open(self._stderr_log_path, "a", encoding="utf-8") as log:
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    self._stderr_lines.append(line)
                    log.write(line + "\n")
                    log.flush()
        except (OSError, ValueError):
            return

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method is not None:
            if msg_id is None:
                self.notifications.append(message)
                if method == "ready":
                    self._ready.set()
                return
            # sidecar → host 反向请求：放到独立线程处理，避免阻塞 stdout reader
            thread = threading.Thread(
                target=self._handle_reverse,
                args=(msg_id, str(method), message.get("params") or {}),
                daemon=True,
            )
            thread.start()
            return
        # 响应
        slot: Optional[_Slot]
        with self._pending_lock:
            slot = self._pending.pop(msg_id, None)
        if slot is None:
            return
        if "error" in message and message.get("error") is not None:
            error = message.get("error") or {}
            slot.error = RpcError(
                int(error.get("code") or -32603),
                str(error.get("message") or ""),
                error.get("data"),
            )
        else:
            slot.response = message.get("result")
        slot.event.set()

    def _handle_reverse(self, msg_id: Any, method: str, params: dict[str, Any]) -> None:
        entry = {"method": method, "params": params}
        self.reverse_calls.append(entry)
        try:
            result = self._reverse_handler(method, params)
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except RpcError as exc:
            payload = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": exc.code, "message": exc.message, "data": exc.data},
            }
        except Exception as exc:  # noqa: BLE001
            payload = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": repr(exc)}}
        try:
            self._write(payload)
        except (SidecarDied, OSError):
            return

    @staticmethod
    def _default_reverse_handler(method: str, params: dict[str, Any]) -> Any:
        if method == "host/log":
            return {"ok": True}
        if method == "host/llm":
            return {"text": ""}
        raise RpcError(-32601, f"unknown host method: {method}")

    # ------------------------------------------------------------------ #
    # 写入 / 请求
    # ------------------------------------------------------------------ #
    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise SidecarDied("sidecar process is not running", stderr_tail=self.stderr_tail())
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._stdin_lock:
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise SidecarDied(f"failed to write to sidecar stdin: {exc}", stderr_tail=self.stderr_tail()) from exc

    def request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Any:
        if self._closed:
            raise SidecarDied("client already closed")
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise SidecarDied(
                f"sidecar not running (method={method}, exit={None if proc is None else proc.returncode})",
                stderr_tail=self.stderr_tail(),
            )
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            slot = _Slot()
            self._pending[request_id] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not slot.event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise RpcTimeout(method, timeout)
        if slot.error is not None:
            error = slot.error
            assert isinstance(error, RpcError)
            error.method = method
            raise error
        return slot.response

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        """发送通知（无 id，宿主→sidecar）。"""
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def raw_stdin(self, line: str) -> None:
        """直接写原始行（用于协议异常用例，如非法 JSON）。"""
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        with self._stdin_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot.error = error
            slot.event.set()

    # ------------------------------------------------------------------ #
    # 便捷方法（全部走公开/内部 JSON-RPC，不经 Python import）
    # ------------------------------------------------------------------ #
    def ping(self) -> dict[str, Any]:
        return self.request("ping", {})

    def ingest(self, turn_text: str, *, scope: str = "workspace", **extra: Any) -> dict[str, Any]:
        params = {"turn_text": turn_text, "scope": scope, **extra}
        return self.request("ingest", params, timeout=60.0)

    def retrieve(
        self,
        query: str,
        *,
        scope: Optional[list[str]] = None,
        explicit: bool = False,
        budget: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query,
            "scope": scope if scope is not None else ["workspace"],
            "explicit": explicit,
        }
        if budget is not None:
            params["budget"] = budget
        return self.request("retrieve", params, timeout=timeout)

    def day_pack(self, query: str, *, scope: Optional[list[str]] = None, timeout: float = 60.0) -> dict[str, Any]:
        params = {"query": query, "scope": scope if scope is not None else ["workspace"]}
        return self.request("day_pack", params, timeout=timeout)

    def activate(self, ids: list[str], *, reason: str = "injected") -> dict[str, Any]:
        return self.request("activate", {"ids": ids, "reason": reason})

    def update(self, query: str, new_value: str, *, scope: str = "workspace") -> dict[str, Any]:
        return self.request("update", {"query": query, "new_value": new_value, "scope": scope})

    def stats(self, *, scope: Optional[str] = None) -> dict[str, Any]:
        params = {} if scope is None else {"scope": scope}
        return self.request("stats", params)

    def panel_state(self, *, timeout: float = 60.0) -> dict[str, Any]:
        return self.request("panel_state", {}, timeout=timeout)

    def journal_list(self, *, scope: str = "workspace", target_id: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"scope": scope}
        if target_id:
            params["target_id"] = target_id
        return self.request("journal_list", params)

    def journal_rollback(self, change_id: int) -> dict[str, Any]:
        return self.request("journal_rollback", {"change_id": change_id})

    def compress(self, *, scope: str = "workspace") -> dict[str, Any]:
        return self.request("compress", {"scope": scope}, timeout=120.0)

    def prune(self, *, scope: str = "workspace", dry_run: bool = True) -> dict[str, Any]:
        return self.request("prune", {"scope": scope, "dry_run": dry_run}, timeout=120.0)

    def round_tick(self, *, scope: str = "workspace") -> dict[str, Any]:
        return self.request("round_tick", {"scope": scope})

    def maintenance(self, *, scope: str = "workspace") -> dict[str, Any]:
        return self.request("maintenance", {"scope": scope}, timeout=120.0)

    def injection_record(self, **params: Any) -> dict[str, Any]:
        return self.request("injection_record", params)

    def tokens(self, texts: list[str]) -> list[int]:
        result = self.request("tokens", {"texts": texts})
        return list(result.get("tokens") or [])

    def conflicts_sweep(self, *, scope: str = "workspace", days: int = 30) -> dict[str, Any]:
        return self.request("conflicts_sweep", {"scope": scope, "days": days}, timeout=120.0)

    # ---- 伪时钟（§2 时间模拟；Lead 冻结的内部方法名） -------------------
    def debug_set_now(self, ts: Optional[float] = None, *, advance_seconds: Optional[float] = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ts is not None:
            params["ts"] = float(ts)
        if advance_seconds is not None:
            params["advance_seconds"] = float(advance_seconds)
        return self.request("debug_set_now", params)

    def advance(self, seconds: float) -> dict[str, Any]:
        return self.debug_set_now(advance_seconds=seconds)

    def today(self) -> str:
        return str(self.debug_state().get("today") or "")

    def debug_state(self) -> dict[str, Any]:
        return self.request("debug_state", {})

    # ---- 造数 / 内部状态（§3.2 内部方法 + Lead 冻结的 debug_*） ---------
    def debug_seed_entities(
        self,
        count: int,
        *,
        prefix: str = "synthetic",
        scope: str = "workspace",
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        return self.request(
            "debug_seed_entities",
            {"scope": scope, "count": int(count), "prefix": prefix},
            timeout=timeout,
        )

    def debug_entity_set(self, entity_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("debug_entity_set", {"id": entity_id, "properties": properties})

    def list_nodes(self, *, scope: str = "workspace") -> list[dict[str, Any]]:
        payload = self.panel_state()
        nodes = payload.get("nodes") or {}
        return list(nodes.get(scope) or [])

    def has_method(self, method: str) -> bool:
        """探测方法是否存在（-32601 → False；其他结果/错误都视为存在）。"""
        try:
            self.request(method, {}, timeout=5.0)
            return True
        except RpcError as exc:
            return exc.code != -32601
        except RpcTimeout:
            return True

    def require_method(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: float = 30.0) -> Any:
        """调用方法；不存在时抛 unittest.SkipTest（延迟到调用方，避免本模块依赖 unittest）。"""
        try:
            return self.request(method, params or {}, timeout=timeout)
        except RpcError as exc:
            if exc.code == -32601:
                raise MethodMissing(method) from exc
            raise


class MethodMissing(Exception):
    def __init__(self, method: str) -> None:
        super().__init__(f"sidecar method not implemented yet: {method}")
        self.method = method


# --------------------------------------------------------------------------- #
# 验收夹具：一个用例一个隔离 sidecar
# --------------------------------------------------------------------------- #
class SidecarFixture:
    """一个隔离的 sidecar 子进程 + 数据目录 + 预算计量辅助。"""

    def __init__(self, client: SidecarClient) -> None:
        self.client = client

    # -- 构造 -----------------------------------------------------------
    @classmethod
    def spawn(
        cls,
        *,
        config_overrides: Optional[dict[str, Any]] = None,
        llm_mode: str = "offline",
        llm_script: Optional[dict[str, Any]] = None,
        reverse_handler: Optional[Callable[[str, dict[str, Any]], Any]] = None,
        name: str = "",
    ) -> "SidecarFixture":
        client = SidecarClient(
            config_overrides=config_overrides,
            llm_mode=llm_mode,
            llm_script=llm_script,
            reverse_handler=reverse_handler,
            name=name,
        )
        client.start()
        return cls(client)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "SidecarFixture":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- 透传 -----------------------------------------------------------
    def __getattr__(self, item: str) -> Any:
        return getattr(self.client, item)

    # -- 注入计量（独立 Python 镜像 + 真实 sidecar 数据） ----------------
    def measure_injection(
        self,
        query: str,
        *,
        scope: Optional[list[str]] = None,
        explicit: bool = False,
        system_text: Optional[str] = None,
        limits: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        scopes = scope if scope is not None else ["user", "workspace"]
        retrieve = self.client.retrieve(query, scope=scopes, explicit=explicit)
        day = self.client.day_pack(query, scope=scopes)
        memory_text = "\n".join(
            str(item.get("rendered") or item.get("text") or "") for item in (retrieve.get("items") or [])
        )
        assembled = injection_model.assemble_injection(
            system_text=system_text if system_text is not None else injection_model.SYSTEM_INSTRUCTIONS,
            memory_text=memory_text,
            day_pack=day,
            retrieve=retrieve,
            explicit=explicit,
            limits=limits,
        )
        assembled["retrieve"] = {
            "items": len(retrieve.get("items") or []),
            "top1_sim": retrieve.get("top1_sim"),
            "high_weight_count": retrieve.get("high_weight_count"),
            "injected_ids": list(retrieve.get("injected_ids") or []),
        }
        assembled["day_pack_tokens"] = day.get("tokens")
        return assembled


# --------------------------------------------------------------------------- #
# 噪声数据生成（F3 用；确定性随机）
# --------------------------------------------------------------------------- #
_NOISE_FILLERS = ("嗯", "好的", "收到", "先这样", "稍等", "我看下", "可以", "行", "对", "是的")
_MEANINGFUL_WORDS = (
    "依赖管理",
    "构建脚本",
    "缓存策略",
    "接口超时",
    "数据库连接",
    "部署流程",
    "日志采集",
    "权限模型",
    "灰度发布",
    "压测报告",
    "版本号",
    "负责人",
)
_SIGNAL_PATTERNS = (
    "明天下午三点开会",
    "价格改成 80",
    "改用 Redis",
    "决定用 pnpm",
    "这个接口谁负责？",
)
_PROPER_NOUNS = ("pnpm", "Redis", "PostgreSQL", "TypeScript", "DeepSeek", "Windows")
_IDENTIFIERS = ("build_cache", "src/sidecar/server.py", "memory_recall()", "api.timeoutMs", "user@example.com")


def generate_short_sentences(count: int = 100, seed: int = 20261028) -> list[str]:
    """生成 8~30 字、必然带“放行信号”的短句（F3：误杀=0）。"""
    rng = random.Random(seed)
    sentences: list[str] = []
    while len(sentences) < count:
        kind = rng.randrange(6)
        word = rng.choice(_MEANINGFUL_WORDS)
        filler = rng.choice(_NOISE_FILLERS)
        if kind == 0:
            text = f"{word}改成 {rng.randrange(10, 999)} 了"
        elif kind == 1:
            text = f"明天 {rng.randrange(1, 29)} 号处理{word}"
        elif kind == 2:
            text = f"决定用 {rng.choice(_PROPER_NOUNS)} 做{word}"
        elif kind == 3:
            text = f"{rng.choice(_IDENTIFIERS)} 这个{word}要改"
        elif kind == 4:
            text = f"{word}为什么变慢了？"
        else:
            text = f"{filler}，{word}用 {rng.choice(_PROPER_NOUNS)}"
        text = text.strip()
        if 8 <= len(text) <= 30 and text not in sentences:
            sentences.append(text)
    return sentences


# --------------------------------------------------------------------------- #
# unittest 辅助（在 test_cases / test_protocol 中使用）
# --------------------------------------------------------------------------- #
def spawn_or_skip(testcase: Any, **kwargs: Any) -> SidecarFixture:
    """启动夹具；产品模块缺失/sidecar 起不来时 skip 并记录原因。"""
    import unittest

    missing = missing_sidecar_modules()
    if missing:
        raise unittest.SkipTest(f"sidecar 模块未就绪，缺少: {', '.join(missing)}")
    try:
        fixture = SidecarFixture.spawn(**kwargs)
    except SidecarStartError as exc:
        tail = exc.stderr_tail or ""
        if "ModuleNotFoundError" in tail or "No module named" in tail or "SyntaxError" in tail:
            raise unittest.SkipTest(f"sidecar 无法启动（产品模块未就绪）: {tail.strip().splitlines()[-1] if tail.strip() else exc}")
        raise
    testcase.addCleanup(fixture.close)
    return fixture


def record_evidence(key: str, value: Any) -> None:
    """把证据写入模块级 EVIDENCE（run_acceptance 读取生成报告）。"""
    import evidence

    evidence.record(key, value)
