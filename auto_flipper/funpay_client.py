"""
auto_flipper/funpay_client.py — FunPay Web Protocol Client (Checkout, Offers, Chat, Runner, Raise)
"""
import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx
from auto_flipper.funpay_transport import proxy_options, response_problem, safe_error

from auto_flipper.config import (
    ARBITRAGE_MAX_BUDGET_DEFAULT,
    DEFAULT_HEADERS,
    FUNPAY_BASE_URL,
    FUNPAY_GOLDEN_KEY,
    FUNPAY_ORDERS_CHECKOUT_URL,
    FUNPAY_ORDERS_TRADE_URL,
    FUNPAY_RAISE_URL,
    FUNPAY_RUNNER_URL,
    FUNPAY_SAVE_OFFER_URL,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger("FunPayClient")


class FunPayClient:
    """
    FunPay client supporting balance checkout, dynamic lot listing,
    buyer order watching, runner message delivery, and lot raising (/boost).
    Supports comprehensive DRY_RUN mode for risk-free simulation.
    """

    def __init__(self, golden_key: Optional[str] = None):
        self.action_authorizer = None
        self.checkout_quote_validator = None
        self.golden_key = golden_key or FUNPAY_GOLDEN_KEY
        self.headers = dict(DEFAULT_HEADERS)
        self.headers["X-Requested-With"] = "XMLHttpRequest"
        self._cached_csrf: Optional[str] = None
        self._csrf_time: float = 0.0
        self._order_node_cache: Dict[str, int] = {}
        self._proxy_url = os.environ.get("FUNPAY_PROXY", "").strip()
        self.last_transport_issue = None

    def _http_client(self, *, follow_redirects=False):
        # Pin one explicit route across account, market, checkout and delivery calls.
        return httpx.AsyncClient(headers=self.headers, cookies=self._get_cookies(),
            timeout=REQUEST_TIMEOUT, follow_redirects=follow_redirects,
            **proxy_options(self._proxy_url))

    def _safe_error(self, error):
        message = safe_error(error, self._proxy_url, self.golden_key)
        self.last_transport_issue = message
        return message

    def _response_problem(self, response):
        problem = response_problem(response)
        self.last_transport_issue = problem
        return problem

    def _require_action(self, action):
        if not self.action_authorizer or not self.action_authorizer(action):
            raise PermissionError("MODE_BLOCKED: " + action)

    def _get_cookies(self) -> dict:
        return {"golden_key": self.golden_key, "cy": "rub", "lang": "ru"}

    @staticmethod
    def _acknowledged(response) -> bool:
        """A successful HTTP transport alone is not an operation acknowledgement."""
        if response.status_code != 200 or response_problem(response):
            return False
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return False
        return (isinstance(payload, dict) and "error" in payload
                and (payload["error"] is False
                     or type(payload["error"]) is int and payload["error"] == 0))

    @staticmethod
    def _live_order_id(order_id: str) -> Optional[str]:
        """Never turn a synthetic ID into a live order or a chat node."""
        if not isinstance(order_id, str):
            return None
        clean = order_id.strip()
        for prefix in ("FP-", "ORD-"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        return clean if re.fullmatch(r"[A-Za-z0-9]+", clean) else None

    @staticmethod
    def _redirect_order_id(redirect: Any) -> Optional[str]:
        if not isinstance(redirect, str):
            return None
        try:
            target = urlsplit(urljoin(FUNPAY_BASE_URL + "/", redirect))
            if (target.scheme != "https" or target.netloc != urlsplit(FUNPAY_BASE_URL).netloc
                    or target.query or target.fragment):
                return None
            match = re.fullmatch(r"/orders/([A-Z0-9]+)/", target.path)
            return match.group(1) if match else None
        except ValueError:
            return None

    @staticmethod
    def parse_cooldown_seconds(msg: str) -> Optional[int]:
        """
        Parses cooldown wait time from FunPay response message into integer seconds.
        Examples:
          'Подождите 3 ч 45 мин' -> 13500 seconds
          'Подождите 1 час 20 минут' -> 4800 seconds
          'через 1 минуту' -> 60 seconds
          'через 15 минут' -> 900 seconds
          'через 2 ч' -> 7200 seconds
          'через 2ч 30м' -> 9000 seconds
          'Подождите 45 сек' -> 45 seconds
          'через 1 секунду' -> 1 second
          'через 1 день 2 часа' -> 93600 seconds
          'Wait 1 hour 30 minutes' -> 5400 seconds
        Returns None if no cooldown wait time could be parsed.
        """
        if not msg:
            return None

        msg_clean = msg.lower().replace("\xa0", " ").strip()
        cooldown_markers = ["подождите", "через", "wait", "доступно", "позже", "секунд", "минут", "час", "сек", "мин", "день", "дня", "дней", "д."]
        if not any(k in msg_clean for k in cooldown_markers):
            return None

        days = 0
        hours = 0
        minutes = 0
        seconds = 0
        found = False

        # 0. Days: e.g. "1 день", "2 дня", "5 дней", "1 day", "2 days", "1 d", "1д"
        m_d = re.search(r'(\d+)\s*(?:д(?:н(?:[яей]+)|ен[ья])?|[dд](?:ays?)?)\b', msg_clean)
        if m_d:
            days = int(m_d.group(1))
            found = True

        # 1. Hours: e.g. "3 ч", "3 часа", "1 час", "5 часов", "2 hours", "2 hour", "2 h", "2ч"
        m_h = re.search(r'(\d+)\s*(?:ч(?:ас(?:[а-я]*)?)?|[hх](?:ours?|r)?)\b', msg_clean)
        if m_h:
            hours = int(m_h.group(1))
            found = True

        # 2. Minutes: e.g. "45 мин", "45 минут", "1 минуту", "2 минуты", "30 min", "30 m", "30м"
        m_m = re.search(r'(\d+)\s*(?:мин(?:ут[а-я]*)?|[mм](?:in(?:ute)?s?)?)\b', msg_clean)
        if m_m:
            minutes = int(m_m.group(1))
            found = True

        # 3. Seconds: e.g. "30 сек", "30 секунд", "1 секунду", "30 seconds", "30 s", "30с"
        m_s = re.search(r'(\d+)\s*(?:сек(?:унд[а-я]*)?|[sс](?:ec(?:ond)?s?)?)\b', msg_clean)
        if m_s:
            seconds = int(m_s.group(1))
            found = True

        if not found:
            return None

        return days * 86400 + hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def parse_balances(html_content: str) -> Dict[str, float]:
        """
        Parses available balance, hold balance (48-hour delay), and total balance from FunPay HTML.
        Returns:
            {
                "balance_available": float,
                "balance_hold": float,
                "balance_total": float,
            }
        """
        if not html_content:
            return {"balance_available": 0.0, "balance_hold": 0.0, "balance_total": 0.0}

        avail_val: Optional[float] = None
        hold_val: Optional[float] = None
        total_val: Optional[float] = None

        # 1. Data attributes in HTML
        m_attr_avail = re.search(r'data-(?:balance-available|available-balance|balance-avail)=["\']([0-9\s.,]+)["\']', html_content, re.I)
        if m_attr_avail:
            avail_val = FunPayClient._parse_price_value(m_attr_avail.group(1))

        m_attr_hold = re.search(r'data-(?:balance-hold|hold-balance|balance-locked)=["\']([0-9\s.,]+)["\']', html_content, re.I)
        if m_attr_hold:
            hold_val = FunPayClient._parse_price_value(m_attr_hold.group(1))

        m_attr_total = re.search(r'data-(?:balance-total|total-balance)=["\']([0-9\s.,]+)["\']', html_content, re.I)
        if m_attr_total:
            total_val = FunPayClient._parse_price_value(m_attr_total.group(1))

        # 2. Hold amount in text (prioritize currency marker to avoid matching 'холд 48 часов')
        if hold_val is None:
            m_hold = re.search(
                r'(?:\bв\s*холде|\bхолд[а-я]*|\bудержан[а-я]*|\bзаблокирован[а-я]*|\bhold\b)[^0-9<>]*?([0-9]+(?:[\s.,][0-9]+)*)\s*(?:₽|руб|RUB)',
                html_content,
                re.IGNORECASE,
            )
            if not m_hold:
                m_hold = re.search(
                    r'(?:class=["\'][^"\']*hold[^"\']*["\'][^>]*>)[^0-9<>]*?([0-9]+(?:[\s.,][0-9]+)*)',
                    html_content,
                    re.IGNORECASE,
                )
            if m_hold:
                hold_val = FunPayClient._parse_price_value(m_hold.group(1))

        # 3. Available amount in text
        if avail_val is None:
            m_avail = re.search(
                r'(?:\bдоступно|\bдоступный\s+баланс|\bavailable\b)[^0-9<>]*?([0-9]+(?:[\s.,][0-9]+)*)\s*(?:₽|руб|RUB)',
                html_content,
                re.IGNORECASE,
            )
            if not m_avail:
                m_avail = re.search(
                    r'(?:\bдоступно|\bдоступный\s+баланс|\bavailable\b)[^0-9<>]*?([0-9]+(?:[\s.,][0-9]+)*)',
                    html_content,
                    re.IGNORECASE,
                )
            if m_avail:
                avail_val = FunPayClient._parse_price_value(m_avail.group(1))

        # 4. General / navbar balance
        bal_general: float = 0.0
        bal_match = re.search(
            r'(?:badge-balance|user-balance|navbar-balance|class=["\'][^"\']*balance[^"\']*["\'])[^>]*>([0-9\s.,]+)\s*(?:₽|руб|RUB)',
            html_content,
            re.IGNORECASE,
        )
        if bal_match:
            bal_general = FunPayClient._parse_price_value(bal_match.group(1))

        hold = hold_val if hold_val is not None else 0.0

        if avail_val is not None:
            available = avail_val
            total = total_val if total_val is not None else round(available + hold, 2)
        elif total_val is not None:
            total = total_val
            available = max(0.0, round(total - hold, 2))
        elif bal_general > 0:
            if hold > 0:
                if bal_general >= hold:
                    total = bal_general
                    available = round(bal_general - hold, 2)
                else:
                    available = bal_general
                    total = round(bal_general + hold, 2)
            else:
                available = bal_general
                total = bal_general
        else:
            available = 0.0
            total = hold

        return {
            "balance_available": round(available, 2),
            "balance_hold": round(hold, 2),
            "balance_total": round(total, 2),
        }

    @staticmethod
    def _extract_csrf_token(html_text: str) -> str:
        """Extracts CSRF token from HTML or data-app-data attribute."""
        if not html_text:
            return ""

        app_data_match = re.search(r'data-app-data=["\']([^"\']+)["\']', html_text)
        if app_data_match:
            try:
                data = json.loads(html.unescape(app_data_match.group(1)))
                if "csrf-token" in data:
                    return str(data["csrf-token"])
                if "csrf" in data:
                    return str(data["csrf"])
            except Exception:
                pass

        patterns = [
            r'["\']csrf-token["\']\s*:\s*["\']([^"\']+)["\']',
            r'["\']csrf["\']\s*:\s*["\']([^"\']+)["\']',
            r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
            r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']',
        ]
        for p in patterns:
            m = re.search(p, html_text)
            if m:
                return m.group(1).strip()
        return ""

    async def get_csrf_token(self, client: Optional[httpx.AsyncClient] = None) -> str:
        """Fetches and caches active CSRF token for FunPay operations."""
        if self._cached_csrf and (time.time() - self._csrf_time) < 1800:
            return self._cached_csrf

        close_client = False
        try:
            if client is None:
                client = self._http_client()
                close_client = True
            resp = await client.get(f"{FUNPAY_BASE_URL}/")
            problem = self._response_problem(resp)
            token = self._extract_csrf_token(resp.text) if resp.status_code == 200 and not problem else ""
            if token:
                self._cached_csrf = token
                self._csrf_time = time.time()
                return token
        except Exception as e:
            logger.debug(f"Error fetching FunPay CSRF token: {self._safe_error(e)}")
        finally:
            if close_client:
                await client.aclose()

        return ""

    async def get_account_info(self) -> Dict[str, Any]:
        """
        Scrapes FunPay homepage with golden_key session to inspect user profile,
        balance in RUB (separated available and 48h hold), active username, user ID, rating,
        and Cloudflare/session health.
        Used by /browser and /status commands.
        """
        if not self.golden_key or self.golden_key == "YOUR_GOLDEN_KEY_HERE":
            return {
                "is_authenticated": False,
                "session_status": "no_key",
                "username": "Не авторизован",
                "balance_rub": 0.0,
                "balance_available": 0.0,
                "balance_hold": 0.0,
                "balance_total": 0.0,
                "rating": 0.0,
                "active_lots_count": 0,
                "has_golden_key": False,
                "error": "golden_key не указан",
                "latency_ms": 0.0,
            }

        start_time = time.time()
        try:
            async with self._http_client() as client:
                resp = await client.get(f"{FUNPAY_BASE_URL}/")
                latency_ms = round((time.time() - start_time) * 1000, 1)

                html_content = resp.text

                # A forbidden response alone does not establish a TLS fingerprint cause.
                problem = self._response_problem(resp)
                if problem == 'CLOUDFLARE_CHALLENGE':
                    return {
                        "is_authenticated": False,
                        "session_status": "cloudflare",
                        "username": "Заблокировано Cloudflare",
                        "balance_rub": 0.0,
                        "balance_available": 0.0,
                        "balance_hold": 0.0,
                        "balance_total": 0.0,
                        "rating": 0.0,
                        "active_lots_count": 0,
                        "latency_ms": latency_ms,
                        "status_code": resp.status_code,
                        "has_golden_key": True,
                        "error": problem,
                    }

                if resp.status_code != 200:
                    return {
                        "is_authenticated": False,
                        "session_status": "error",
                        "username": "Ошибка сети",
                        "balance_rub": 0.0,
                        "balance_available": 0.0,
                        "balance_hold": 0.0,
                        "balance_total": 0.0,
                        "rating": 0.0,
                        "active_lots_count": 0,
                        "status_code": resp.status_code,
                        "latency_ms": latency_ms,
                        "has_golden_key": True,
                        "error": problem or f"HTTP {resp.status_code}",
                    }

                # 2. Check if logged in: look for user-name or data-app-data
                user_match = re.search(r'class=["\']user-link-name["\'][^>]*>([^<]+)</span>', html_content)
                username = user_match.group(1).strip() if user_match else ""

                if not username:
                    user_match2 = re.search(r'data-user=["\']\{[^}]*["\']name["\']:\s*["\']([^"\']+)["\']', html_content)
                    username = user_match2.group(1).strip() if user_match2 else ""

                if not username:
                    return {
                        "is_authenticated": False,
                        "session_status": "expired",
                        "username": "Сессия истекла",
                        "balance_rub": 0.0,
                        "balance_available": 0.0,
                        "balance_hold": 0.0,
                        "balance_total": 0.0,
                        "rating": 0.0,
                        "active_lots_count": 0,
                        "latency_ms": latency_ms,
                        "status_code": resp.status_code,
                        "has_golden_key": True,
                        "error": "Сессия истекла (golden_key недействителен)",
                    }

                # 3. Parse balances (distinguishing available, 48h hold, total)
                balances = self.parse_balances(html_content)

                # 4. Scrape rating
                rating_match = re.search(r'class=["\']rating["\'][^>]*>.*?([0-9]\.[0-9])', html_content)
                rating = float(rating_match.group(1)) if rating_match else 5.0

                return {
                    "is_authenticated": True,
                    "session_status": "valid",
                    "username": username,
                    "balance_rub": balances["balance_available"],
                    "balance_available": balances["balance_available"],
                    "balance_hold": balances["balance_hold"],
                    "balance_total": balances["balance_total"],
                    "rating": rating,
                    "latency_ms": latency_ms,
                    "status_code": resp.status_code,
                    "has_golden_key": True,
                }
        except Exception as e:
            return {
                "is_authenticated": False,
                "session_status": "error",
                "username": "Ошибка",
                "balance_rub": 0.0,
                "balance_available": 0.0,
                "balance_hold": 0.0,
                "balance_total": 0.0,
                "rating": 0.0,
                "active_lots_count": 0,
                "error": self._safe_error(e),
                "has_golden_key": True,
                "latency_ms": round((time.time() - start_time) * 1000, 1),
            }

    async def get_balance(self, fallback: float = 0.0) -> float:
        """Helper to get current available RUB balance. Returns 0.0 if unauthenticated (no phantom money)."""
        info = await self.get_account_info()
        if info.get("is_authenticated"):
            return float(info.get("balance_available", info.get("balance_rub", 0.0)))
        return 0.0

    async def checkout_lot(self, lot_id: str, price: float, dry_run: bool = True, *, preflight=None, expected_sku=None) -> Dict[str, Any]:
        """
        Executes order checkout using FunPay balance.
        In DRY_RUN mode: generates simulated order ID and records execution.
        If a network error occurs after dispatching the POST request, marks outcome as UNKNOWN
        to enforce external reconciliation before retrying.
        """
        if not isinstance(lot_id, str):
            return {"success": False, "dry_run": dry_run, "status": "FAILED", "error": "INVALID_LOT_ID"}
        raw_lot_num = lot_id.removeprefix("funpay_")
        from auto_flipper.economics import kopecks
        try:
            valid_price = not isinstance(price, bool) and kopecks(price) > 0
        except (ValueError, TypeError, ArithmeticError):
            valid_price = False
        if not raw_lot_num.isascii() or not raw_lot_num.isdigit() or int(raw_lot_num) <= 0 or not valid_price:
            return {"success": False, "dry_run": dry_run, "status": "FAILED", "error": "INVALID_CHECKOUT_INPUT"}

        if dry_run:
            sim_order_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"
            logger.info(f"[DRY_RUN] Simulated balance checkout for lot #{raw_lot_num} at {price} RUB. Order: {sim_order_id}")
            return {
                "success": True,
                "dry_run": True,
                "status": "EXECUTED",
                "order_id": sim_order_id,
                "lot_id": lot_id,
                "price": price,
                "message": f"Симуляция выкупа успешна (Лот #{raw_lot_num}, {price} ₽)",
            }

        if not self.golden_key:
            return {"success": False, "dry_run": False, "status": "FAILED", "error": "golden_key не настроен"}

        request_sent = False
        try:
            async with self._http_client() as client:
                offer_url = f"{FUNPAY_BASE_URL}/lots/offer?id={raw_lot_num}"
                resp = await client.get(offer_url)
                problem = self._response_problem(resp)
                if problem or resp.status_code != 200:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": problem or f"Ошибка страницы лота: HTTP {resp.status_code}"}

                csrf_token = self._extract_csrf_token(resp.text) or await self.get_csrf_token(client)
                if not csrf_token:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "Не удалось получить CSRF токен FunPay"}

                payload = {
                    "id": raw_lot_num,
                    "payment_method": "balance",
                    "amount": 1,
                    "csrf_token": csrf_token,
                }
                self._require_action('checkout')
                # The listing price is not the balance debit. Fail closed until an
                # adapter can verify the exact checkout quote from a captured fixture.
                if self.checkout_quote_validator is None:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CHECKOUT_QUOTE_UNVERIFIED"}
                quote = self.checkout_quote_validator(resp.text, raw_lot_num)
                if (not isinstance(quote, dict) or quote.get('currency') != 'RUB'
                        or isinstance(quote.get('debit'), bool)
                        or kopecks(quote.get('debit')) != kopecks(price)):
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CHECKOUT_PRICE_CHANGED_OR_UNKNOWN"}
                if expected_sku is not None and (quote.get('sku') != expected_sku
                        or quote.get('lot_id') != raw_lot_num or quote.get('quantity') != 1
                        or type(quote.get('quantity')) is not int):
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CHECKOUT_PRODUCT_CHANGED_OR_UNKNOWN"}
                if preflight is not None:
                    preflight()
                self._require_action('checkout')
                request_sent = True
                checkout_resp = await client.post(FUNPAY_ORDERS_CHECKOUT_URL, data=payload)
                problem = self._response_problem(checkout_resp)
                if problem:
                    return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": problem}

                order_id = (self._redirect_order_id(checkout_resp.headers.get("Location"))
                            if checkout_resp.status_code in (302, 303) else None)
                if order_id:
                    return {"success": True, "dry_run": False, "status": "EXECUTED", "order_id": order_id, "price": price}

                try:
                    res_json = checkout_resp.json()
                    if self._acknowledged(checkout_resp):
                        order_id = self._redirect_order_id(res_json.get("redirect"))
                        if order_id:
                            return {"success": True, "dry_run": False, "status": "EXECUTED", "order_id": order_id, "price": price}
                    if isinstance(res_json, dict) and res_json.get("msg"):
                        return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": res_json["msg"]}
                except Exception:
                    pass

                err_match = re.search(r'class=["\'][^"\']*alert-(?:danger|warning)[^"\']*["\'][^>]*>([^<]+)', checkout_resp.text)
                err_msg = err_match.group(1).strip() if err_match else f"Чекаут отклонен (HTTP {checkout_resp.status_code})"
                return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": err_msg}
        except Exception as e:
            logger.error(f"Live checkout error for lot {lot_id} (request_sent={request_sent}): {self._safe_error(e)}")
            if request_sent:
                return {
                    "success": False,
                    "dry_run": False,
                    "status": "UNKNOWN",
                    "error": f"UNKNOWN: Checkout request sent but network error occurred: {self._safe_error(e)}. External reconciliation required before retrying.",
                }
            return {"success": False, "dry_run": False, "status": "FAILED", "error": self._safe_error(e)}

    async def save_offer(
        self,
        node_id: int = 1355,
        title: str = "",
        desc: str = "",
        price: float = 0.0,
        offer_id: Optional[str] = None,
        active: bool = True,
        custom_fields: Optional[Dict[str, str]] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Lists or relists an offer on FunPay via POST /lots/saveOffer.
        Can activate or deactivate (active=False) for emergency stop.
        Supports passing custom_fields (e.g. fields[type], fields[method], etc.).
        """
        if dry_run:
            sim_id = offer_id or f"SIM-LOT-{uuid.uuid4().hex[:8].upper()}"
            status_word = "АКТИВИРОВАН" if active else "ДЕАКТИВИРОВАН"
            logger.info(f"[DRY_RUN] Offer {sim_id} {status_word} at {price} RUB (node {node_id}, custom_fields: {custom_fields})")
            return {
                "success": True,
                "dry_run": True,
                "offer_id": sim_id,
                "active": active,
                "price": price,
                "title": title,
                "custom_fields": custom_fields or {},
                "node_id": node_id,
            }

        if not self.golden_key:
            return {"success": False, "dry_run": False, "error": "golden_key не настроен"}

        if offer_id is not None and not re.fullmatch(r"[0-9]+", str(offer_id)):
            return {"success": False, "dry_run": False, "status": "FAILED", "error": "INVALID_LIVE_OFFER_ID"}
        if custom_fields and any(not re.fullmatch(r"fields\[[A-Za-z0-9_-]+\]", key) for key in custom_fields):
            return {"success": False, "dry_run": False, "status": "FAILED", "error": "UNSAFE_CUSTOM_FIELD"}
        request_sent = False
        try:
            async with self._http_client() as client:
                csrf_token = await self.get_csrf_token(client)
                if not csrf_token:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CSRF_UNVERIFIED"}
                data = {
                    "node_id": str(node_id),
                    "offer_id": offer_id or "0",
                    "fields[summary][ru]": title,
                    "fields[desc][ru]": desc,
                    "price": str(price),
                    "amount": "1",
                    "active": "on" if active else "",
                    "deactivate_after_sale": "on",
                    "csrf_token": csrf_token,
                }
                if custom_fields:
                    data.update(custom_fields)
                self._require_action('publish' if active else 'deactivate')
                request_sent = True
                resp = await client.post(FUNPAY_SAVE_OFFER_URL, data=data)
                problem = self._response_problem(resp)
                if problem:
                    return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": problem}
                if self._acknowledged(resp):
                    new_id = offer_id
                    if not new_id or new_id == "0":
                        payload = resp.json()
                        candidate_id = payload.get("offer_id", payload.get("id"))
                        new_id = (str(candidate_id) if type(candidate_id) in (int, str)
                                  and re.fullmatch(r"[1-9][0-9]*", str(candidate_id)) else None)
                    if not new_id:
                        return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": "OFFER_ID_UNVERIFIED"}
                    return {"success": True, "dry_run": False, "status": "EXECUTED", "offer_id": str(new_id), "active": active, "custom_fields": custom_fields}
                return {"success": False, "dry_run": False, "status": "UNKNOWN", "error": f"OFFER_ACK_UNVERIFIED: HTTP {resp.status_code}"}
        except Exception as e:
            return {"success": False, "dry_run": False, "status": "UNKNOWN" if request_sent else "FAILED", "error": self._safe_error(e)}

    async def deactivate_offer(self, offer_id: str, node_id: int = 1355, dry_run: bool = True) -> bool:
        """Deactivates offer for emergency kill-switch or post-sale stock control."""
        res = await self.save_offer(node_id=node_id, offer_id=offer_id, active=False, dry_run=dry_run)
        return bool(res.get("success", False))

    @staticmethod
    def extract_chat_node_id(html_text: str) -> Optional[int]:
        """Extracts numeric chat node ID from FunPay order page HTML."""
        if not html_text:
            return None
        patterns = [
            r'data-node=["\']?(\d+)["\']?',
            r'data-chat-node=["\']?(\d+)["\']?',
            r'data-node-id=["\']?(\d+)["\']?',
            r'<input[^>]*name=["\']node["\'][^>]*value=["\'](\d+)["\']',
            r'<input[^>]*value=["\'](\d+)["\'][^>]*name=["\']node["\']',
            r'<[^>]*class=["\'][^"\']*chat[^"\']*["\'][^>]*data-id=["\'](\d+)["\']',
            r'<[^>]*data-id=["\'](\d+)["\'][^>]*class=["\'][^"\']*chat[^"\']*["\']',
            r'<[^>]*class=["\'][^"\']*chat-form[^"\']*["\'][^>]*data-node=["\'](\d+)["\']',
            r'<[^>]*data-node=["\'](\d+)["\'][^>]*class=["\'][^"\']*chat-form[^"\']*["\']',
            r'["\']node(?:_id|Id)?["\']\s*:\s*["\']?(\d+)["\']?',
            r'chat(?:Node|_node)?\s*[:=]\s*["\']?(\d+)["\']?',
        ]
        nodes = {int(match.group(1)) for pattern in patterns
                 for match in re.finditer(pattern, html_text, re.IGNORECASE)
                 if int(match.group(1)) > 0}
        # A page can contain category nodes as well as chat nodes. Ambiguity must
        # not send a buyer's credentials into whichever node happened to be first.
        return next(iter(nodes)) if len(nodes) == 1 else None

    async def resolve_chat_node_id(self, order_id: str, client: Optional[httpx.AsyncClient] = None) -> Optional[int]:
        """
        Resolves numeric chat_node_id from order HTML.
        Caches resolved node_id for subsequent message delivery.
        """
        clean_ord = self._live_order_id(order_id)
        if not clean_ord:
            return None

        if clean_ord in self._order_node_cache:
            return self._order_node_cache[clean_ord]

        close_client = False
        try:
            if client is None:
                client = self._http_client()
                close_client = True
            url = f"{FUNPAY_BASE_URL}/orders/{clean_ord}/"
            resp = await client.get(url)
            problem = self._response_problem(resp)
            if resp.status_code == 200 and not problem:
                node_id = self.extract_chat_node_id(resp.text)
                if node_id:
                    self._order_node_cache[clean_ord] = node_id
                    return node_id
        except Exception as e:
            logger.debug(f"Error resolving chat node for order {order_id}: {self._safe_error(e)}")
        finally:
            if close_client:
                await client.aclose()

        return None

    async def send_chat_message(
        self,
        order_id: str,
        message: str,
        node_id: Optional[int] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Sends order delivery credentials to buyer via FunPay order chat.
        Resolves numeric chat_node_id from order HTML if node_id is not passed.
        """
        if dry_run:
            sim_node = node_id or 123456
            logger.info(f"[DRY_RUN] Delivered credentials to order #{order_id} (node: {sim_node}, chars={len(message)})")
            return {"success": True, "dry_run": True, "order_id": order_id, "node_id": sim_node}

        if not self.golden_key:
            return {"success": False, "dry_run": False, "error": "golden_key не настроен"}

        if not self._live_order_id(order_id):
            return {"success": False, "dry_run": False, "status": "FAILED", "error": "INVALID_LIVE_ORDER_ID"}
        request_sent = False
        try:
            async with self._http_client() as client:
                csrf_token = await self.get_csrf_token(client)
                if not csrf_token:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CSRF_UNVERIFIED"}

                target_node = await self.resolve_chat_node_id(order_id, client=client)

                if target_node is None or node_id is not None and node_id != target_node:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CHAT_NODE_UNKNOWN_OR_MISMATCH"}
                clean_node = str(target_node)

                data = {
                    "action": "chat_message",
                    "node": clean_node,
                    "content": message,
                    "csrf_token": csrf_token,
                }
                self._require_action('deliver')
                request_sent = True
                resp = await client.post(FUNPAY_RUNNER_URL, data=data)
                problem = self._response_problem(resp)
                confirmed = self._acknowledged(resp)
                return {
                    "success": confirmed,
                    "status": "DELIVERED" if confirmed else "UNKNOWN",
                    "dry_run": False,
                    "node_id": clean_node,
                    "order_id": order_id,
                    "error": problem if not confirmed else None,
                }
        except Exception as e:
            return {"success": False, "dry_run": False, "status": "UNKNOWN" if request_sent else "FAILED", "error": self._safe_error(e)}

    async def fetch_order_chat(self, order_id: str) -> List[str]:
        """Fetches incoming seller chat messages from FunPay order page."""
        if not self.golden_key or self.golden_key == "YOUR_GOLDEN_KEY_HERE":
            return []

        clean_ord = self._live_order_id(order_id)
        if not clean_ord:
            return []
        url = f"{FUNPAY_BASE_URL}/orders/{clean_ord}/"
        try:
            async with self._http_client() as client:
                resp = await client.get(url)
                problem = self._response_problem(resp)
                if resp.status_code == 200 and not problem:
                    msg_matches = re.findall(
                        r'class=["\']chat-msg-item(?!\s+chat-msg-outgoing)[^"\']*["\'].*?class=["\']chat-msg-text["\'][^>]*>(.*?)</div>',
                        resp.text,
                        re.DOTALL,
                    )
                    clean_msgs = []
                    for m in msg_matches:
                        text = re.sub(r'<br\s*/?>', '\n', m)
                        text = re.sub(r'<[^>]+>', '', text).strip()
                        if text:
                            clean_msgs.append(text)
                    return clean_msgs
        except Exception as e:
            logger.debug(f"Error fetching order chat for {order_id}: {self._safe_error(e)}")
        return []

    async def fetch_incoming_orders(self) -> List[Dict[str, Any]]:
        """Scrapes https://funpay.com/orders/trade to check for newly paid buyer orders."""
        if not self.golden_key or self.golden_key == "YOUR_GOLDEN_KEY_HERE":
            return []

        try:
            async with self._http_client() as client:
                resp = await client.get(FUNPAY_ORDERS_TRADE_URL)
                problem = self._response_problem(resp)
                if resp.status_code == 200 and not problem:
                    orders = []
                    order_rows = re.findall(
                        r'<a\b[^>]*href=["\'](?:https://funpay\.com)?/orders/([A-Z0-9]+)/["\'][^>]*>(.*?)</a>',
                        resp.text, re.DOTALL)
                    for ord_id, row_html in order_rows:
                        # Missing fields on one row must never borrow a paid
                        # status or buyer identity from the next order.
                        details = re.search(
                            r'class=["\']tc-desc-text["\'][^>]*>(.*?)</div>.*?'
                            r'class=["\']tc-user["\'][^>]*>.*?class=["\']media-user-name["\'][^>]*>([^<]+)</span>.*?'
                            r'class=["\']tc-status[^"\']*["\'][^>]*>(.*?)</div>',
                            row_html, re.DOTALL)
                        if not details:
                            continue
                        desc, buyer, status_text = details.groups()
                        status_clean = re.sub(r'<[^>]+>', '', status_text).strip()
                        orders.append({
                            "order_id": ord_id,
                            "desc": desc.strip(),
                            "title": desc.strip(),
                            "buyer": buyer.strip(),
                            "status": status_clean,
                            "is_paid": " ".join(html.unescape(status_clean).lower().split()) in ("оплачен", "оплачено", "paid"),
                        })
                    return orders
        except Exception as e:
            logger.debug(f"Error checking incoming buyer orders: {self._safe_error(e)}")
        return []

    async def raise_lots(self, node_ids: Optional[List[int]] = None, dry_run: bool = True) -> Dict[str, Any]:
        """
        Executes FunPay lot raise (Bump to top / Boost) via POST /lots/raise.
        Target nodes: 1355 (ChatGPT Accounts), 3559 (ChatGPT Subscriptions).
        Parses cooldown duration into seconds if lots cannot yet be raised.
        """
        if node_ids is None:
            from auto_flipper.categories import get_all_target_node_ids
            node_ids = get_all_target_node_ids()

        if dry_run:
            logger.info(f"[DRY_RUN] Simulated /boost: raised lots in nodes {node_ids}")
            return {
                "success": True,
                "dry_run": True,
                "raised_nodes": node_ids,
                "cooldown": False,
                "cooldown_seconds": 0,
                "message": f"Симуляция буста успешна: подняты лоты в категориях {node_ids}",
                "results": [{"node_id": nid, "status_code": 200, "msg": "Лоты подняты", "cooldown": False, "cooldown_seconds": 0} for nid in node_ids],
            }

        if not self.golden_key:
            return {"success": False, "dry_run": False, "error": "golden_key не настроен"}

        results = []
        try:
            async with self._http_client() as client:
                csrf_token = await self.get_csrf_token(client)
                if not csrf_token:
                    return {"success": False, "dry_run": False, "status": "FAILED", "error": "CSRF_UNVERIFIED"}
                for node_id in node_ids:
                    data = {
                        "game_id": "",
                        "node_id": str(node_id),
                        "csrf_token": csrf_token,
                    }
                    self._require_action('boost')
                    resp = await client.post(FUNPAY_RAISE_URL, data=data)
                    problem = self._response_problem(resp)
                    msg = problem or "BOOST_ACK_UNVERIFIED"
                    acknowledged = self._acknowledged(resp)
                    cooldown = False
                    cooldown_seconds: Optional[int] = None
                    if resp.status_code == 200:
                        try:
                            rj = resp.json()
                            if rj.get("msg"):
                                msg = rj["msg"]
                            is_err = bool(rj.get("error"))
                            cd_sec = self.parse_cooldown_seconds(msg)
                            is_cd_msg = any(w in msg.lower() for w in ["подождите", "wait", "слишком часто", "нельзя", "ошибка", "запрещено"])
                            is_success_msg = any(w in msg.lower() for w in ["подняты", "успешно", "raised", "success"])

                            if is_err or is_cd_msg or (cd_sec is not None and not is_success_msg):
                                cooldown = True
                                cooldown_seconds = cd_sec if cd_sec else 14400
                            else:
                                cooldown = False
                                cooldown_seconds = cd_sec if cd_sec else 0
                        except Exception:
                            pass
                    else:
                        msg = f"HTTP {resp.status_code}"
                    results.append({
                        "node_id": node_id,
                        "success": acknowledged and not cooldown,
                        "status_code": resp.status_code,
                        "msg": msg,
                        "cooldown": cooldown,
                        "cooldown_seconds": cooldown_seconds,
                    })
                    if problem:
                        break  # Do not repeat blocked mutations for the other categories.

            max_cd = 0
            for r in results:
                if r.get("cooldown_seconds"):
                    max_cd = max(max_cd, r["cooldown_seconds"])

            is_cd = any(r["cooldown"] for r in results)
            success = any(r["success"] for r in results)

            return {
                "success": success,
                "dry_run": False,
                "cooldown": is_cd,
                "cooldown_seconds": max_cd if max_cd > 0 else (14400 if is_cd else 0),
                "raised_nodes": [r["node_id"] for r in results if r["success"]],
                "results": results,
            }
        except Exception as e:
            return {"success": False, "dry_run": False, "error": self._safe_error(e)}

    # ─────────────────────────────────────────────────────────────
    # Market Lots Fetching & Parsing for Autonomous Auto-Buying
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_price_value(raw: str) -> float:
        if not raw:
            return 0.0
        s = raw.replace('\xa0', ' ').strip().replace(' ', '')
        if '.' in s and ',' in s:
            if s.rfind('.') > s.rfind(','):
                s = s.replace(',', '')
            else:
                s = s.replace('.', '').replace(',', '.')
        elif ',' in s:
            s = s.replace(',', '.')
        try:
            return float(s)
        except ValueError:
            return 0.0

    @staticmethod
    def _is_plus_lot(title: str, f_type: str = "", f_sub: str = "") -> bool:
        t = title.lower()
        if "go" in f_type.lower() or "без подписки" in t or "без подписки" in f_sub.lower():
            return False
        if any(w in t for w in ["только api", "api ключ", "пустой аккаунт", "без plus"]):
            return False
        return any(p in t for p in ["plus", "плюс", "gpt 4", "gpt-4", "gpt4", "gpt-5", "gpt5", "4o"]) or "plus" in f_type.lower()

    @staticmethod
    def _is_personal_lot(title: str) -> bool:
        t = title.lower()
        # Non-account / service / rental blacklist
        shared_keywords = [
            "общий", "shared", "слот", "slot", "инвайт", "invite", "тима", "team",
            "workspace", "семейный", "аренда", "прокат", "карпулинг", "соседи", "пул",
            "1 час", "2 часа", "3 часа", "на час", "на сутки", "1 день", "2 дня", "3 дня",
            "на 1 день", "на 2 дня", "на 3 дня", "на 7 дней", "вход по коду", "без смены",
            "без почты", "не личный", "продление на ваш", "активация на ваш", "подключение на ваш",
            "на вашу почту", "на ваш аккаунт", "куплю"
        ]
        # Preserve positive negations e.g. "не общий", "без соседей", "без аренды"
        cleaned_t = t
        for pos_neg in ["не общий", "без соседей", "без аренды", "без слотов", "не shared", "без общего"]:
            cleaned_t = cleaned_t.replace(pos_neg, "личный")

        if any(bad in cleaned_t for bad in shared_keywords):
            return False

        personal_keywords = [
            "личн", "персонал", "своя почта", "родная почта", "с почтой",
            "полный доступ", "авторег", "на одного", "в одни руки"
        ]
        return any(good in cleaned_t for good in personal_keywords)

    def parse_lots(self, html_content: str, node_id: int = 1355) -> List[Dict[str, Any]]:
        """Parses FunPay lots HTML table into structured lot dictionaries."""
        lots: List[Dict[str, Any]] = []
        item_regex = re.compile(
            r'(<a\s+[^>]*?class=["\'][^"\']*?tc-item[^"\']*?["\'][^>]*?>)(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )

        for match in item_regex.finditer(html_content):
            tag_open = match.group(1)
            body = match.group(2)
            full_html = match.group(0)

            # 1. Lot ID
            id_match = re.search(r'offer\?id=(\d+)', tag_open)
            if not id_match:
                continue
            lot_num = id_match.group(1)
            lot_id = f"funpay_{lot_num}"

            # 2. Metadata attributes
            f_sub_m = re.search(r'data-f-subscription=["\']([^"\']*)["\']', tag_open, re.IGNORECASE)
            f_sub = f_sub_m.group(1).strip() if f_sub_m else ""
            f_type_m = re.search(r'data-f-type=["\']([^"\']*)["\']', tag_open, re.IGNORECASE)
            f_type = f_type_m.group(1).strip() if f_type_m else ""

            # 3. Title / Description
            desc_m = re.search(r'class=["\']tc-desc-text["\'][^>]*>(.*?)</div>', body, re.DOTALL | re.IGNORECASE)
            raw_title = desc_m.group(1) if desc_m else ""
            clean_title = re.sub(r'<[^>]+>', '', raw_title).strip()
            clean_title = re.sub(r'\s+', ' ', clean_title)

            # 4. Seller
            seller_m = re.search(r'class=["\']media-user-name["\'][^>]*>(.*?)</div>', body, re.DOTALL | re.IGNORECASE)
            raw_seller = seller_m.group(1) if seller_m else "Неизвестен"
            seller = re.sub(r'<[^>]+>', '', raw_seller).strip()

            # 5. Rating & Reviews
            rating_m = re.search(r'rating-stars\s+rating-(\d+)', body, re.IGNORECASE)
            seller_rating = float(rating_m.group(1)) if rating_m else 0.0

            rev_m = re.search(r'class=["\']rating-mini-count["\'][^>]*>(\d+)<', body, re.IGNORECASE)
            seller_reviews = int(rev_m.group(1)) if rev_m else 0

            # 6. Price
            tc_price_m = re.search(r'(<div\s+[^>]*?class=["\'][^"\']*?tc-price[^"\']*?["\'][^>]*>.*?)(?:</a>|<div\s+class=["\']tc-|\Z)', body, re.DOTALL | re.IGNORECASE)
            tc_price_html = tc_price_m.group(1) if tc_price_m else ""

            price_val = 0.0
            if tc_price_html:
                p_m = re.search(r'data-s=["\']([0-9.,\s]+)["\']', tc_price_html, re.IGNORECASE)
                if p_m:
                    price_val = self._parse_price_value(p_m.group(1))

            if price_val == 0.0:
                p_m = re.search(r'data-s=["\']([0-9.,\s]+)["\']', full_html, re.IGNORECASE)
                if p_m:
                    price_val = self._parse_price_value(p_m.group(1))

            if price_val == 0.0 and tc_price_html:
                digits_m = re.search(r'([0-9\s.,]+)\s*(?:<span|\Z)', re.sub(r'<[^>]+>', ' ', tc_price_html))
                if digits_m:
                    price_val = self._parse_price_value(digits_m.group(1))

            unit_m = re.search(r'<span[^>]*class=["\'][^"\']*unit[^"\']*["\'][^>]*>([^<]+)</span>', tc_price_html, re.IGNORECASE) if tc_price_html else None
            unit = html.unescape(unit_m.group(1)).strip().lower() if unit_m else ""
            tc_clean = html.unescape(re.sub(r'<[^>]+>', ' ', tc_price_html)).lower() if tc_price_html else ""

            # Normalize to RUB
            if any(sym in (unit or tc_clean) for sym in ["$", "usd", "dollar"]):
                continue  # No verified FX quote
            elif any(sym in (unit or tc_clean) for sym in ["€", "eur", "euro"]):
                continue  # No verified FX quote
            elif any(sym in (unit or tc_clean) for sym in ["₴", "грн", "uah", "₸", "kzt", "byn", "zł", "try", "₺"]):
                continue
            elif re.search(r"₽|\bруб\.?|\brub\b", unit or tc_clean):
                price_rub = round(price_val, 2)
            else:
                continue  # Session cookies do not prove the displayed currency.

            if price_rub <= 0.0:
                continue

            from auto_flipper.categories import detect_category_for_lot, get_category_by_node
            cat = get_category_by_node(node_id) or detect_category_for_lot(node_id, clean_title)
            cat_id = cat.id if cat else "chatgpt"

            if cat_id == "chatgpt":
                is_plus = self._is_plus_lot(clean_title, f_type, f_sub)
                is_personal = self._is_personal_lot(clean_title)
            else:
                is_plus = True
                is_personal = not any(b in clean_title.lower() for b in cat.blacklisted_keywords) if cat else True

            lots.append({
                "lot_id": lot_id,
                "lot_num": lot_num,
                "title": clean_title,
                "price": price_rub,
                "currency": "RUB",
                "seller": seller,
                "seller_rating": seller_rating,
                "seller_reviews": seller_reviews,
                "url": f"{FUNPAY_BASE_URL}/lots/offer?id={lot_num}",
                "is_personal": is_personal,
                "is_plus": is_plus,
                "node_id": node_id,
                "category_id": cat_id,
            })

        return lots

    async def fetch_market_lots(self, node_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """
        Fetches active marketplace listings from FunPay target categories.
        Default nodes: all target categories [89, 923, 3734, 1568, 1391, 1355, 3559].
        """
        if node_ids is None:
            from auto_flipper.categories import get_all_target_node_ids
            node_ids = get_all_target_node_ids()

        all_lots: List[Dict[str, Any]] = []
        seen_ids = set()

        try:
            async with self._http_client(follow_redirects=True) as client:
                for node in node_ids:
                    url = f"{FUNPAY_BASE_URL}/lots/{node}/"
                    try:
                        resp = await client.get(url)
                        problem = self._response_problem(resp)
                        if problem:
                            logger.warning("FunPay market request blocked: %s", problem)
                            break
                        if resp.status_code == 200:
                            lots = self.parse_lots(resp.text, node_id=node)
                            for lot in lots:
                                if lot["lot_id"] not in seen_ids:
                                    seen_ids.add(lot["lot_id"])
                                    all_lots.append(lot)
                        else:
                            logger.warning(f"FunPay returned HTTP {resp.status_code} for node {node}")
                    except Exception as e:
                        logger.debug(f"Error fetching FunPay lots for node {node}: {self._safe_error(e)}")
        except Exception as e:
            logger.error(f"Error in fetch_market_lots: {self._safe_error(e)}")

        return all_lots
