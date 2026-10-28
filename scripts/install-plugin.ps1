# PowerShell：把 dsh-graph-memory 安装到指定 DSH profile（默认 web）。
# 注意：本脚本会写 $DSH_HOME/profiles（在 DSH 会话沙箱之外），请在普通 PowerShell
# 终端里手动运行，不要在 DSH agent 的受限沙箱内运行。
#
# 用法：
#   powershell -NoProfile -File scripts/install-plugin.ps1 -Profile web
#   powershell -NoProfile -File scripts/install-plugin.ps1 -Profile headless -PythonPath "py -3"
#
# 步骤：
#   1. 构建插件（pnpm/npm 任选，本仓库用 npm scripts）；
#   2. 把本目录以 link: 依赖安装进 profile（dsh plugin add 转发给 pnpm）；
#   3. 若 profile 未把 bundle 加入 dsh.profile.bundles，用 dsh plugin 自动选择；
#   4. 提示重启（工具注册与 bundle 行在 DSH 启动时生效）。
param(
  [string]$Profile = "web",
  [string]$PythonPath = "",
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Write-Host "[install] repo: $RepoRoot"

if (-not $SkipBuild) {
  Write-Host "[install] building TypeScript -> lib/"
  Push-Location $RepoRoot
  try {
    if (Test-Path "node_modules\typescript") {
      npx tsc -p tsconfig.json
    } else {
      $env:npm_config_cache = Join-Path $RepoRoot ".npm-cache"
      npm install --no-audit --no-fund
      npx tsc -p tsconfig.json
    }
  } finally {
    Pop-Location
  }
}

Write-Host "[install] adding link dependency to profile '$Profile' ..."
dsh plugin --profile $Profile add "link:$RepoRoot"
if ($LASTEXITCODE -ne 0) {
  throw "dsh plugin add failed (exit $LASTEXITCODE)。可手动在 profile package.json 的 dependencies 加 `"dsh-graph-memory`": `"link:$RepoRoot`"，并把 dsh-graph-memory 追加到 dsh.profile.bundles。"
}

if ($PythonPath) {
  Write-Host "[install] 在 profile 的 cordis.patch.yml 中写入 pythonPath=$PythonPath 覆盖"
  Write-Host "  建议配置（按需手改 profile 的 cordis.patch.yml）："
  Write-Host "  - id: graph-memory"
  Write-Host "    config:"
  Write-Host "      sidecar:"
  Write-Host "        pythonPath: '$PythonPath'"
}

Write-Host ""
Write-Host "[install] 完成。重启当前 DSH profile 后生效；验证："
Write-Host "  - 让模型调用 memory_stats 工具；"
Write-Host "  - 打开本地面板 http://127.0.0.1:9629/ ；"
Write-Host "  - 移除：powershell -NoProfile -File scripts/uninstall-plugin.ps1 -Profile $Profile"
