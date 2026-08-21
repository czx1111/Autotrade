@echo off
REM ============================================================
REM  TradingAgents 启动脚本 — Web UI (Streamlit)
REM  双击或命令行运行即可启动 Web 控制台
REM ============================================================
cd /d "%~dp0"

REM 激活虚拟环境
call venv\Scripts\activate.bat

REM 设置 UTF-8 编码（解决中文输出问题）
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

REM 启动 Streamlit Web UI
echo ============================================
echo  TradingAgents Web UI 启动中...
echo  浏览器访问: http://localhost:8501
echo  按 Ctrl+C 停止
echo ============================================
streamlit run webui.py --server.port 8501

pause
