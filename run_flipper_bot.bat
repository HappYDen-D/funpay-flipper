@echo off
chcp 65001 > nul
setlocal
title FunPay Flipper
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Local .venv is missing. Follow README.md to install the locked environment.
    exit /b 1
)
if not defined FLIPPER_BOT_TOKEN (
    echo Set FLIPPER_BOT_TOKEN in your environment before starting the bot.
    exit /b 1
)
".venv\Scripts\python.exe" start_flipper_bot.py
exit /b %errorlevel%