"""Sidecar 冒烟验证（M0/M1）：启动子进程 → ping → ingest → retrieve → day_pack → stats → shutdown。

用法（仓库根目录）：
  py -3 scripts/smoke_sidecar.py
  py -3 scripts/smoke_sidecar.py --keep-data      # 保留临时数据便于排查
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "sidecar", "server.py")


def _configure_stdio() -> None:
    """Windows 控制台默认 GBK：统一成 UTF-8 + replace，避免验收结果因编码失败。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


_configure_stdio()


class Client:
    def __init__(self, user_home: str, workspace_home: str) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                SERVER,
                "--user-home",
                user_home,
                "--workspace-home",
                workspace_home,
                "--workspace-root",
                os.path.dirname(workspace_home) or ROOT,
                "--config",
                json.dumps({"llm": {"mode": "offline"}, "panel": {"enabled": False}}),
                "--panel-port",
                "19629",
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.next_id = 1
        self.deadline = time.time() + 20

    def _read_message(self):
        while time.time() < self.deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("sidecar closed stdout")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if "method" in message and "id" in message:  # 反向调用
                if message["method"] == "host/llm":
                    self._write({"jsonrpc": "2.0", "id": message["id"], "result": {"text": ""}})
                else:
                    self._write({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})
                continue
            if "id" in message:
                return message
        raise RuntimeError("sidecar response timeout")

    def _write(self, payload) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params=None):
        self.next_id += 1
        request_id = self.next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        while True:
            message = self._read_message()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message.get("result")

    def close(self) -> None:
        try:
            self.call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        stderr = self.proc.stderr.read() if self.proc.stderr else ""
        if stderr.strip():
            print("[sidecar stderr tail]")
            print("\n".join(stderr.strip().splitlines()[-8:]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    # 注意：本环境 Windows 沙箱下 tempfile.mkdtemp 生成的目录 ACL 受限（子进程再创建目录会
    # WinError 5），因此统一在仓库 .tmp/ 下用普通 mkdir 创建唯一目录。
    tmp_base = os.path.join(ROOT, ".tmp")
    os.makedirs(tmp_base, exist_ok=True)
    tmp = os.path.join(tmp_base, f"gm-smoke-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp, exist_ok=True)
    user_home = os.path.join(tmp, "user")
    workspace_home = os.path.join(tmp, "workspace")
    os.makedirs(user_home, exist_ok=True)
    os.makedirs(workspace_home, exist_ok=True)
    client = Client(user_home, workspace_home)
    failures: list[str] = []

    def check(name: str, condition: bool, detail: object = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name} {detail}")
        if not condition:
            failures.append(name)

    try:
        pong = client.call("ping")
        check("ping/pong", pong.get("pong") is True, pong)
        print("  llm_mode:", pong.get("llm_mode"), "backend:", pong.get("backend"))

        ingested = client.call(
            "ingest",
            {
                "turn_text": "用户：决定用 pnpm 管理依赖，版本是 9.15。助手：好的，已记录 pnpm。",
                "turn_id": "turn-smoke-1",
                "scope": "workspace",
            },
        )
        check("ingest 写入节点", len(ingested.get("node_ids") or []) >= 1, ingested.get("node_ids"))
        check("ingest 有 chunk", bool(ingested.get("chunk_id")), ingested.get("chunk_id"))

        retrieved = client.call(
            "retrieve",
            {"query": "上次我说过用什么包管理器", "scope": ["user", "workspace"], "budget": {"mem_max": 1024}},
        )
        items = retrieved.get("items") or []
        check("retrieve 命中记忆", len(items) >= 1, [item.get("id") for item in items])
        check("retrieve injected_ids 一致", set(retrieved.get("injected_ids") or []) == {item["id"] for item in items})
        if items:
            print("  top item:", items[0].get("rendered") or items[0].get("text"))
            activated = client.call("activate", {"ids": retrieved["injected_ids"], "reason": "injected"})
            check("activate 更新权重", len(activated.get("updated") or []) == len(retrieved["injected_ids"]), activated)

        packed = client.call("day_pack", {"query": "包管理器", "scope": "workspace"})
        check("day_pack 预算内", int(packed.get("tokens", {}).get("total") or 0) <= 1024, packed.get("tokens"))
        check("day_pack 有当天内容", bool(packed.get("day_text")), packed.get("day_text", "")[:60])

        stats = client.call("stats", {})
        nodes = stats.get("scopes", {}).get("workspace", {}).get("nodes")
        check("stats 图谱规模", isinstance(nodes, int) and nodes >= 1, nodes)

        tokens = client.call("tokens", {"texts": ["你好 world", "abcdefgh", ""]})
        check("tokens 与公式一致", tokens.get("tokens") == [4, 2, 0], tokens)

        # 改口闭环（C 用例的最小冒烟）：update 走 supersede 六步 + Journal
        updated = client.call("update", {"query": "包管理器", "new_value": "用户改口：现在改用 yarn 管理依赖", "scope": "workspace"})
        check("update 覆盖式改写", bool(updated.get("node_id")), updated)
        journals = client.call("journal_list", {"scope": "workspace"})
        entries = journals.get("entries") or []
        check("Journal 记录改口 before→after", len(entries) >= 1 and entries[0].get("kind") == "supersede", entries[:1])
        if entries:
            rollback = client.call("journal_rollback", {"change_id": entries[0]["id"]})
            check("Journal 7 天回滚", bool(rollback.get("restored")), rollback.get("restored"))
    finally:
        client.close()
    if args.keep_data:
        print("data dir:", tmp)
    else:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\nFAILED: {len(failures)} -> {failures}")
        return 1
    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
