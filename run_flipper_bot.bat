@echo off
chcp 65001 > nul
setlocal
title FunPay Flipper
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Local .venv is missing. Follow README.md to install the locked environment.
    exit /b 1
)
if not defined FLIPPER_BOT_TOKEN if not exist ".env" (
    echo Set FLIPPER_BOT_TOKEN in your environment or configure .env before starting the bot.
    echo Copy .env.example to .env and fill in credentials.
    exit /b 1
)
".venv\Scripts\python.exe" start_flipper_bot.py
exit /b %errorlevel%