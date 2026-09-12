"""
start_flipper_bot.py — Entrypoint script to run the Auto-Flipper FunPay Resale Bot
"""
import asyncio
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from auto_flipper.config import BOT_TOKEN

if __name__ == "__main__":
    token = BOT_TOKEN or os.getenv("FLIPPER_BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        print("[!] ОШИБКА: FLIPPER_BOT_TOKEN не задан или содержит значение по умолчанию.")
        print("    Укажите токен Telegram-бота в файле .env или переменной окружения.")
        print("    Пример: FLIPPER_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz")
        print("    Шаблон доступен в .env.example")
        sys.exit(1)

    from auto_flipper.bot import main

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[+] Auto-Flipper Bot stopped.")
