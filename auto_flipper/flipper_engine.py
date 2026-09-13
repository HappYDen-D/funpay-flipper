"""
auto_flipper/flipper_engine.py — Autonomous coordinator for auto-buying, listing, order tracking, and auto-delivery
"""
import asyncio
import logging
import statistics
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from auto_flipper.categories import (
    CATEGORY_REGISTRY,
    CategoryDefinition,
    detect_category_for_lot,
    get_all_target_node_ids,
    get_category_by_id,
    get_category_by_node,
)
from auto_flipper.config import (
    ACCOUNT_MARKET_OBSERVER_ENABLED,
    ARBITRAGE_AUTO_BUY_DEFAULT,
    ARBITRAGE_DRY_RUN_DEFAULT,
    ARBITRAGE_MARKUP_DISCOUNT,
    ARBITRAGE_MAX_BUDGET_DEFAULT,
    ARBITRAGE_MIN_MARGIN_PCT,
    ARBITRAGE_MIN_PROFIT,
    ARBITRAGE_MIN_SELLER_RATING,
    ARBITRAGE_MIN_SELLER_REVIEWS,
    ARBITRAGE_PRICE_FLOOR,
    DEFAULT_PROFIT_GOAL,
    FUNPAY_DIGITAL_FEE_RATE,
    FUNPAY_LOTS_NODE_ACCOUNTS,
    MARKET_BENCHMARK_PRICE,
    NORMAL_POLL_INTERVAL,
    ORDERS_POLL_INTERVAL,
    TURBO_POLL_INTERVAL,
)
from auto_flipper.credential_extractor import CredentialExtractor, MultiCategoryCredentialExtractor
from auto_flipper.database import db
from auto_flipper.funpay_client import FunPayClient
from auto_flipper.math_engine import ArbitrageEvaluation, ArbitrageMath

logger = logging.getLogger("FlipperEngine")


class BackgroundBoostScheduler:
    """
    Automated lot boost scheduler and peak-window Turbo mode manager:
    - Periodically triggers FunPay lot raise (Bump / Boost).
    - If a cooldown is encountered (or after raising), sleeps until cooldown expires.
    - Upon successful raise (or simulation), keeps Turbo mode active for 30 minutes
      (the highest traffic conversion window), then automatically resets Turbo mode back to normal.
    """

    def __init__(self, engine: "FlipperEngine"):
        self.engine = engine
        self._scheduler_task: Optional[asyncio.Task] = None
        self._turbo_timer_task: Optional[asyncio.Task] = None
        self.next_raise_time: Optional[float] = None
        self.last_raise_time: Optional[float] = None
        self.turbo_expires_at: Optional[float] = None
        self.is_running: bool = False
        self.default_cooldown: int = 14400  # 4 hours standard FunPay cooldown
        self.turbo_duration: int = 1800     # 30 minutes peak traffic window

    def start(self, auto_schedule: bool = True):
        """Starts boost background scheduler."""
        self.is_running = True
        if auto_schedule and not self._scheduler_task:
            self.schedule_next(delay_seconds=1)

    def stop(self):
        """Stops boost background scheduler and cancels pending timers."""
        self.is_running = False
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            self._scheduler_task = None
        if self._turbo_timer_task and not self._turbo_timer_task.done():
            self._turbo_timer_task.cancel()
            self._turbo_timer_task = None

    def schedule_next(self, delay_seconds: int):
        """Schedules the next lot raise after delay_seconds."""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()

        clean_delay = max(1, delay_seconds)
        self.next_raise_time = time.time() + clean_delay
        self._scheduler_task = asyncio.create_task(self._sleep_and_raise(clean_delay))

    async def _sleep_and_raise(self, delay_seconds: int):
        try:
            await asyncio.sleep(delay_seconds)
            if self.is_running:
                if self.engine.can_act('boost'):
                    await self.trigger_boost()
                else:
                    # If emergency stopped, retry in 60 seconds without killing the scheduler
                    self.schedule_next(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in boost scheduler timer: {e}")
            if self.is_running:
                self.schedule_next(300)

    def activate_turbo_window(self, duration_seconds: Optional[int] = None):
        """Enables Turbo mode and sets auto-reset timer."""
        duration = duration_seconds if duration_seconds is not None else self.turbo_duration
        self.engine.turbo_mode = True
        db.set_setting("turbo_mode", "1")
        self.turbo_expires_at = time.time() + duration
        db.set_setting("turbo_expires_at", str(self.turbo_expires_at))

        if self._turbo_timer_task and not self._turbo_timer_task.done():
            self._turbo_timer_task.cancel()

        self._turbo_timer_task = asyncio.create_task(self._auto_reset_turbo(duration))

    async def _auto_reset_turbo(self, duration_seconds: int):
        try:
            await asyncio.sleep(duration_seconds)
            self.engine.turbo_mode = False
            db.set_setting("turbo_mode", "0")
            db.set_setting("turbo_expires_at", "0")
            self.turbo_expires_at = None
            logger.info("⚡ Turbo mode window expired (30 min peak ended). Reverted to normal polling interval.")
        except asyncio.CancelledError:
            pass

    async def trigger_boost(self) -> Dict[str, Any]:
        """
        Executes raise_lots:
        1. Calls FunPay POST /lots/raise.
        2. If successful (or dry_run): enables Turbo mode for 30 minutes,
           and schedules next raise after cooldown.
        3. If in cooldown: schedules next raise when cooldown expires.
        4. If failed with network/API error: retries after 5 min backoff.
        """
        if not self.engine.can_act('boost'):
            return {"success": False, "error": "MODE_BLOCKED", "status": "BLOCKED"}
        self.last_raise_time = time.time()
        res = await self.engine.client.raise_lots(dry_run=self.engine.dry_run)

        is_success = res.get("success", False)
        cooldown_sec = res.get("cooldown_seconds") or 0

        if is_success or self.engine.dry_run:
            self.activate_turbo_window(self.turbo_duration)
            next_delay = cooldown_sec if cooldown_sec > 0 else self.default_cooldown
            self.schedule_next(next_delay)
        elif res.get("cooldown") or cooldown_sec > 0:
            next_delay = cooldown_sec if cooldown_sec > 0 else 3600
            self.schedule_next(next_delay)
        else:
            logger.warning(f"Boost returned non-success ({res.get('error', 'error')}). Retrying in 5 min.")
            self.schedule_next(300)

        return res

    def get_scheduler_info(self) -> Dict[str, Any]:
        now = time.time()
        remaining_raise = max(0, int(self.next_raise_time - now)) if self.next_raise_time else None
        remaining_turbo = max(0, int(self.turbo_expires_at - now)) if self.turbo_expires_at else 0
        return {
            "is_running": self.is_running,
            "next_raise_time": self.next_raise_time,
            "remaining_raise_seconds": remaining_raise,
            "turbo_expires_at": self.turbo_expires_at,
            "remaining_turbo_seconds": remaining_turbo,
            "turbo_active": self.engine.turbo_mode,
        }


class ResumeResult(int):
    """
    Subclass of int for backward compatibility (evaluates as int/count),
    while exposing detailed reconciliation dictionary fields.
    """
    def __new__(cls, val: int, unresolved_intents: Optional[List[Dict[str, Any]]] = None):
        obj = super().__new__(cls, val)
        obj.reactivated_count = val
        obj.unresolved_intents = unresolved_intents or []
        obj.unresolved_intents_count = len(obj.unresolved_intents)
        obj.success = True
        return obj

    def __getitem__(self, item: str) -> Any:
        if item == "reactivated_count":
            return self.reactivated_count
        elif item == "unresolved_intents_count":
            return self.unresolved_intents_count
        elif item == "unresolved_intents":
            return self.unresolved_intents
        elif item == "success":
            return self.success
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except KeyError:
            return default


from auto_flipper.assistant_engine import AssistantWorkflow


class FlipperEngine(AssistantWorkflow):
    """
    Autonomous engine driving high-frequency ChatGPT Plus resale on FunPay:
    - Auto-evaluates candidate deals
    - Executes balance checkout
    - Extracts login & password from order chat
    - Automatically lists the account on FunPay with undercut pricing
    - Polls /orders/trade for incoming buyer orders
    - Delivers structured credentials to the buyer via FunPay runner chat
    - Manages emergency stops, profit goals, and lot boosts
    """

    def __init__(self):
        self.client = FunPayClient()
        self._load_settings()
        self.client.action_authorizer = lambda action: self.can_act(action) and not self.dry_run
        self.client.emergency_stop_checker = lambda: bool(self.is_emergency_stopped) or (hasattr(db, 'is_emergency_stopped') and bool(db.is_emergency_stopped()))
        self._is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._bot = None
        self.category_medians: Dict[str, float] = {
            cat_id: cat.market_benchmark for cat_id, cat in CATEGORY_REGISTRY.items()
        }
        self.boost_scheduler = BackgroundBoostScheduler(self)
        from auto_flipper.account_market_observer import AccountMarketObserver
        self.account_market_observer = AccountMarketObserver(self.client, db)
        self._account_observer_task: Optional[asyncio.Task] = None

    @property
    def market_median(self) -> float:
        return self.category_medians.get("chatgpt", MARKET_BENCHMARK_PRICE)

    @market_median.setter
    def market_median(self, val: float):
        self.category_medians["chatgpt"] = float(val)

    @property
    def auto_buy(self) -> bool:
        return self._auto_buy

    @auto_buy.setter
    def auto_buy(self, val: bool):
        self._auto_buy = bool(val)
        if self._auto_buy:
            if getattr(self, "mode", "OBSERVE") in ("OBSERVE", "PAUSED"):
                self.mode = "LIMITED_AUTO"
        else:
            if getattr(self, "mode", "OBSERVE") == "LIMITED_AUTO":
                self.mode = "OBSERVE"

    def _load_settings(self):
        dry_str = db.get_setting("dry_run")
        self.dry_run = (dry_str.lower() in ("true", "1", "yes")) if dry_str is not None else ARBITRAGE_DRY_RUN_DEFAULT

        ab_str = db.get_setting("auto_buy")
        self._auto_buy = (ab_str.lower() in ("true", "1", "yes")) if ab_str is not None else ARBITRAGE_AUTO_BUY_DEFAULT

        mode_str = db.get_flipper_mode()
        if mode_str:
            self.mode = mode_str if mode_str in ("OBSERVE", "ASSIST", "LIMITED_AUTO", "PAUSED") else "OBSERVE"
        else:
            self.mode = "LIMITED_AUTO" if self._auto_buy else "OBSERVE"

        self._auto_buy = self.mode == "LIMITED_AUTO"

        b_str = db.get_setting("purchase_cap")
        self.max_budget = float(b_str) if b_str is not None else 0.0

        p_str = db.get_setting("pilot_min_profit")
        self.min_profit = float(p_str) if p_str is not None else 10.0

        m_str = db.get_setting("pilot_min_roi_pct")
        self.min_margin_pct = float(m_str) if m_str is not None else 15.0

        r_str = db.get_setting("min_seller_rating")
        self.min_seller_rating = float(r_str) if r_str is not None else ARBITRAGE_MIN_SELLER_RATING

        rev_str = db.get_setting("min_seller_reviews")
        self.min_seller_reviews = int(rev_str) if rev_str is not None else ARBITRAGE_MIN_SELLER_REVIEWS

        stopped_str = db.get_setting("is_emergency_stopped")
        self.is_emergency_stopped = (stopped_str.lower() in ("true", "1", "yes")) if stopped_str is not None else False

        turbo_str = db.get_setting("turbo_mode")
        self.turbo_mode = (turbo_str.lower() in ("true", "1", "yes")) if turbo_str is not None else False
        turbo_exp_str = db.get_setting("turbo_expires_at")
        if self.turbo_mode and turbo_exp_str:
            try:
                exp_ts = float(turbo_exp_str)
                if exp_ts > 0 and (exp_ts - time.time()) <= 0:
                    self.turbo_mode = False
                    db.set_setting("turbo_mode", "0")
                    db.set_setting("turbo_expires_at", "0")
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # Settings & Toggles
    # ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> str:
        """Sets flipper operating mode: OBSERVE, ASSIST, LIMITED_AUTO, PAUSED."""
        clean_mode = mode.upper()
        if clean_mode not in ("OBSERVE", "ASSIST", "LIMITED_AUTO", "PAUSED"):
            clean_mode = "OBSERVE"
        if self.is_emergency_stopped and clean_mode != 'PAUSED':
            return self.mode
        self.mode = clean_mode
        db.set_flipper_mode(clean_mode)
        if clean_mode in ("OBSERVE", "ASSIST", "PAUSED"):
            self._auto_buy = False
            db.set_setting("auto_buy", "0")
        elif clean_mode == "LIMITED_AUTO":
            self._auto_buy = True
            db.set_setting("auto_buy", "1")
        logger.info(f"Flipper operating mode switched to: {self.mode}")
        return self.mode

    def toggle_dry_run(self) -> bool:
        self.set_mode("OBSERVE")
        self.dry_run = not self.dry_run
        db.set_setting("dry_run", "1" if self.dry_run else "0")
        logger.info(f"Flipper DRY_RUN toggled to: {self.dry_run}")
        return self.dry_run

    def toggle_auto_buy(self) -> bool:
        if self.is_emergency_stopped:
            logger.warning("Cannot toggle auto-buy while emergency stop is active. Reconcile and call resume_from_emergency().")
            return False
        self.set_mode("OBSERVE" if self.auto_buy else "LIMITED_AUTO")
        logger.info(f"Flipper AUTO_BUY toggled to: {self.auto_buy}")
        return self.auto_buy

    def toggle_turbo_mode(self) -> bool:
        self.turbo_mode = not self.turbo_mode
        db.set_setting("turbo_mode", "1" if self.turbo_mode else "0")
        if not self.turbo_mode:
            db.set_setting("turbo_expires_at", "0")
            if self.boost_scheduler._turbo_timer_task and not self.boost_scheduler._turbo_timer_task.done():
                self.boost_scheduler._turbo_timer_task.cancel()
                self.boost_scheduler.turbo_expires_at = None
        logger.info(f"Flipper TURBO_MODE toggled to: {self.turbo_mode}")
        return self.turbo_mode

    def set_max_budget(self, budget: float):
        from auto_flipper.economics import kopecks
        self.max_budget = kopecks(budget) / 100
        db.set_setting("purchase_cap", str(self.max_budget))

    def set_min_profit(self, profit: float):
        self.min_profit = max(10.0, float(profit))
        db.set_setting("pilot_min_profit", str(self.min_profit))

    # ─────────────────────────────────────────────────────────────
    # Deal Evaluation & Auto-Purchase Pipeline
    # ─────────────────────────────────────────────────────────────

    def evaluate_candidate_deal(
        self,
        lot_id: str = "candidate",
        title: str = "",
        price: float = 0.0,
        seller: str = "",
        seller_rating: float = 5.0,
        seller_reviews: int = 10,
        node_id: int = 1355,
        category_id: Optional[str] = None,
        is_personal: bool = True,
        is_plus: bool = True,
        lowest_reputable: Optional[float] = None,
        review: Optional[Dict[str, Any]] = None,
        currency: str = "UNKNOWN",
    ) -> ArbitrageEvaluation:
        cat = get_category_by_id(category_id) if category_id else (get_category_by_node(node_id) or detect_category_for_lot(node_id, title))
        if not cat:
            return ArbitrageEvaluation(
                is_eligible=False,
                rejection_reasons=[f"Категория не определена для node_id={node_id}, title='{title}' (NEEDS_EVIDENCE)"],
                category_id="unknown",
                buy_price=price,
                sell_price=0.0,
                expected_revenue=0.0,
                expected_profit=0.0,
                margin_pct=0.0,
                roi_pct=0.0,
                platform_fee=0.0,
                pricing_source="Unknown category (NEEDS_EVIDENCE)",
            )

        from auto_flipper.safety import validate_review, EXCLUDED_CATEGORIES
        from auto_flipper.exit_policy import evaluate_exit
        from auto_flipper.economics import decimal, kopecks
        reasons = []
        if cat.id in EXCLUDED_CATEGORIES:
            reasons.append('SUBSCRIPTIONS_EXCLUDED')
        if not self.dry_run and (getattr(cat, 'is_deprecated', False) or cat.id == 'mm2_items' or cat.node_id == 925):
            reasons.append('CATEGORY_DEPRECATED_FOR_REAL_PURCHASE')
        if not db.is_category_enabled(cat.id):
            reasons.append('CATEGORY_DISABLED')
        if cat.node_id != node_id:
            reasons.append('CATEGORY_NODE_MISMATCH')
        if self.max_budget > 0 and price > self.max_budget:
            reasons.append('MANUAL_PURCHASE_CAP')
        if currency != 'RUB':
            reasons.append('PURCHASE_CURRENCY_UNVERIFIED')
        try:
            if kopecks(price) <= 0:
                reasons.append('INVALID_PURCHASE_PRICE')
        except ValueError:
            return ArbitrageEvaluation(False, ['INVALID_PURCHASE_PRICE'], category_id=cat.id)
        try:
            if not 0 <= decimal(seller_rating) <= 5 or type(seller_reviews) is not int or seller_reviews < 0:
                raise ValueError('Invalid seller history')
        except (ValueError, TypeError):
            return ArbitrageEvaluation(False, ['INVALID_SELLER_HISTORY'], category_id=cat.id, buy_price=price)
        if seller_rating < max(self.min_seller_rating, cat.min_seller_rating):
            reasons.append('SELLER_RATING_TOO_LOW')
        if seller_reviews < max(self.min_seller_reviews, cat.min_seller_reviews):
            reasons.append('SELLER_HISTORY_TOO_SHORT')
        if any(bad in title.lower() for bad in cat.blacklisted_keywords):
            reasons.append('UNSAFE_PRODUCT_DESCRIPTION')
        if not review:
            return ArbitrageEvaluation(False, reasons + ['NEEDS_VERIFIED_DEMAND'],
                category_id=cat.id, buy_price=price, pricing_source='Нет подтверждённого выкупа')
        try:
            validate_review(review)
        except (ValueError, KeyError, TypeError) as error:
            return ArbitrageEvaluation(False, reasons + [str(error)], category_id=cat.id, buy_price=price)
        # Historical median prices and guessed probabilities never authorize spending.
        route = evaluate_exit(review, price, min_profit=self.min_profit,
                              min_roi=max(.15, self.min_margin_pct / 100))
        reasons.extend(route['reasons'])
        if not route.get('net_quote'):
            return ArbitrageEvaluation(False, reasons, category_id=cat.id, buy_price=price)
        net, profit = float(route['net_quote']), float(route['net_profit'])
        return ArbitrageEvaluation(
            is_eligible=not reasons, rejection_reasons=reasons, category_id=cat.id,
            buy_price=price, sell_price=float(review.get('listing_price', 0)),
            expected_revenue=net, expected_profit=profit,
            margin_pct=float(decimal(route['net_profit']) / decimal(route['net_quote']) * 100),
            roi_pct=float(decimal(route['roi']) * 100), pricing_source='Условная прибыль по заявке на выкуп',
            b_max=float(route['b_max']), ev_rub=0, exit_metrics=route)

    def evaluate_deal(
        self,
        title: str,
        price: float,
        seller: str,
        seller_rating: float = 5.0,
        seller_reviews: int = 10,
        is_personal: bool = True,
        is_plus: bool = True,
        lowest_reputable: Optional[float] = None,
    ) -> ArbitrageEvaluation:
        """Backwards compatibility default evaluation for ChatGPT Plus lots."""
        return self.evaluate_candidate_deal(
            lot_id="candidate",
            title=title,
            price=price,
            seller=seller,
            seller_rating=seller_rating,
            seller_reviews=seller_reviews,
            node_id=1355,
            category_id="chatgpt",
            is_personal=is_personal,
            is_plus=is_plus,
            lowest_reputable=lowest_reputable,
        )

    async def process_candidate_deal(
        self,
        lot_id: str,
        title: str,
        price: float,
        seller: str,
        seller_rating: float,
        seller_reviews: int,
        node_id: int = 1355,
        category_id: Optional[str] = None,
        is_personal: bool = True,
        is_plus: bool = True,
    ) -> Optional[str]:
        if not self.auto_buy or self.mode != 'LIMITED_AUTO' or not self.can_act('checkout'):
            return None
        candidate = db.get_candidate(lot_id)
        if not candidate:
            return None
        try:
            return await self._execute_reviewed(candidate)
        except (ValueError, KeyError, TypeError) as error:
            db.log_action('CANDIDATE_REJECTED',f'{lot_id}: {error}')
            return None

    async def _process_post_purchase(
        self, item_uuid: str, order_id: str, orig_title: str, sell_price: float, category_id: str = "chatgpt"
    ):
        """Waits for seller message in order chat, extracts credentials, and lists offer with category-specific copy."""
        item = db.get_inventory_item(item_uuid)
        if not item or bool(item['is_dry_run']) != self.dry_run or not self.can_act('publish'):
            return
        cat = get_category_by_id(category_id)
        if not cat:
            return
        logger.info(f"Processing post-purchase for {item_uuid} [{cat.id}] (Order: {order_id})...")

        if cat.item_type in ('trade_item', 'permanent_key'):
            db.update_inventory(item_uuid, status='awaiting_intake')
            return  # Platform asset intake and transfer are confirmed separately.

        credentials = None
        if self.dry_run:
            # Simulate instant credentials delivery in simulation mode for this category
            if cat.id == "steam":
                login = f"steam_user_{item_uuid[-4:]}"
                password = f"SteamPass_{item_uuid[-4:]}!"
                mail = f"mail_{item_uuid[-4:]}@rambler.ru"
                mail_pass = f"Mail_{item_uuid[-4:]}#"
                credentials = {
                    "category": "steam",
                    "login": login,
                    "password": password,
                    "mail_password": mail_pass,
                    "mail": mail,
                    "link": "",
                    "key": "",
                    "raw": f"{login}:{password}:{mail}:{mail_pass}",
                    "formatted": CredentialExtractor.format_steam_payload(login, password, f"{mail} (пароль: {mail_pass})"),
                }
            elif cat.id == "discord":
                link = f"https://discord.com/billing/promotions/{uuid.uuid4().hex}"
                credentials = {
                    "category": "discord",
                    "login": "DISCORD_PROMO_LINK",
                    "password": link.split("/")[-1],
                    "mail_password": "",
                    "link": link,
                    "key": "",
                    "raw": link,
                    "formatted": CredentialExtractor.format_discord_payload(link),
                }
            elif cat.id == "cursor":
                login = f"cursor_pro_{item_uuid[-4:]}@gmail.com"
                password = f"Cursor_{item_uuid[-4:]}!"
                credentials = {
                    "category": "cursor",
                    "login": login,
                    "password": password,
                    "mail_password": "mail_pass_secret",
                    "link": "",
                    "key": "",
                    "raw": f"{login}:{password}",
                    "formatted": CredentialExtractor.format_cursor_payload(login, password, "доступ в почту по паролю"),
                }
            elif cat.id == "exitlag":
                key = f"EXIT-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
                credentials = {
                    "category": "exitlag",
                    "login": "EXITLAG_KEY",
                    "password": key,
                    "mail_password": "",
                    "link": "",
                    "key": key,
                    "raw": f"ExitLag Key: {key}",
                    "formatted": CredentialExtractor.format_license_key_payload(key),
                }
            elif cat.id == "tg_premium":
                link = f"https://t.me/giftcode/{uuid.uuid4().hex}"
                credentials = {
                    "category": "tg_premium",
                    "login": "TELEGRAM_GIFT_LINK",
                    "password": link.split("/")[-1],
                    "mail_password": "",
                    "link": link,
                    "key": "",
                    "raw": link,
                    "formatted": CredentialExtractor.format_telegram_payload(link),
                }
            else:
                login = f"chatgpt_pro_{item_uuid[-6:]}@gmail.com"
                password = f"Pass_{item_uuid[-4:]}!2026"
                mail_pass = f"Mail_{item_uuid[-4:]}#"
                credentials = {
                    "category": "chatgpt",
                    "login": login,
                    "password": password,
                    "mail_password": mail_pass,
                    "link": "",
                    "key": "",
                    "cookies": "",
                    "raw": "SIMULATED CREDENTIALS",
                    "formatted": CredentialExtractor.format_chatgpt_payload(login, password, mail_pass),
                }
        else:
            # Poll order chat up to 5 times (waiting up to 60s for seller automated delivery)
            for _ in range(5):
                await asyncio.sleep(8)
                msgs = await self.client.fetch_order_chat(order_id)
                for m in msgs:
                    parsed = CredentialExtractor.extract(m, category_id=cat.id)
                    if parsed:
                        credentials = parsed
                        break
                if credentials:
                    break

        if credentials:
            if bool(item['is_dry_run']) != self.dry_run:
                return
            db.update_inventory(
                item_uuid,
                status="ready_for_sale" if self.dry_run else "awaiting_intake",
                credentials_raw=credentials.get("raw", ""),
                credentials_parsed=credentials.get("formatted", ""),
            )
            logger.info(f"Credentials obtained for {item_uuid} [{cat.id}]. Ready for sale!")

            if not self.dry_run or not self.can_act('publish'):
                return  # Parsed access must pass an explicit intake review.
            # Auto-list simulated inventory only.
            resale_title = cat.template_title
            resale_desc = cat.template_desc
            list_res = await self.client.save_offer(
                node_id=cat.node_id,
                title=resale_title,
                desc=resale_desc,
                price=sell_price,
                active=True,
                custom_fields=cat.custom_fields,
                dry_run=self.dry_run,
            )

            if list_res.get("success"):
                resale_lot_id = list_res.get("offer_id", "resale_lot")
                now_str = datetime.now().isoformat()
                db.update_inventory(item_uuid, status="listed", resale_lot_id=resale_lot_id, listed_at=now_str)
                logger.info(f"Offer listed on FunPay [{cat.name}]: {resale_lot_id} at {sell_price} RUB!")
                db.log_action("AUTO_LIST", f"Listed offer {resale_lot_id} [{cat.id}] for {item_uuid} at {sell_price} RUB")
            else:
                logger.error(f"Failed to list offer for {item_uuid}: {list_res.get('error')}")
        else:
            logger.warning(f"No credentials received yet from seller for order {order_id} [{cat.id}].")

    # ─────────────────────────────────────────────────────────────
    # Buyer Order Listener & Auto-Delivery
    # ─────────────────────────────────────────────────────────────

    async def check_and_fulfill_buyer_orders(self) -> int:
        """
        Polls FunPay incoming trade orders (/orders/trade).
        If an order is paid, matches with active inventory by category and delivers credentials.
        """
        if self.dry_run or not self.can_act('deliver'):
            return 0

        incoming = await self.client.fetch_incoming_orders()
        fulfilled_count = 0

        for order in incoming:
            if not self.can_act("deliver") or not order.get("is_paid"):
                continue

            order_id = order["order_id"]
            # Check if this buyer order is already fulfilled using buyer_order_id (Fixes duplicate delivery bug)
            existing = db.get_inventory_by_buyer_order(order_id)
            if existing:
                continue

            # Identify target category or exact resale lot for this buyer order
            order_node = order.get("node_id")
            order_title = order.get("title") or order.get("desc", "")
            resale_lot_id = order.get("lot_id") or order.get("resale_lot_id") or order.get("offer_id") or db.get_verified_offer_for_order(order_id)
            cat = detect_category_for_lot(order_node or 0, order_title)
            cat_id = cat.id if cat else None
            node_id = cat.node_id if cat else order_node

            if not cat_id and not node_id and not resale_lot_id:
                logger.warning(f"Could not determine category or lot for order {order_id} ('{order_title}'). Skipping fulfillment to prevent misdelivery.")
                continue

            # Atomically reserve matching available inventory item to prevent double-delivery race condition
            item = db.reserve_inventory_for_fulfillment(
                category_id=cat_id,
                node_id=node_id,
                resale_lot_id=resale_lot_id,
                buyer_order_id=order_id,
            )
            if not item:
                # If no matching item is currently ready for this category, skip
                continue

            item_uuid = item["item_uuid"]
            creds_payload = item.get("credentials_parsed") or item.get("credentials_raw", "")
            if not creds_payload:
                db.update_inventory(item_uuid, status="reserved", delivery_status="missing_creds")
                continue

            try:
                if not self.can_act('deliver'):
                    continue
                send_res = await self.client.send_chat_message(
                    order_id=order_id, message=creds_payload, dry_run=False)
            except (Exception, asyncio.CancelledError):
                db.update_inventory(item_uuid,status='reserved',delivery_status='unknown')
                raise
            if send_res.get('success'):
                db.update_inventory(item_uuid,status='delivered',delivery_status='delivered',
                                    settlement_status='pending',net_profit_realized=0.0,
                                    sold_at=datetime.now().isoformat(),buyer_username=order.get('buyer',''))
                fulfilled_count += 1
                db.log_action('DELIVERED',f'Order {order_id}; awaiting verified settlement')
            else:
                # Keep binding: the server may have accepted the message before timeout.
                db.update_inventory(item_uuid,status='reserved',delivery_status='unknown')

        return fulfilled_count

    # ─────────────────────────────────────────────────────────────
    # Emergency Kill-Switch & Boost (/boost)
    # ─────────────────────────────────────────────────────────────

    def emergency_stop(self) -> Dict[str, Any]:
        self.is_emergency_stopped = True
        self.set_mode('PAUSED')
        db.set_setting('is_emergency_stopped','1')
        self.boost_scheduler.stop()
        count = db.deactivate_all_active_listings()
        return {'success': True, 'deactivated_count': 0, 'pending_count': count,
                'auto_buy': False, 'is_emergency_stopped': True}

    async def stop_and_deactivate(self):
        self.emergency_stop()
        completed, failed = [], []
        for item in db.get_inventory_list(status='emergency_paused',limit=100000):
            if not item.get('resale_lot_id') or bool(item['is_dry_run']) != self.dry_run:
                continue
            try:
                ok = await self.client.deactivate_offer(item['resale_lot_id'],
                                                       node_id=item['node_id'],dry_run=self.dry_run)
            except Exception:
                ok = False
            (completed if ok else failed).append(item['resale_lot_id'])
        return {'success': not failed, 'deactivated_count': len(completed),
                'failed_count': len(failed), 'failed_offers': failed}

    def resume_from_emergency(self) -> Dict[str, Any]:
        unresolved = db.get_unresolved_purchase_intents()
        result = ResumeResult(0, unresolved_intents=unresolved)
        if unresolved:
            result.success = False
            return result
        self.is_emergency_stopped = False
        db.set_setting('is_emergency_stopped','0')
        self.set_mode('OBSERVE')
        # External listings remain paused until individually checked and republished.
        return result

    async def execute_boost(self) -> Dict[str, Any]:
        """
        Executes /boost:
        1. Triggers boost via boost_scheduler.
        2. Returns status with boost details, turbo status, and next scheduled raise.
        """
        raise_res = await self.boost_scheduler.trigger_boost()
        scheduler_info = self.boost_scheduler.get_scheduler_info()

        db.log_action("BOOST", "Triggered lot raise and scheduled peak-window Turbo mode")
        return {
            "success": raise_res.get("success", True),
            "dry_run": self.dry_run,
            "raise_info": raise_res,
            "turbo_mode": self.turbo_mode,
            "scheduler": scheduler_info,
            "timestamp": datetime.now().isoformat(),
        }

    # ─────────────────────────────────────────────────────────────
    # Background Processing Loops
    # ─────────────────────────────────────────────────────────────

    async def start_background_loops(self, bot=None):
        if self._is_running:
            return
        self._is_running = True
        self._bot = bot
        self.boost_scheduler.start(auto_schedule=self.can_act("boost"))
        self._loop_task = asyncio.create_task(self._main_flipper_loop())
        if ACCOUNT_MARKET_OBSERVER_ENABLED:
            self._account_observer_task = asyncio.create_task(self.account_market_observer.run())
        logger.info("FlipperEngine background runner launched.")

    async def stop(self):
        self._is_running = False
        self.boost_scheduler.stop()
        self.account_market_observer.stop()
        if self._account_observer_task:
            self._account_observer_task.cancel()
            try:
                await self._account_observer_task
            except asyncio.CancelledError:
                pass
            self._account_observer_task = None
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("FlipperEngine stopped.")

    async def check_orphan_bought_orders(self, min_age_seconds: float = 0.0, max_age_hours: float = 48.0) -> int:
        """
        Background watcher for orphan orders stuck in 'bought' state.
        If a seller replied and sent credentials after the initial window,
        this extracts credentials with category-awareness, updates inventory, and automatically lists the offer for sale.
        Orders older than max_age_hours are marked as unresolved to prevent infinite polling.
        """
        if not self.can_act('publish'):
            return 0
        bought_items = db.get_inventory_list(status="bought", limit=25)
        if not bought_items:
            return 0

        recovered_count = 0
        now = datetime.now()
        for item in bought_items:
            if bool(item['is_dry_run']) != self.dry_run or not self.can_act('publish'):
                continue
            order_id = item.get("order_id")
            item_uuid = item.get("item_uuid")
            if not order_id or not item_uuid:
                continue

            cat_id = item.get("category_id") or "chatgpt"
            cat = get_category_by_id(cat_id) or CATEGORY_REGISTRY["chatgpt"]
            if cat.item_type in ('trade_item', 'permanent_key'):
                db.update_inventory(item_uuid, status='awaiting_intake')
                continue

            bought_at_str = item.get("bought_at")
            if bought_at_str:
                try:
                    b_dt = datetime.fromisoformat(bought_at_str)
                    age_sec = (now - b_dt).total_seconds()
                    if age_sec < min_age_seconds:
                        # Order is fresh and actively being processed by post-purchase loop
                        continue
                    if age_sec > (max_age_hours * 3600):
                        # Permanently stalled order; mark as unresolved to stop polling
                        db.update_inventory(item_uuid, status="unresolved", notes=f"No credentials within {max_age_hours}h")
                        logger.warning(f"Order {order_id} ({item_uuid}) exceeded {max_age_hours}h without credentials. Marked unresolved.")
                        continue
                except Exception:
                    pass

            credentials = None
            if item.get("is_dry_run") or self.dry_run:
                # In simulation/dry-run, simulate instant category credentials
                if cat.id == "steam":
                    login = f"steam_user_{item_uuid[-4:]}"
                    password = f"SteamPass_{item_uuid[-4:]}!"
                    mail_pass = f"Mail_{item_uuid[-4:]}#"
                    credentials = {
                        "category": "steam",
                        "login": login,
                        "password": password,
                        "mail_password": mail_pass,
                        "raw": f"{login}:{password}:mail@rambler.ru:{mail_pass}",
                        "formatted": CredentialExtractor.format_steam_payload(login, password, f"mail@rambler.ru ({mail_pass})"),
                    }
                elif cat.id == "discord":
                    link = f"https://discord.com/billing/promotions/{uuid.uuid4().hex}"
                    credentials = {
                        "category": "discord",
                        "login": "DISCORD_PROMO_LINK",
                        "password": link.split("/")[-1],
                        "link": link,
                        "raw": link,
                        "formatted": CredentialExtractor.format_discord_payload(link),
                    }
                elif cat.id == "cursor":
                    login = f"cursor_{item_uuid[-4:]}@gmail.com"
                    password = f"Cursor_{item_uuid[-4:]}!"
                    credentials = {
                        "category": "cursor",
                        "login": login,
                        "password": password,
                        "raw": f"{login}:{password}",
                        "formatted": CredentialExtractor.format_cursor_payload(login, password, "доступ в почту"),
                    }
                elif cat.id == "exitlag":
                    key = f"EXIT-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
                    credentials = {
                        "category": "exitlag",
                        "login": "EXITLAG_KEY",
                        "password": key,
                        "key": key,
                        "raw": f"ExitLag Key: {key}",
                        "formatted": CredentialExtractor.format_license_key_payload(key),
                    }
                elif cat.id == "tg_premium":
                    link = f"https://t.me/giftcode/{uuid.uuid4().hex}"
                    credentials = {
                        "category": "tg_premium",
                        "login": "TELEGRAM_GIFT_LINK",
                        "password": link.split("/")[-1],
                        "link": link,
                        "raw": link,
                        "formatted": CredentialExtractor.format_telegram_payload(link),
                    }
                else:
                    credentials = {
                        "category": "chatgpt",
                        "login": f"chatgpt_pro_{item_uuid[-6:]}@gmail.com",
                        "password": f"Pass_{item_uuid[-4:]}!2026",
                        "mail_password": f"Mail_{item_uuid[-4:]}#",
                        "raw": "SIMULATED ORPHAN RECOVERED",
                        "formatted": CredentialExtractor.format_chatgpt_payload(
                            f"chatgpt_pro_{item_uuid[-6:]}@gmail.com",
                            f"Pass_{item_uuid[-4:]}!2026",
                            f"Mail_{item_uuid[-4:]}#",
                        ),
                    }
            else:
                try:
                    msgs = await self.client.fetch_order_chat(order_id)
                    for m in msgs:
                        parsed = CredentialExtractor.extract(m, category_id=cat.id)
                        if parsed:
                            credentials = parsed
                            break
                except Exception as e:
                    logger.debug(f"Error checking chat for orphan order {order_id}: {e}")

            if credentials:
                if bool(item['is_dry_run']) != self.dry_run:
                    continue
                db.update_inventory(
                    item_uuid,
                    status="ready_for_sale" if self.dry_run else "awaiting_intake",
                    credentials_raw=credentials.get("raw", ""),
                    credentials_parsed=credentials.get("formatted", ""),
                )
                logger.info(f"🔄 ORPHAN RECOVERED! Credentials extracted for {item_uuid} [{cat.id}] (Order: {order_id})")

                if not self.dry_run or not self.can_act('publish'):
                    continue
                resale_title = cat.template_title
                resale_desc = cat.template_desc
                cat_median = self.category_medians.get(cat.id, cat.market_benchmark)
                sell_price = item.get("sell_price") or ArbitrageMath.calculate_sell_price(
                    cat_median, markup_discount=cat.markup_discount, price_floor=cat.price_floor
                )

                list_res = await self.client.save_offer(
                    node_id=cat.node_id,
                    title=resale_title,
                    desc=resale_desc,
                    price=sell_price,
                    active=True,
                    custom_fields=cat.custom_fields,
                    dry_run=self.dry_run,
                )

                if list_res.get("success"):
                    resale_lot_id = list_res.get("offer_id", "resale_lot")
                    now_str = datetime.now().isoformat()
                    db.update_inventory(item_uuid, status="listed", resale_lot_id=resale_lot_id, listed_at=now_str)
                    logger.info(f"Recovered orphan offer listed on FunPay [{cat.name}]: {resale_lot_id} at {sell_price} RUB!")
                    db.log_action("ORPHAN_RECOVERED", f"Listed recovered orphan item {item_uuid} [{cat.id}] (Offer {resale_lot_id})")
                    recovered_count += 1

        return recovered_count

    async def scan_and_autobuy_cycle(self) -> List[str]:
        """
        Scans FunPay marketplace across all enabled categories for underpriced candidate deals.
        Updates market median price dynamically per category.
        Evaluates eligible deals and triggers auto-purchase for qualifying lots.
        """
        from auto_flipper.safety import EXCLUDED_CATEGORIES
        enabled_cat_ids = [c for c in db.get_enabled_categories() if c not in EXCLUDED_CATEGORIES]
        target_nodes = [
            CATEGORY_REGISTRY[cid].node_id
            for cid in enabled_cat_ids
            if cid in CATEGORY_REGISTRY
            and not getattr(CATEGORY_REGISTRY[cid], 'is_deprecated', False)
            and CATEGORY_REGISTRY[cid].node_id not in (0, 925)
        ]
        if "chatgpt" in enabled_cat_ids and 3559 not in target_nodes:
            target_nodes.append(3559)

        if not target_nodes:
            return []

        candidate_lots = await self.client.fetch_market_lots(node_ids=target_nodes)
        if not candidate_lots:
            return []

        # Record market history for tracked benchmark SKUs
        try:
            confirmed_nodes = getattr(self.client, "last_market_scan_nodes", None)
            db.record_market_observation(
                candidate_lots,
                scanned_nodes=set(target_nodes) if confirmed_nodes is None else set(confirmed_nodes),
            )
        except Exception as e:
            logger.warning(f"Error recording market history observation: {e}")

        # Update dynamic median prices per category from observed listings
        category_prices: Dict[str, List[float]] = {cid: [] for cid in CATEGORY_REGISTRY.keys()}
        for l in candidate_lots:
            node = l.get("node_id", 1355)
            cat = detect_category_for_lot(node, l.get("title", ""))
            if not cat:
                continue
            price = l.get("price", 0.0)
            if cat.min_buy_price <= price <= (cat.market_benchmark * 2.5):
                category_prices[cat.id].append(price)

        for c_id, prices in category_prices.items():
            if len(prices) >= 3:
                self.category_medians[c_id] = float(statistics.median(prices))

        bought_uuids: List[str] = []
        alert_rows = []
        for lot in candidate_lots:
            lot_id = lot["lot_id"]
            # Skip if already bought or recorded in inventory
            if db.get_inventory_by_lot(lot_id):
                continue

            node = lot.get("node_id", 1355)
            cat = detect_category_for_lot(node, lot.get("title", ""))
            if not cat:
                continue
            cat_id = cat.id

            payload = {k: lot[k] for k in ('lot_id','title','price','seller','node_id','category_id','seller_rating','seller_reviews','is_personal','is_plus','currency') if k in lot}
            payload['category_id'] = cat_id
            payload['seller_rating'] = lot.get('seller_rating',0.0)
            payload['seller_reviews'] = lot.get('seller_reviews',0)
            previous = db.get_candidate(lot_id)
            db.observe_candidate(payload)
            if not previous or previous['payload'] != payload:
                alert_rows.append(db.get_candidate(lot_id))
            if not self.auto_buy or not self.can_act('checkout'):
                continue
            # Process candidate deal
            uuid_bought = await self.process_candidate_deal(
                lot_id=lot_id,
                title=lot["title"],
                price=lot["price"],
                seller=lot["seller"],
                seller_rating=lot.get("seller_rating", 0.0),
                seller_reviews=lot.get("seller_reviews", 0),
                node_id=node,
                category_id=cat_id,
                is_personal=lot.get("is_personal", True),
                is_plus=lot.get("is_plus", True),
            )
            if uuid_bought:
                bought_uuids.append(uuid_bought)
                # Rate-limit to at most 1 buy per scan pass
                break

        from auto_flipper.candidate_alerts import notify_candidates
        await notify_candidates(self, db, alert_rows)
        return bought_uuids

    async def _main_flipper_loop(self):
        while self._is_running:
            try:
                # 1. Check incoming buyer orders on FunPay and fulfill
                await self.check_and_fulfill_buyer_orders()

                # 2. Check and recover orphan bought orders (stuck >45s)
                await self.check_orphan_bought_orders(min_age_seconds=45.0)

                # 3. Scan FunPay market for profitable underpriced deals to buy
                if self.mode != "PAUSED" and not self.is_emergency_stopped:
                    await self.scan_and_autobuy_cycle()

                # 4. Polling sleep depending on turbo mode (reverts to NORMAL_POLL_INTERVAL when turbo expires)
                interval = TURBO_POLL_INTERVAL if self.turbo_mode else NORMAL_POLL_INTERVAL
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in flipper loop: {e}", exc_info=True)
                await asyncio.sleep(10.0)

    # ─────────────────────────────────────────────────────────────
    # Status Summary
    # ─────────────────────────────────────────────────────────────

    def get_status_summary(self) -> Dict[str, Any]:
        pnl = db.get_pnl_stats()
        counts = db.get_inventory_counts()
        goal_data = db.get_goal_progress()

        return {
            "dry_run": self.dry_run,
            "auto_buy": self.auto_buy,
            "is_emergency_stopped": self.is_emergency_stopped,
            "turbo_mode": self.turbo_mode,
            "max_budget": self.max_budget,
            "min_profit": self.min_profit,
            "min_margin_pct": self.min_margin_pct,
            "market_median": self.market_median,
            "category_medians": dict(self.category_medians),
            "enabled_categories": db.get_enabled_categories(),
            "target_resale_price": ArbitrageMath.calculate_sell_price(self.market_median),
            "counts": counts,
            "pnl": pnl,
            "goal": goal_data,
            "scheduler": self.boost_scheduler.get_scheduler_info(),
        }


# Global flipper engine instance
flipper_engine = FlipperEngine()
