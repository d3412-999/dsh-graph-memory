# PowerShell：卸载 dsh-graph-memory（撤销工具注册/提示词/事件监听由 profile 重载完成，
# Sidecar 随插件 dispose 停止；数据文件保留在用户与 workspace 的 .dsh/graph-memory 目录）。
#
# 用法：
#   powershell -NoProfile -File scripts/uninstall-plugin.ps1 -Profile web
param(
  [string]$Profile = "web",
  [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "[uninstall] removing plugin from profile '$Profile' ..."
dsh plugin --profile $Profile remove dsh-graph-memory
if ($LASTEXITCODE -ne 0) {
  Write-Warning "dsh plugin remove 返回 $LASTEXITCODE；请在 profile 的 package.json 中手动移除 dsh-graph-memory 依赖与 dsh.profile.bundles 项。"
}

$UserData = Join-Path $HOME ".dsh\graph-memory"
$WorkspaceData = Join-Path (Get-Location) ".dsh\graph-memory"
Write-Host "[uninstall] 数据默认保留："
Write-Host "  user:      $UserData"
Write-Host "  workspace: $WorkspaceData"

if ($RemoveData) {
  Write-Host "[uninstall] -RemoveData 指定，删除数据目录 ..."
  Remove-Item -Recurse -Force $UserData -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force $WorkspaceData -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "[uninstall] 完成。重启 profile 后插件不再加载；数据文件按上面路径保留（可用回滚/迁移）。"
