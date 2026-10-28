#!/usr/bin/env bash
# =============================================================================
# dsh-graph-memory 一键启动（本地 Sidecar + 只读面板）
#
#   ./start.sh                  # 构建（必要时）+ 启动 sidecar/panel，Ctrl+C 停止
#   ./start.sh --no-build       # 跳过构建
#   ./start.sh --port 9629      # 面板起始端口（被占用会自动顺延）
#   ./start.sh --install-profile web   # 额外把插件 link 安装进 DSH profile（需重启 profile）
#
# 说明：
# - 记忆插件本体由 DSH 在启动时加载；本脚本负责把“记忆服务 + 面板”单独拉起来，
#   方便在 DSH 之外试用/调试（面板 http://127.0.0.1:9629）。
# - 支持 Git Bash / WSL / Linux / macOS；Python 仅需标准库。
# - 常驻模式通过 sidecar.idleExitSeconds=0 关闭空闲退出（Ctrl+C 或 `./stop.sh` 停止）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=9629
BUILD_MODE="auto"          # auto | always | never
PANEL_ENABLED=true
USER_HOME_OVERRIDE=""
WS_HOME_OVERRIDE=""
LOG_OVERRIDE=""
INSTALL_PROFILE=""
READY_TIMEOUT=25

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) BUILD_MODE="always"; shift ;;
    --no-build) BUILD_MODE="never"; shift ;;
    --port) PORT="${2:?--port 需要端口号}"; shift 2 ;;
    --user-home) USER_HOME_OVERRIDE="${2:?}"; shift 2 ;;
    --workspace-home) WS_HOME_OVERRIDE="${2:?}"; shift 2 ;;
    --log) LOG_OVERRIDE="${2:?}"; shift 2 ;;
    --install-profile) INSTALL_PROFILE="${2:?}"; shift 2 ;;
    --no-panel) PANEL_ENABLED=false; shift ;;
    --debug) GM_DEBUG=1; shift ;;
    --timeout) READY_TIMEOUT="${2:?}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1（--help 查看用法）" >&2; exit 2 ;;
  esac
done

# --debug / GM_DEBUG=1：打印每条命令，便于排查环境问题
if [[ "${GM_DEBUG:-}" == "1" ]]; then
  set -x
fi

# ---------------------------------------------------------------- 工具函数 ---
native_path() {
  # Git Bash/MSYS 下把 POSIX 路径转成 Windows 原生路径（写进 JSON/传给 Python 用）
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

is_windows() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

fail() {
  echo "" >&2
  echo "✗ $*" >&2
  echo "" >&2
  exit 1
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " dsh-graph-memory 一键启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------- Python ---
PY_BIN=""
PY_ARGS=()
pick_python() {
  local cand bin arg rest
  for cand in "python3" "python" "py -3"; do
    read -r bin arg rest <<<"$cand"
    local -a cmd=("$bin")
    if [[ -n "${arg:-}" ]]; then cmd+=("$arg"); fi
    if out=$("${cmd[@]}" -c 'import sys,sqlite3;print("gm-python-ok", sys.version.split()[0])' 2>/dev/null) && [[ "$out" == gm-python-ok* ]]; then
      PY_BIN="$bin"
      if [[ -n "${arg:-}" ]]; then PY_ARGS=("$arg"); fi
      return 0
    fi
  done
  return 1
}
pick_python || fail "未找到可用的 Python（3.10+）。请安装 Python，或用 PATH 暴露 python3/python/py。"
echo "· Python : $PY_BIN ${PY_ARGS[*]:-} ($("${PY_BIN}" "${PY_ARGS[@]}" -c 'import sys;print(sys.version.split()[0])'))"

gm_py() {
  if [[ ${#PY_ARGS[@]} -gt 0 ]]; then
    "${PY_BIN}" "${PY_ARGS[@]}" "$@"
  else
    "${PY_BIN}" "$@"
  fi
}

# ---------------------------------------------------------------- 构建 -----
TS_ENTRY="$ROOT/lib/index.js"
needs_build() {
  [[ "$BUILD_MODE" == "never" ]] && return 1
  [[ "$BUILD_MODE" == "always" ]] && return 0
  [[ ! -f "$TS_ENTRY" ]] && return 0
  # 任一 src/*.ts 比 lib/index.js 新则需要重新构建
  if find "$ROOT/src" -name '*.ts' -newer "$TS_ENTRY" -print -quit 2>/dev/null | grep -q .; then
    return 0
  fi
  return 1
}
if needs_build; then
  echo "· 构建   : TypeScript -> lib/"
  if [[ ! -x "$ROOT/node_modules/.bin/tsc" && ! -f "$ROOT/node_modules/typescript/bin/tsc" ]]; then
    command -v npm >/dev/null 2>&1 || fail "缺少 npm，无法安装构建依赖；可手动 npm install 后重试。"
    ( cd "$ROOT" && npm_config_cache="$ROOT/.npm-cache" npm install --no-audit --no-fund ) || fail "npm install 失败"
  fi
  if [[ -f "$ROOT/node_modules/typescript/bin/tsc" ]]; then
    node "$ROOT/node_modules/typescript/bin/tsc" -p "$ROOT/tsconfig.json" || fail "TypeScript 构建失败"
  else
    ( cd "$ROOT" && npx tsc -p tsconfig.json ) || fail "TypeScript 构建失败"
  fi
else
  echo "· 构建   : 跳过（lib/ 已就绪，可用 --build 强制重建）"
fi

# ---------------------------------------------------------------- 路径 -----
USER_HOME="${USER_HOME_OVERRIDE:-$HOME/.dsh/graph-memory}"
WS_HOME="${WS_HOME_OVERRIDE:-$ROOT/.dsh/graph-memory}"
LOG_FILE="${LOG_OVERRIDE:-$WS_HOME/sidecar.log}"
mkdir -p "$USER_HOME" "$WS_HOME" "$(dirname "$LOG_FILE")"

USER_HOME_NATIVE="$(native_path "$USER_HOME")"
WS_HOME_NATIVE="$(native_path "$WS_HOME")"
ROOT_NATIVE="$(native_path "$ROOT")"

# ---------------------------------------------------------------- 配置 -----
# mktemp 模板必须以连续 X 结尾（GNU/MSYS mktemp 不保证支持 X 后接后缀），扩展名不影响解析
CONFIG_FILE="$(mktemp "${TMPDIR:-/tmp}/graph-memory-start.XXXXXX")"
CONFIG_FILE_NATIVE="$(native_path "$CONFIG_FILE")"
# 注意：传给原生 Python 的环境变量不会被 MSYS 自动转换路径，必须给原生路径
export GM_USER_HOME="$USER_HOME_NATIVE" GM_WS_HOME="$WS_HOME_NATIVE" GM_PANEL_PORT="$PORT" GM_PANEL_ENABLED="$PANEL_ENABLED" GM_CONFIG_FILE="$CONFIG_FILE_NATIVE"
gm_py - <<'PYEOF'
import json, os
config = {
    "paths": {
        "userDataHome": os.environ["GM_USER_HOME"],
        "workspaceDataHome": os.environ["GM_WS_HOME"],
    },
    "sidecar": {
        "idleExitSeconds": 0,          # 0 = 常驻（面板/演示模式不自动退出）
        "panelPort": int(os.environ["GM_PANEL_PORT"]),
        "panelHost": "127.0.0.1",
    },
    "panel": {
        "enabled": os.environ["GM_PANEL_ENABLED"].lower() != "false",
        "port": int(os.environ["GM_PANEL_PORT"]),
        "host": "127.0.0.1",
    },
    "logging": {"level": "info"},
}
with open(os.environ["GM_CONFIG_FILE"], "w", encoding="utf-8", newline="\n") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PYEOF

# ---------------------------------------------------------------- 启动 -----
TOKEN="$(gm_py -c 'import secrets;print(secrets.token_hex(18))')"
SIDECAR_PID=""
STARTED=0

cleanup() {
  local code=$?
  if [[ "$STARTED" == "1" && -n "$SIDECAR_PID" ]]; then
    echo ""
    echo "· 停止    : 关闭 sidecar (pid=$SIDECAR_PID) ..."
    gm_py "$ROOT/scripts/sidecar_ctl.py" --workspace-home "$WS_HOME_NATIVE" stop >/dev/null 2>&1 || true
    if kill -0 "$SIDECAR_PID" 2>/dev/null; then
      kill "$SIDECAR_PID" 2>/dev/null || true
    fi
    wait "$SIDECAR_PID" 2>/dev/null || true
  fi
  rm -f "$CONFIG_FILE" 2>/dev/null || true
  if [[ "$code" == "130" || "$code" == "143" ]]; then
    echo "· 已停止（数据与日志保留）"
    exit 0
  fi
  exit "$code"
}
trap cleanup EXIT INT TERM

echo "· 数据   : user=$USER_HOME"
echo "           workspace=$WS_HOME"
echo "· 日志   : $LOG_FILE"
: >"$LOG_FILE"
echo "· 启动   : sidecar (loopback tcp, 常驻模式) ..."
gm_py "$ROOT/sidecar/server.py" \
  --user-home "$USER_HOME_NATIVE" \
  --workspace-home "$WS_HOME_NATIVE" \
  --workspace-root "$ROOT_NATIVE" \
  --config "$CONFIG_FILE_NATIVE" \
  --panel-port "$PORT" \
  --transport tcp \
  --token "$TOKEN" \
  >>"$LOG_FILE" 2>&1 &
SIDECAR_PID=$!
STARTED=1

READY_JSON=""
if ! READY_JSON="$(gm_py "$ROOT/scripts/sidecar_ctl.py" --workspace-home "$WS_HOME_NATIVE" wait-ready --timeout "$READY_TIMEOUT" 2>/dev/null)"; then
  echo ""
  echo "✗ sidecar 启动失败，日志尾部：" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  exit 1
fi
SIDE_PID_DISPLAY="$(printf '%s' "$READY_JSON" | sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p')"
[[ -n "$SIDE_PID_DISPLAY" ]] || SIDE_PID_DISPLAY="$SIDECAR_PID"

PANEL_URL="$(grep -o 'http://127\.0\.0\.1:[0-9]\+' "$LOG_FILE" | tail -n 1 || true)"
if [[ -z "$PANEL_URL" ]]; then
  PANEL_URL="http://127.0.0.1:$PORT"
fi
if [[ "$PANEL_ENABLED" == "true" ]]; then
  gm_py - "$PANEL_URL" <<'PYEOF' >/dev/null 2>&1 || true
import sys, urllib.request
url, timeout = sys.argv[1], 5
with urllib.request.urlopen(url.rstrip('/') + '/healthz', timeout=timeout) as response:
    assert response.status == 200
PYEOF
fi

if [[ -n "$INSTALL_PROFILE" ]]; then
  echo "· 安装   : 把插件 link 安装进 DSH profile '$INSTALL_PROFILE' ..."
  if command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -File "$ROOT/scripts/install-plugin.ps1" -Profile "$INSTALL_PROFILE" -SkipBuild || echo "⚠ 安装脚本失败，请手动按 docs/INSTALL.md 安装"
  else
    echo "⚠ 当前环境没有 powershell，请按 docs/INSTALL.md 手动安装进 profile"
  fi
  echo "· 提示   : 安装/升级后需重启 DSH profile 才会注册 memory_* 工具"
fi

if is_windows; then
  OPEN_HINT="在浏览器打开（或 Git Bash 执行 start \"\" $PANEL_URL）"
else
  OPEN_HINT="在浏览器打开（或 xdg-open $PANEL_URL）"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " ✓ 就绪：sidecar pid=$SIDE_PID_DISPLAY"
if [[ "$PANEL_ENABLED" == "true" ]]; then
  echo "   面板    : $PANEL_URL   ($OPEN_HINT)"
fi
echo "   停止    : Ctrl+C 或另开终端执行 ./stop.sh"
echo "   调试    : py -3 scripts/sidecar_ctl.py status | ping | log | call <method>"
echo "   插件本体: 由 DSH 启动时加载；本命令只启动记忆服务与面板"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 前台等待 sidecar；Ctrl+C 由 trap 统一优雅关闭。
set +e
wait "$SIDECAR_PID"
CODE=$?
set -e
STARTED=0
if [[ "$CODE" -ne 0 ]]; then
  echo ""
  echo "⚠ sidecar 已退出（code=$CODE），日志尾部：" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
fi
exit "$CODE"
