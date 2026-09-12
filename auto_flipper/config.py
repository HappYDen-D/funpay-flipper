"""
auto_flipper/config.py — Configuration and tuning parameters for Auto-Flipper Resale Bot
"""
import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Database configuration (Strictly isolated auto_flipper.db)
DB_PATH = os.getenv("FLIPPER_DB_PATH", str(BASE_DIR / "auto_flipper.db"))

# Telegram Bot Token (Dedicated Auto-Flipper Bot)
BOT_TOKEN = os.getenv("FLIPPER_BOT_TOKEN", "")

# Admin IDs for Telegram notifications
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()
]

# FunPay Web Endpoints
FUNPAY_BASE_URL = "https://funpay.com"
FUNPAY_CHATGPT_ACCOUNTS_URL = "https://funpay.com/lots/1355/"
FUNPAY_CHATGPT_SUBSCRIPTIONS_URL = "https://funpay.com/lots/3559/"
FUNPAY_ORDERS_CHECKOUT_URL = "https://funpay.com/orders/checkout/"
FUNPAY_SAVE_OFFER_URL = "https://funpay.com/lots/saveOffer"
FUNPAY_RUNNER_URL = "https://funpay.com/runner/"
FUNPAY_RAISE_URL = "https://funpay.com/lots/raise"
FUNPAY_ORDERS_TRADE_URL = "https://funpay.com/orders/trade"

# FunPay Account Configuration
FUNPAY_GOLDEN_KEY = os.getenv("FUNPAY_GOLDEN_KEY", "")
FUNPAY_LOTS_NODE_ACCOUNTS = 1355
FUNPAY_LOTS_NODE_SUBSCRIPTIONS = 3559

# Arbitrage Financial Model Defaults
ARBITRAGE_DRY_RUN_DEFAULT = os.getenv("FLIPPER_DRY_RUN", "true").lower() in ("true", "1", "yes")
ARBITRAGE_AUTO_BUY_DEFAULT = os.getenv("FLIPPER_AUTO_BUY", "false").lower() in ("true", "1", "yes")
ARBITRAGE_MAX_BUDGET_DEFAULT = float(os.getenv("FLIPPER_MAX_BUDGET", "350.0"))
ARBITRAGE_MIN_PROFIT = float(os.getenv("FLIPPER_MIN_PROFIT", "150.0"))
ARBITRAGE_MIN_MARGIN_PCT = float(os.getenv("FLIPPER_MIN_MARGIN_PCT", "35.0"))
ARBITRAGE_MIN_SELLER_RATING = float(os.getenv("FLIPPER_MIN_SELLER_RATING", "4.8"))
ARBITRAGE_MIN_SELLER_REVIEWS = int(os.getenv("FLIPPER_MIN_SELLER_REVIEWS", "5"))
FUNPAY_DIGITAL_FEE_RATE = 0.12  # 12% FunPay platform fee
ARBITRAGE_MARKUP_DISCOUNT = 0.82  # Target resale price: 82% of clean 1-month market median
ARBITRAGE_PRICE_FLOOR = 450.0  # Minimum resale price floor in RUB
MARKET_BENCHMARK_PRICE = 1099.0  # Clean dynamic median baseline (~1099 ₽)
DEFAULT_PROFIT_GOAL = float(os.getenv("DEFAULT_PROFIT_GOAL", "5000.0"))

# Polling and Timers
NORMAL_POLL_INTERVAL = 25.0
TURBO_POLL_INTERVAL = 6.0
ORDERS_POLL_INTERVAL = 15.0
REQUEST_TIMEOUT = 15.0

# HTTP Headers for FunPay Web Protocol
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Proxy & Network Configuration
TELEGRAM_PROXY = (
    os.getenv("TELEGRAM_PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("https_proxy")
    or os.getenv("ALL_PROXY")
    or os.getenv("all_proxy")
    or None
)
TELEGRAM_API_SERVER = os.getenv("TELEGRAM_API_SERVER", None)
AUTO_DETECT_PROXY = os.getenv("AUTO_DETECT_PROXY", "true").lower() in ("true", "1", "yes")

# Known local proxy candidate ports to avoid WinError 121 timeouts
DEFAULT_LOCAL_PROXIES = [
    "http://127.0.0.1:10809",   # v2rayN / Xray / Hiddify HTTP
    "socks5://127.0.0.1:10808",  # v2rayN / Xray / Hiddify SOCKS5
    "http://127.0.0.1:7890",    # Clash / Clash Verge HTTP
    "socks5://127.0.0.1:7890",   # Clash / Clash Verge SOCKS5
    "http://127.0.0.1:2080",    # NekoRay / NekoBox HTTP
    "socks5://127.0.0.1:2080",   # NekoRay / NekoBox SOCKS5
    "socks5://127.0.0.1:1080",   # Shadowsocks / Outline
]
