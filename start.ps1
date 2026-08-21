# ============================================================
#  TradingAgents 启动脚本 — PowerShell 版
# ============================================================
param(
    [Parameter(Position=0)]
    [ValidateSet("webui", "auto", "cli", "once")]
    [string]$Mode = "webui"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# 激活虚拟环境
$venvActivate = Join-Path $ProjectRoot "venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
} else {
    Write-Error "虚拟环境不存在，请先运行: python -m venv venv; pip install .[ui,dev]"
    exit 1
}

# 设置 UTF-8 编码
chcp 65001 > $null 2>&1
$env:PYTHONUTF8 = "1"

Set-Location $ProjectRoot

switch ($Mode) {
    "webui" {
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host " TradingAgents Web UI 启动中..." -ForegroundColor Cyan
        Write-Host " 浏览器访问: http://localhost:8501" -ForegroundColor Green
        Write-Host " 按 Ctrl+C 停止" -ForegroundColor Yellow
        Write-Host "============================================" -ForegroundColor Cyan
        streamlit run webui.py --server.port 8501
    }
    "auto" {
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host " TradingAgents 自动交易守护进程启动中..." -ForegroundColor Cyan
        Write-Host " 模拟盘模式 (paper trading)" -ForegroundColor Green
        Write-Host " 按 Ctrl+C 停止" -ForegroundColor Yellow
        Write-Host "============================================" -ForegroundColor Cyan
        python run_auto.py
    }
    "cli" {
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host " TradingAgents CLI 启动中..." -ForegroundColor Cyan
        Write-Host "============================================" -ForegroundColor Cyan
        tradingagents
    }
    "once" {
        Write-Host "============================================" -ForegroundColor Cyan
        Write-Host " TradingAgents 模拟盘试跑一轮..." -ForegroundColor Cyan
        Write-Host "============================================" -ForegroundColor Cyan
        python run_auto.py --once
    }
}
