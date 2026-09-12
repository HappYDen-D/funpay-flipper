"""
auto_flipper/bot.py — Telegram bot initialization, auto-proxy resolution, and lifecycle runner
"""
import asyncio
import logging
import socket
import sys
from typing import Optional
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from auto_flipper.config import (
    AUTO_DETECT_PROXY,
    BOT_TOKEN,
    DEFAULT_LOCAL_PROXIES,
    TELEGRAM_API_SERVER,
    TELEGRAM_PROXY,
)
from auto_flipper.database import db
from auto_flipper.flipper_engine import flipper_engine
from auto_flipper.handlers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("AutoFlipperBot")


def _is_port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _can_reach_telegram_direct(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("api.telegram.org", 443), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_telegram_session() -> AiohttpSession:
    """
    Intelligently configures an AiohttpSession with proxy support.
    1. Explicit TELEGRAM_PROXY if set.
    2. Direct connection to api.telegram.org if reachable.
    3. Auto-detection of local VPN / proxy ports (v2rayN, Xray, Clash on 10809, 10808, 7890, 2080).
    """
    api_server = None
    if TELEGRAM_API_SERVER:
        api_server = TelegramAPIServer.from_base(TELEGRAM_API_SERVER)
        logger.info(f"Using custom Telegram API server mirror: {TELEGRAM_API_SERVER}")

    # 1. Explicit proxy
    if TELEGRAM_PROXY:
        logger.info(f"Using configured Telegram proxy: {TELEGRAM_PROXY}")
        return AiohttpSession(
            proxy=TELEGRAM_PROXY,
            api=api_server or TelegramAPIServer.from_base("https://api.telegram.org"),
        )

    # 2. Direct connection check
    if _can_reach_telegram_direct(timeout=1.0):
        logger.info("Direct connection to api.telegram.org is available.")
        if api_server:
            return AiohttpSession(api=api_server)
        return AiohttpSession()

    # 3. Auto-detection of local proxy on Windows
    if AUTO_DETECT_PROXY:
        logger.info("Direct connection to api.telegram.org timed out. Scanning for local VPN / proxy ports...")
        for proxy_candidate in DEFAULT_LOCAL_PROXIES:
            parsed = urlparse(proxy_candidate)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port
            if port and _is_port_open(host, port, timeout=0.3):
                logger.info(f"🌐 Found active local proxy: {proxy_candidate}. Using it for Telegram Bot API...")
                return AiohttpSession(
                    proxy=proxy_candidate,
                    api=api_server or TelegramAPIServer.from_base("https://api.telegram.org"),
                )

    logger.warning(
        "⚠️ Direct connection to api.telegram.org timed out and no local proxy responded.\n"
        "If the bot fails with a network timeout, verify your VPN (v2rayN, Clash, Hiddify) is running\n"
        "or set TELEGRAM_PROXY in auto_flipper/config.py."
    )
    if api_server:
        return AiohttpSession(api=api_server)
    return AiohttpSession()


async def setup_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="alerts", description="Включить или отключить уведомления о кандидатах"),
        BotCommand(command="candidates", description="Кандидаты и проверенные расчёты"),
        BotCommand(command="reconciliation", description="Неопределённые платежи"),
        BotCommand(command="capital", description="Стартовый и текущий капитал, доступные деньги"),
        BotCommand(command="seed", description="Записать начальный капитал и подтверждение"),
        BotCommand(command="cash", description="Подтвердить доступные деньги по выписке"),
        BotCommand(command="mode", description="Режим OBSERVE, ASSIST, LIMITED_AUTO или PAUSED"),
        BotCommand(command="review", description="Сохранить доказательства спроса и расчёт маршрута"),
        BotCommand(command="prepare", description="Зарезервировать ручную покупку без оплаты"),
        BotCommand(command="resolve_purchase", description="Сверить фактический результат оплаты"),
        BotCommand(command="asset_intake", description="Подтвердить получение конкретного товара"),
        BotCommand(command="manual_exit", description="Записать передачу товара проверенному покупателю"),
        BotCommand(command="settle", description="Записать подтверждённое поступление денег"),
        BotCommand(command="refund", description="Записать подтверждённый возврат денег"),
        BotCommand(command="expense", description="Учесть расход по конкретной сделке"),
        BotCommand(command="writeoff", description="Учесть окончательную потерю товара"),
        BotCommand(command="deadlines", description="Товары и расчёты дольше 24 или 72 часов"),
        BotCommand(command="intake", description="Подтвердить проверку учётных данных товара"),
        BotCommand(command="publish", description="Опубликовать проверенный товар"),
        BotCommand(command="bind_sale", description="Связать оплаченный заказ с объявлением"),
        BotCommand(command="start", description="Главный дашборд флипера"),
        BotCommand(command="status", description="Статус оборота и авто-выкупа"),
        BotCommand(command="liquidity", description="📊 Меню ликвидности рынка"),
        BotCommand(command="liquidity_top", description="🔝 ТОП-10 ликвидных товаров TF2"),
        BotCommand(command="liquidity_bottom", description="🔻 Наименее ликвидные позиции"),
        BotCommand(command="liquidity_key", description="🔑 Профиль Mann Co. Supply Crate Key"),
        BotCommand(command="liquidity_ticket", description="🎫 Профиль Tour of Duty Ticket"),
        BotCommand(command="accountmarkets", description="Read-only наблюдение рынков игровых аккаунтов"),
        BotCommand(command="pnl", description="📈 Финансовый отчёт P&L и ROI"),
        BotCommand(command="categories", description="📁 Целевые категории снайпинга"),
        BotCommand(command="inventory", description="📦 Склад и активные лоты"),
        BotCommand(command="boost", description="🚀 Поднять лоты в ТОП и Турбо"),
        BotCommand(command="browser", description="🌐 Проверка сессии FunPay и прокси"),
        BotCommand(command="goal", description="🎯 Цель прибыли и прогресс"),
        BotCommand(command="emergency_stop", description="🛑 ЭКСТРЕННЫЙ СТОП (пауза и снятие лотов)"),
        BotCommand(command="resume", description="▶️ Возобновить работу после стопа"),
        BotCommand(command="settings", description="⚙️ Настройки бюджета и параметров"),
        BotCommand(command="help", description="ℹ️ Инструкция по работе"),
    ]
    await bot.set_my_commands(commands)


async def on_startup(bot: Bot):
    logger.info("Initializing Auto-Flipper Bot...")
    db.init_db()
    await setup_bot_commands(bot)

    me = await bot.get_me()
    logger.info(f"Auto-Flipper Bot authorized as @{me.username} (ID: {me.id})")

    # Start background order fulfillment loop
    await flipper_engine.start_background_loops(bot)
    logger.info("FlipperEngine background runner started.")


async def on_shutdown(bot: Bot):
    logger.info("Shutting down Auto-Flipper Bot...")
    await flipper_engine.stop()
    await bot.session.close()
    logger.info("Shutdown complete.")


def create_bot_and_dispatcher(session: Optional[AiohttpSession] = None) -> tuple[Bot, Dispatcher]:
    if session is None:
        session = resolve_telegram_session()

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    router._parent_router = None
    from auto_flipper.assistant_handlers import router as assistant_router
    assistant_router._parent_router = None
    dp.include_router(assistant_router)
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return bot, dp


async def main():
    import os
    token = BOT_TOKEN or os.getenv("FLIPPER_BOT_TOKEN", "").strip()
    if not token or token == "your_bot_token_here":
        logger.error(
            "FLIPPER_BOT_TOKEN is missing or not configured. "
            "Please configure it in .env or as an environment variable."
        )
        return
    bot, dp = create_bot_and_dispatcher()
    logger.info("Starting Auto-Flipper polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        logger.error(
            "\n"
            "❌ ОШИБКА АВТОРИЗАЦИИ TELEGRAM: Неверный или отозванный FLIPPER_BOT_TOKEN!\n"
            "Проверьте правильность токена, полученного от @BotFather, в файле .env или переменных окружения."
        )
    except TelegramNetworkError as e:
        logger.error(
            f"\n"
            f"❌ ОШИБКА СЕТИ TELEGRAM: Не удалось установить соединение с api.telegram.org!\n"
            f"Детали ошибки: {e}\n\n"
            f"💡 КАК ИСПРАВИТЬ:\n"
            f"1. Включите ваш VPN (v2rayN, Clash, Amnezia, Hiddify, Outline).\n"
            f"2. Если VPN уже включён (например v2rayN на порту 10809 или Clash на 7890),\n"
            f"   убедитесь, что прокси-сервер запущен в режиме System Proxy или HTTP/SOCKS.\n"
            f"3. Вы также можете явно указать прокси в файле auto_flipper/config.py:\n"
            f"   TELEGRAM_PROXY = 'http://127.0.0.1:10809'\n"
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Auto-Flipper Bot stopped by user.")
