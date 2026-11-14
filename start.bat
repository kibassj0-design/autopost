@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo First run: installing, this takes a few minutes...
  python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
if not exist .env copy .env.example .env >nul
if not exist config.json copy config.example.json config.json >nul
.venv\Scripts\python -m autopost %*
pause
