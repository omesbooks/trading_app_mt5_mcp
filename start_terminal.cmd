@echo off
rem MCP Terminal: MT5 charts + trading panel through MT5's MCP server. Opens http://127.0.0.1:8790
rem Uses .venv in this folder if present, else the TradingAgents web app's ..\repo\.venv, else python on PATH.
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if "%PY%"=="python" if exist "%~dp0..\repo\.venv\Scripts\python.exe" set "PY=%~dp0..\repo\.venv\Scripts\python.exe"
start "" http://127.0.0.1:8790
"%PY%" server.py
pause
