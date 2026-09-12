"""
auto_flipper/config.py — Configuration and tuning parameters for Auto-Flipper Resale Bot
"""
import os
from pathlib import Path
from typing import Optional

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(dotenv_path: Optional[Path] = None) -> None:
    """
    Load environment variables from a .env file into os.environ.
    Preserves existing os.environ entries.
    Ambient loading of BASE_DIR / .env is suppressed with FLIPPER_IGNORE_DOTENV=true.
    """
    if dotenv_path is None and os.getenv("FLIPPER_IGNORE_DOTENV", "").lower() in ("true", "1", "yes"):
        return
    path = dotenv_path or (BASE_DIR / ".env")
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


load_dotenv()

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
FUNPAY_PROXY = os.getenv("FUNPAY_PROXY", "").strip()

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

# Read-only account-market observer. These nodes are verified public FunPay
# account markets; adding a node here requires separate validation.
ACCOUNT_MARKET_OBSERVER_ENABLED = os.getenv("ACCOUNT_MARKET_OBSERVER_ENABLED", "true").lower() in ("true", "1", "yes")
ACCOUNT_MARKET_SEEDS = {
    "funpay:account:436": {"name": "Brawl Stars", "node_id": 436, "aliases": ("brawl", "bs", "бравл")},
    "funpay:account:147": {"name": "Clash of Clans", "node_id": 147, "aliases": ("coc", "клеш", "кок")},
    "funpay:account:248": {"name": "Fortnite", "node_id": 248, "aliases": ("fortnite", "fn", "фортнайт")},
}
ACCOUNT_CHEAP_RUB_THRESHOLD = float(os.getenv("ACCOUNT_CHEAP_RUB_THRESHOLD", "500"))
MAX_ACTIVE_ACCOUNT_MARKETS = min(2, max(0, int(os.getenv("MAX_ACTIVE_ACCOUNT_MARKETS", "2"))))
ACCOUNT_ACTIVE_POLL_MIN_SECONDS = float(os.getenv("ACCOUNT_ACTIVE_POLL_MIN_SECONDS", "180"))
ACCOUNT_ACTIVE_POLL_MAX_SECONDS = float(os.getenv("ACCOUNT_ACTIVE_POLL_MAX_SECONDS", "300"))
ACCOUNT_BACKGROUND_POLL_MIN_SECONDS = float(os.getenv("ACCOUNT_BACKGROUND_POLL_MIN_SECONDS", "720"))
ACCOUNT_BACKGROUND_POLL_MAX_SECONDS = float(os.getenv("ACCOUNT_BACKGROUND_POLL_MAX_SECONDS", "900"))
ACCOUNT_SAMPLE_INTERVAL_SECONDS = float(os.getenv("ACCOUNT_SAMPLE_INTERVAL_SECONDS", "180"))
ACCOUNT_MIN_ACTIVE_LOTS = int(os.getenv("ACCOUNT_MIN_ACTIVE_LOTS", "100"))
ACCOUNT_MIN_INDEPENDENT_SELLERS = int(os.getenv("ACCOUNT_MIN_INDEPENDENT_SELLERS", "20"))
ACCOUNT_MIN_CHEAP_LOTS = int(os.getenv("ACCOUNT_MIN_CHEAP_LOTS", "10"))
ACCOUNT_MIN_CHEAP_SELLERS = int(os.getenv("ACCOUNT_MIN_CHEAP_SELLERS", "5"))
ACCOUNT_MIN_PARSEABLE_RATIO = float(os.getenv("ACCOUNT_MIN_PARSEABLE_RATIO", "0.35"))
ACCOUNT_MAX_LARGEST_SELLER_SHARE = float(os.getenv("ACCOUNT_MAX_LARGEST_SELLER_SHARE", "0.50"))
ACCOUNT_SELECTION_HYSTERESIS_POINTS = float(os.getenv("ACCOUNT_SELECTION_HYSTERESIS_POINTS", "5"))
ACCOUNT_SELECTION_CONFIRM_SAMPLES = int(os.getenv("ACCOUNT_SELECTION_CONFIRM_SAMPLES", "3"))

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
