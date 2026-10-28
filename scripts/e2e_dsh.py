"""真实 DSH headless E2E：安装插件 → 两轮会话验证记忆闭环 → 卸载验证无残留。

用法（仓库根目录）：
  py -3 scripts/e2e_dsh.py                # 完整 E2E（约 1-3 分钟，需要 DEEPSEEK_API_KEY）
  py -3 scripts/e2e_dsh.py --keep-data    # 保留 .tmp/e2e 数据便于排查
  py -3 scripts/e2e_dsh.py --skip-run     # 只重建隔离 profile 并检查安装状态

环境：
- 使用仓库内隔离 DSH_HOME（.tmp/e2e/home），绝不写 ~/.dsh；
- 模型凭据优先读环境变量 DEEPSEEK_API_KEY，否则从 ~/.dsh/.credentials.yaml 的 refs 读取；
- 插件以 link: 安装进 headless profile（junction），这是 dsh plugin add 的等价手工安装。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.path.join(ROOT, ".tmp", "e2e")
DSH_HOME = os.path.join(E2E, "home")
PROFILE = os.path.join(DSH_HOME, "profiles", "headless")
GM_USER = os.path.join(E2E, "gm-user")
GM_WS = os.path.join(E2E, "gm-ws")
RUNTIME_FILE = os.path.join(GM_WS, "sidecar.runtime.json")
BUNDLE = "dsh-graph-memory"
PLUGIN_ROW_ID = "graph-memory"

RUN1_PROMPT = "我用 pnpm 9.15 管理项目依赖，请只回复 OK。"
RUN2_PROMPT = "上次我说过用什么包管理器？只回答包管理器名字，不要解释。"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


configure_stdio()


def log(message: str) -> None:
    print(message, flush=True)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_credentials_key() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    cred = os.path.expanduser(os.path.join("~", ".dsh", ".credentials.yaml"))
    if not os.path.isfile(cred):
        return None
    try:
        import yaml  # type: ignore

        with open(cred, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return (data.get("refs") or {}).get("DEEPSEEK_API_KEY")
    except Exception:  # noqa: BLE001
        # py -3 环境可能没有 PyYAML：用极简解析兜底（credentials 文件是扁平的 refs: key: value）
        try:
            with open(cred, "r", encoding="utf-8") as fh:
                text = fh.read()
            in_refs = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("refs:"):
                    in_refs = True
                    continue
                if in_refs and stripped.startswith("DEEPSEEK_API_KEY:"):
                    return stripped.split(":", 1)[1].strip().strip("'\"")
                if in_refs and line and not line.startswith((" ", "\t")):
                    in_refs = False
        except OSError:
            return None
    return None


def remove_path(path: str) -> None:
    if not os.path.exists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            subprocess.run(["cmd", "/c", "rmdir", path], capture_output=True, text=True)


def setup_profile(install_plugin: bool) -> None:
    os.makedirs(os.path.join(PROFILE, "node_modules"), exist_ok=True)
    bundles = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]
    deps: dict[str, str] = {}
    if install_plugin:
        bundles.append(BUNDLE)
        deps[BUNDLE] = "link:" + ROOT
    manifest = {
        "name": "dsh-profile-headless",
        "private": True,
        "dependencies": deps,
        "dsh": {"profile": {"bundles": bundles}},
    }
    write_text(os.path.join(PROFILE, "package.json"), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    patch = [
        "- id: graph-memory",
        "  config:",
        "    paths:",
        f"      userDataHome: '{GM_USER}'",
        f"      workspaceDataHome: '{GM_WS}'",
        "    sidecar:",
        "      idleExitSeconds: 60",
        "      transport: auto",
        "    llm:",
        "      mode: host",
        "    panel:",
        "      enabled: false",
    ]
    if not install_plugin:
        patch = ["[]"]
    write_text(os.path.join(PROFILE, "cordis.patch.yml"), "\n".join(patch) + "\n")
    link_path = os.path.join(PROFILE, "node_modules", BUNDLE)
    remove_path(link_path)
    if install_plugin:
        subprocess.run(["cmd", "/c", "mklink", "/J", link_path, ROOT], capture_output=True, text=True)


def run_headless(prompt: str, label: str, env: dict[str, str]) -> str:
    log(f"--- {label}: dsh --profile headless ---")
    started = time.time()
    dsh_cmd = shutil.which("dsh") or "dsh"
    if dsh_cmd.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", dsh_cmd, "--profile", "headless", prompt]
    else:
        argv = [dsh_cmd, "--profile", "headless", prompt]
    result = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    elapsed = time.time() - started
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    write_text(os.path.join(E2E, f"{label}.log"), output)
    log(f"{label}: exit={result.returncode} elapsed={elapsed:.1f}s")
    log(f"{label}: stdout tail = {(result.stdout or '')[-600:]!r}")
    return output


def journal_rows(db_path: str, table: str) -> list[dict]:
    if not os.path.isfile(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def check(name: str, condition: bool, detail: object = "") -> bool:
    log(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
    return condition


def e2e_main(keep_data: bool = False) -> int:
    api_key = read_credentials_key()
    if not api_key:
        log("FAIL: 缺少 DEEPSEEK_API_KEY（环境变量或 ~/.dsh/.credentials.yaml refs）")
        return 2
    failures: list[bool] = []
    remove_path(E2E)
    os.makedirs(E2E, exist_ok=True)
    setup_profile(install_plugin=True)
    env = dict(os.environ)
    env["DSH_HOME"] = DSH_HOME
    env["DEEPSEEK_API_KEY"] = api_key
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("GRAPH_MEMORY_LLM_MODE", None)

    out1 = run_headless(RUN1_PROMPT, "run1", env)
    failures.append(check("run1 插件加载（日志出现 graph-memory loaded）", "graph-memory loaded" in out1))
    failures.append(check("run1 Sidecar 启动（stdio 或 tcp fallback）", "sidecar started" in out1))
    failures.append(check("run1 当天原文目录落盘（days/）", os.path.isdir(os.path.join(GM_WS, "days"))))
    day_dirs = []
    days_root = os.path.join(GM_WS, "days")
    if os.path.isdir(days_root):
        day_dirs = [name for name in os.listdir(days_root) if os.path.isdir(os.path.join(days_root, name))]
    failures.append(check("run1 当天 turn 原文非空", bool(day_dirs) and any(
        os.path.isfile(os.path.join(days_root, day, "turns.jsonl")) for day in day_dirs)))
    failures.append(check("run1 Journal 落盘", os.path.isfile(os.path.join(GM_WS, "journal.db"))))

    out2 = run_headless(RUN2_PROMPT, "run2", env)
    failures.append(check("run2 插件加载", "graph-memory loaded" in out2))
    answer = (out2 or "")
    failures.append(check("run2 回答包含 pnpm（记忆闭环）", "pnpm" in answer.lower(), answer[-300:].replace("\n", " ")))
    injections = journal_rows(os.path.join(GM_WS, "journal.db"), "injections")
    # run1 无历史记忆/无当天上下文 → 设计上零注入（不写 record）；run2 必须至少有一条注入记录
    failures.append(check("run2 注入记录 >=1 条", len(injections) >= 1, len(injections)))
    positive = False
    for row in injections:
        try:
            tokens = json.loads(row.get("tokens_json") or "{}")
        except json.JSONDecodeError:
            tokens = {}
        if int(tokens.get("memory") or 0) + int(tokens.get("day") or 0) > 0:
            positive = True
    failures.append(check("至少一次注入携带 memory/day 内容", positive, [row.get("tokens_json") for row in injections]))
    failures.append(check("注入 token 总量 <= 3000", all(
        int((json.loads(row.get("tokens_json") or "{}")).get("total") or 0) <= 3000 for row in injections)))

    # 卸载验证：移除 bundle，再跑一次，确认无插件日志、无残留 sidecar、数据文件保留
    log("--- uninstall verify ---")
    setup_profile(install_plugin=False)
    out3 = run_headless("只回复 UNINSTALL-CHECK。", "uninstall", env)
    failures.append(check("卸载后不再加载插件", "[graph-memory]" not in out3))
    failures.append(check("卸载后数据文件保留", os.path.isfile(os.path.join(GM_WS, "journal.db"))))
    process = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*sidecar*server.py*' } | Select-Object -ExpandProperty ProcessId"],
        capture_output=True,
        text=True,
    )
    failures.append(check("无残留 sidecar 进程", not (process.stdout or "").strip(), process.stdout.strip()))
    if not keep_data:
        try:
            shutil.rmtree(E2E, ignore_errors=True)
        except OSError:
            pass
    log("")
    if all(failures):
        log(f"E2E PASSED ({len(failures)} checks)")
        return 0
    log(f"E2E FAILED: {failures.count(False)}/{len(failures)} checks failed")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    code = e2e_main(keep_data=args.keep_data)
    if args.keep_data:
        log(f"data kept at {E2E}")
    return code


if __name__ == "__main__":
    sys.exit(main())
