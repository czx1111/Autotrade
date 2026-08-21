@echo off
REM ============================================================
REM  TradingAgents 启动脚本 — CLI 交互模式
REM ============================================================
cd /d "%~dp0"

REM 激活虚拟环境
call venv\Scripts\activate.bat

REM 设置 UTF-8 编码
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

REM 启动 CLI
echo ============================================
echo  TradingAgents CLI 启动中...
echo ============================================
tradingagents

pause
