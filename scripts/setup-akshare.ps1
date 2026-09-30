# setup-akshare.ps1 — 一键安装 akshare + baostock 免费数据源依赖
#
# 用法:
#   .\scripts\setup-akshare.ps1
#   .\scripts\setup-akshare.ps1 -Mirror "https://pypi.tuna.tsinghua.edu.cn/simple"
#
# 前置: 已用 dev.ps1 启动过一次(后端 .venv 已创建)。
# 详见 docs/akshare-setup.md。

[CmdletBinding()]
param(
    [string]$Mirror = "https://mirrors.cloud.tencent.com/pypi/simple"
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$VenvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host "[setup] 后端 .venv 不存在, 请先在项目根目录运行 .\dev.ps1" -ForegroundColor Red
    exit 1
}

Write-Host "[setup] 安装 akshare + baostock 到后端 venv ..." -ForegroundColor Cyan
uv pip install --python $VenvPython --index-url $Mirror "akshare>=1.12" "baostock>=0.9"
if ($LASTEXITCODE -ne 0) { Write-Host "[setup] 安装失败" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "[setup] 依赖安装完成。接下来:" -ForegroundColor Green
Write-Host "  1. 启动面板: .\dev.ps1"
Write-Host "  2. 设置 → 数据源 Beta → 将 实时/日K/分钟K/复权/盘口/财务 切到 akshare"
Write-Host "  3. 编辑 backend\sync_watchlist_data.py 的股票列表, 运行同步数据:"
Write-Host "     cd backend; .\.venv\Scripts\python.exe sync_watchlist_data.py"
Write-Host "  详见 docs\akshare-setup.md"
