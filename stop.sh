#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 停止由 start.sh 启动的 sidecar（优雅 shutdown：flush 图谱/Journal 后退出）。
# 用法：
#   ./stop.sh
#   ./stop.sh --workspace-home /custom/ws/data
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_HOME="$ROOT/.dsh/graph-memory"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace-home) WS_HOME="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if command -v cygpath >/dev/null 2>&1; then
  WS_HOME_NATIVE="$(cygpath -w "$WS_HOME")"
else
  WS_HOME_NATIVE="$WS_HOME"
fi

PY_BIN=""
PY_ARGS=()
for cand in "python3" "python" "py -3"; do
  read -r bin arg rest <<<"$cand"
  cmd=("$bin")
  if [[ -n "${arg:-}" ]]; then cmd+=("$arg"); fi
  if "${cmd[@]}" -c 'import sys' >/dev/null 2>&1; then
    PY_BIN="$bin"
    if [[ -n "${arg:-}" ]]; then PY_ARGS=("$arg"); fi
    break
  fi
done
[[ -n "$PY_BIN" ]] || { echo "未找到可用的 Python" >&2; exit 1; }

if [[ ${#PY_ARGS[@]} -gt 0 ]]; then
  exec "${PY_BIN}" "${PY_ARGS[@]}" "$ROOT/scripts/sidecar_ctl.py" --workspace-home "$WS_HOME_NATIVE" stop
else
  exec "${PY_BIN}" "$ROOT/scripts/sidecar_ctl.py" --workspace-home "$WS_HOME_NATIVE" stop
fi
