"""
start_flipper_bot.py — Entrypoint script to run the Auto-Flipper FunPay Resale Bot
"""
import asyncio
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from auto_flipper.bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[+] Auto-Flipper Bot stopped.")
