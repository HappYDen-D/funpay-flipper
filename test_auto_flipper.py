"""
test_auto_flipper.py — Unit and integration test suite for the Autonomous Auto-Flipper Bot
"""
import asyncio
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

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

_test_workspace = tempfile.TemporaryDirectory(prefix="flipper-legacy-tests-")
os.environ["FLIPPER_DB_PATH"] = os.path.join(_test_workspace.name, "test.db")

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from auto_flipper.bot import _can_reach_telegram_direct, _is_port_open, create_bot_and_dispatcher, resolve_telegram_session
from auto_flipper.categories import (
    CATEGORY_REGISTRY,
    CategoryDefinition,
    detect_category_for_lot,
    get_all_target_node_ids,
    get_category_by_id,
    get_category_by_node,
)
from auto_flipper.config import BOT_TOKEN, DB_PATH
from auto_flipper.credential_extractor import CredentialExtractor, MultiCategoryCredentialExtractor
from auto_flipper.database import Database, db
from auto_flipper.flipper_engine import BackgroundBoostScheduler, FlipperEngine, flipper_engine
from auto_flipper.funpay_client import FunPayClient
from auto_flipper.handlers import (
    GoalInputState,
    cb_categories_menu,
    cb_edit_budget,
    cb_emergency_stop,
    cb_enable_all_categories,
    cb_goal_menu,
    cb_pnl_categories,
    cb_raise_now,
    cb_resume,
    cb_set_goal_preset,
    cb_settings_menu,
    cb_toggle_buy,
    cb_toggle_category,
    cb_toggle_dry,
    cb_toggle_turbo,
    cmd_boost,
    cmd_browser,
    cmd_categories,
    cmd_emergency_stop,
    cmd_goal,
    cmd_help,
    cmd_inventory,
    cmd_mode,
    cmd_pnl,
    cmd_resume,
    cmd_settings,
    cmd_start,
    cmd_status,
    cb_mode_menu,
    cb_set_mode,
    is_admin,
)
from auto_flipper.math_engine import ArbitrageEvaluation, ArbitrageMath


def verified_receipt(storage, item_id, receipt):
    storage.update_inventory(item_id, is_dry_run=False, status='delivered')
    with patch('auto_flipper.database.ADMIN_IDS', [42]):
        storage.record_receipt(item_id,'test-receipt:'+item_id,receipt,'test statement',42)


class TestFlipperMath(unittest.TestCase):
    def test_calculate_sell_price_basic(self):
        # Median target: 1099.0 * 0.82 = 901.18 -> round = 901.0
        price = ArbitrageMath.calculate_sell_price(market_median=1099.0)
        self.assertEqual(price, 901.0)

    def test_calculate_sell_price_floor(self):
        # If median is low, e.g. 500 * 0.82 = 410.0, price floor is 450.0
        price = ArbitrageMath.calculate_sell_price(market_median=500.0, price_floor=450.0)
        self.assertEqual(price, 450.0)

    def test_calculate_sell_price_undercut_lowest_reputable(self):
        # Median target 901, but lowest reputable competitor is 750 -> undercut: 750 - 10 = 740.0
        price = ArbitrageMath.calculate_sell_price(market_median=1099.0, lowest_reputable_price=750.0)
        self.assertEqual(price, 740.0)

    def test_calculate_net_revenue(self):
        # 1000 * 0.88 = 880.0 (12% FunPay fee)
        rev = ArbitrageMath.calculate_net_revenue(1000.0, fee_rate=0.12)
        self.assertEqual(rev, 880.0)

    def test_calculate_profit_and_margin(self):
        # Buy 300, Sell 600 -> Net revenue = 600 * 0.88 = 528.0
        # Profit = 528.0 - 300.0 = 228.0
        # Margin = (228.0 / 300.0) * 100 = 76.0%
        rev, profit, margin = ArbitrageMath.calculate_profit_and_margin(300.0, 600.0, fee_rate=0.12)
        self.assertEqual(rev, 528.0)
        self.assertEqual(profit, 228.0)
        self.assertEqual(margin, 76.0)

    def test_evaluate_deal_eligible(self):
        res = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный аккаунт с почтой",
            price=290.0,
            seller="TopSeller",
            seller_rating=5.0,
            seller_reviews=50,
            market_median=1099.0,
            is_personal=True,
            is_plus=True,
            max_budget=350.0,
            min_profit=150.0,
        )
        self.assertTrue(res.is_eligible, f"Should be eligible: {res.rejection_reasons}")
        self.assertEqual(res.buy_price, 290.0)
        self.assertGreaterEqual(res.expected_profit, 150.0)

    def test_evaluate_deal_budget_exceeded(self):
        res = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный аккаунт с почтой",
            price=399.0,  # exceeds 350.0
            seller="TopSeller",
            seller_rating=5.0,
            seller_reviews=50,
            market_median=1099.0,
            max_budget=350.0,
        )
        self.assertFalse(res.is_eligible)
        self.assertTrue(any("превышает бюджет" in r for r in res.rejection_reasons))

    def test_evaluate_deal_low_rating_rejected(self):
        res = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный аккаунт с почтой",
            price=250.0,
            seller="LowRatingSeller",
            seller_rating=4.2,  # below 4.8
            seller_reviews=100,
            market_median=1099.0,
        )
        self.assertFalse(res.is_eligible)
        self.assertTrue(any("Рейтинг продавца" in r for r in res.rejection_reasons))

    def test_evaluate_deal_low_reviews_rejected(self):
        res = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный аккаунт с почтой",
            price=250.0,
            seller="NewSeller",
            seller_rating=5.0,
            seller_reviews=1,  # below 5
            market_median=1099.0,
        )
        self.assertFalse(res.is_eligible)
        self.assertTrue(any("отзывов" in r for r in res.rejection_reasons))


class TestCredentialExtractor(unittest.TestCase):
    def test_extract_standard_email_pass_colon(self):
        text = "chatgpt_user@domain.com:SecretPass123!"
        res = CredentialExtractor.extract(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["login"], "chatgpt_user@domain.com")
        self.assertEqual(res["password"], "SecretPass123!")

    def test_extract_standard_email_pass_pipe_and_dash(self):
        cases = [
            "ai_guru@mail.ru|SuperKey999",
            "gpt_pro@gmail.com - UltraSecret44",
            "john.doe@proton.me/MyPass_2026",
        ]
        for c in cases:
            res = CredentialExtractor.extract(c)
            self.assertIsNotNone(res, f"Failed parsing: {c}")
            self.assertIn("@", res["login"])
            self.assertGreaterEqual(len(res["password"]), 4)

    def test_extract_labeled_russian_format(self):
        text = """
        Здравствуйте! Ваш заказ:
        Логин: user_gpt@yandex.ru
        Пароль: Password_ChatGPT_5
        Пароль от почты: NativeMail_Pass!
        Спасибо за покупку!
        """
        res = CredentialExtractor.extract(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["login"], "user_gpt@yandex.ru")
        self.assertEqual(res["password"], "Password_ChatGPT_5")
        self.assertEqual(res["mail_password"], "NativeMail_Pass!")

    def test_extract_multiline_format(self):
        text = "account123@gmail.com\nMyStrongPassword987"
        res = CredentialExtractor.extract(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["login"], "account123@gmail.com")
        self.assertEqual(res["password"], "MyStrongPassword987")

    def test_mask_password(self):
        self.assertEqual(CredentialExtractor.mask_password("secret123"), "se*******")
        self.assertEqual(CredentialExtractor.mask_password("abc"), "***")
        self.assertEqual(CredentialExtractor.mask_password(""), "—")

    def test_format_delivery_payload(self):
        formatted = CredentialExtractor.format_delivery_payload("user@test.com", "Pass123", "MailPass99")
        self.assertIn("user@test.com", formatted)
        self.assertIn("Pass123", formatted)
        self.assertIn("MailPass99", formatted)
        self.assertIn("ИНСТРУКЦИЯ ПО ЭКСПЛУАТАЦИИ", formatted)


class TestFlipperDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_flipper.db")
        self.db = Database(db_path=self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_inventory_crud_and_transitions(self):
        # 1. Add bought item
        uuid = self.db.add_inventory(
            lot_id="fp_1001",
            title="ChatGPT Plus 1 месяц",
            buy_price=280.0,
            sell_price=750.0,
            net_profit_expected=380.0,
            seller="SellerA",
            order_id="ORD-1001",
            status="bought",
            is_dry_run=True,
        )
        self.assertTrue(uuid.startswith("flip_"))

        item = self.db.get_inventory_item(uuid)
        self.assertIsNotNone(item)
        self.assertEqual(item["status"], "bought")
        self.assertEqual(item["buy_price"], 280.0)

        # 2. Update to ready_for_sale
        self.db.update_inventory(uuid, status="ready_for_sale", credentials_raw="user:pass")
        item = self.db.get_inventory_item(uuid)
        self.assertEqual(item["status"], "ready_for_sale")

        # 3. Update to listed
        self.db.update_inventory(uuid, status="listed", resale_lot_id="LOT-777")
        item_by_resale = self.db.get_inventory_by_resale_lot("LOT-777")
        self.assertIsNotNone(item_by_resale)
        self.assertEqual(item_by_resale["item_uuid"], uuid)

        # 4. Update to sold
        self.db.update_inventory(
            uuid,
            status="sold",
            buyer_order_id="BUY-888",
            buyer_username="HappyBuyer",
            net_profit_realized=380.0,
            sold_at=datetime.now().isoformat(),
        )
        item_sold = self.db.get_inventory_item(uuid)
        self.assertEqual(item_sold["status"], "sold")
        self.assertEqual(item_sold["buyer_username"], "HappyBuyer")

    def test_emergency_stop_and_resume(self):
        u1 = self.db.add_inventory("1", "T1", 200, 500, 240, status="listed")
        u2 = self.db.add_inventory("2", "T2", 250, 600, 278, status="listed")

        paused_count = self.db.deactivate_all_active_listings()
        self.assertEqual(paused_count, 2)

        it1 = self.db.get_inventory_item(u1)
        self.assertEqual(it1["status"], "emergency_paused")

        resumed_count = self.db.reactivate_emergency_listings()
        self.assertEqual(resumed_count, 2)

        it1 = self.db.get_inventory_item(u1)
        self.assertEqual(it1["status"], "listed")

    def test_pnl_stats_calculation(self):
        # Add 1 sold item
        u1 = self.db.add_inventory(
            "1", "Item1", buy_price=300.0, sell_price=700.0, net_profit_expected=316.0,
            status="sold",
        )
        self.db.update_inventory(u1, net_profit_realized=316.0)

        verified_receipt(self.db,u1,616)
        pnl = self.db.get_pnl_stats()
        self.assertEqual(pnl["total_sold"], 1)
        self.assertEqual(pnl["total_spent_on_sold"], 300.0)
        self.assertEqual(pnl["total_revenue_gross"], 616.0)
        self.assertEqual(pnl["total_fees"], 0.0)
        self.assertEqual(pnl["total_net_profit"], 316.0)
        self.assertGreater(pnl["roi_pct"], 100.0)

    def test_profit_goal_tracking(self):
        # Default goal is 5000
        goal = self.db.get_profit_goal()
        self.assertEqual(goal, 5000.0)

        self.db.set_profit_goal(10000.0)
        self.assertEqual(self.db.get_profit_goal(), 10000.0)

        progress = self.db.get_goal_progress()
        self.assertEqual(progress["goal"], 10000.0)
        self.assertIn("bar", progress)
        self.assertIsInstance(progress["percent"], float)

    def test_goal_dynamic_average_and_eta(self):
        # Initial goal with 0 sales: falls back to BENCHMARK_NET_PROFIT (219.20)
        self.db.set_profit_goal(5000.0)
        p1 = self.db.get_goal_progress()
        self.assertEqual(p1["avg_per_flip"], 0.0)
        self.assertFalse(p1["has_dynamic_avg"])
        self.assertIsNone(p1["flips_needed"])
        self.assertIn("Недостаточно данных", p1["eta_text"])

        # Add 2 sales with net_profit_realized = 300.0 and 400.0 (average = 350.0)
        u1 = self.db.add_inventory("1", "T1", 200, 500, 240, status="sold")
        self.db.update_inventory(u1, net_profit_realized=300.0)
        u2 = self.db.add_inventory("2", "T2", 200, 600, 328, status="sold")
        self.db.update_inventory(u2, net_profit_realized=400.0)

        verified_receipt(self.db,u1,500)
        verified_receipt(self.db,u2,600)
        p2 = self.db.get_goal_progress()
        self.assertEqual(p2["avg_per_flip"], 350.0)
        self.assertTrue(p2["has_dynamic_avg"])
        self.assertEqual(p2["realized"], 700.0)
        self.assertEqual(p2["flips_needed"], 13)
        self.assertIsNone(p2["eta_seconds"])
        # Verify ETA text has no phantom days (e.g. not "~0 д")
        self.assertNotIn("~0 д", p2["eta_text"])

    def test_goal_milestone_tracking_and_reset(self):
        self.db.set_profit_goal(1000.0)
        # Realized 0 -> None
        self.assertIsNone(self.db.check_and_update_milestones())

        # Add sale crossing 25% (260.0 RUB)
        u1 = self.db.add_inventory("1", "T1", 200, 500, 240, status="sold")
        self.db.update_inventory(u1, net_profit_realized=260.0)
        verified_receipt(self.db,u1,460)
        m = self.db.check_and_update_milestones()
        self.assertEqual(m, 25)

        # Checking again should return None (already notified)
        self.assertIsNone(self.db.check_and_update_milestones())

        # Add sale crossing 50% (total 550.0 RUB)
        u2 = self.db.add_inventory("2", "T2", 200, 500, 240, status="sold")
        self.db.update_inventory(u2, net_profit_realized=290.0)
        verified_receipt(self.db,u2,490)
        m2 = self.db.check_and_update_milestones()
        self.assertEqual(m2, 50)

        # Reset goal -> milestones should reset
        self.db.set_profit_goal(2000.0)
        # Realized is 550 / 2000 = 27.5%, should trigger milestone 25 for new goal
        m3 = self.db.check_and_update_milestones()
        self.assertEqual(m3, 25)

    def test_get_inventory_by_lot(self):
        self.db.add_inventory(
            lot_id="fp_target_999",
            title="ChatGPT Plus Target",
            buy_price=250.0,
            sell_price=700.0,
            net_profit_expected=350.0,
        )
        found = self.db.get_inventory_by_lot("fp_target_999")
        self.assertIsNotNone(found)
        self.assertEqual(found["lot_id"], "fp_target_999")
        self.assertIsNone(self.db.get_inventory_by_lot("nonexistent_lot"))

    def test_get_all_active_users(self):
        self.db.get_or_create_user(user_id=8881, username="admin1")
        active = self.db.get_all_active_users()
        self.assertTrue(any(u["user_id"] == 8881 for u in active))


class TestFunPayClient(unittest.IsolatedAsyncioTestCase):
    async def test_csrf_token_extraction(self):
        html_text = '<div data-app-data=\'{"csrf-token":"test_csrf_token_12345"}\'></div>'
        token = FunPayClient._extract_csrf_token(html_text)
        self.assertEqual(token, "test_csrf_token_12345")

    async def test_account_info_unauthenticated(self):
        client = FunPayClient(golden_key="")
        info = await client.get_account_info()
        self.assertFalse(info["is_authenticated"])
        self.assertEqual(info["balance_rub"], 0.0)

    async def test_checkout_lot_dry_run(self):
        client = FunPayClient(golden_key="dummy_key")
        res = await client.checkout_lot(lot_id="funpay_555", price=299.0, dry_run=True)
        self.assertTrue(res["success"])
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["order_id"].startswith("SIM-"))

    async def test_save_offer_dry_run(self):
        client = FunPayClient(golden_key="dummy_key")
        res = await client.save_offer(node_id=1355, title="Test Offer", desc="Desc", price=750.0, dry_run=True)
        self.assertTrue(res["success"])
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["offer_id"].startswith("SIM-LOT-"))

    async def test_raise_lots_dry_run(self):
        client = FunPayClient(golden_key="dummy_key")
        res = await client.raise_lots(dry_run=True)
        self.assertTrue(res["success"])
        self.assertTrue(res["dry_run"])
        self.assertIn(1355, res["raised_nodes"])

    def test_parse_cooldown_seconds(self):
        # Russian formats
        self.assertEqual(FunPayClient.parse_cooldown_seconds("Подождите 3 ч 45 мин"), 13500)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("Подождите 1 час 20 минут"), 4800)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 1 минуту"), 60)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 15 минут"), 900)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 2 ч"), 7200)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 2ч 30м"), 9000)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("Подождите 45 сек"), 45)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 1 секунду"), 1)
        self.assertEqual(FunPayClient.parse_cooldown_seconds("через 1 день 2 часа"), 93600)
        # English format
        self.assertEqual(FunPayClient.parse_cooldown_seconds("Wait 1 hour 30 minutes"), 5400)
        # Non cooldown
        self.assertIsNone(FunPayClient.parse_cooldown_seconds("Лоты успешно подняты"))
        self.assertIsNone(FunPayClient.parse_cooldown_seconds(""))

    def test_parse_balances_hold_and_available(self):
        # 1. Available + hold from text
        html1 = """
        <div class="user-balance">1 250 ₽</div>
        <div class="balance-hold">В холде: 500 ₽</div>
        """
        b1 = FunPayClient.parse_balances(html1)
        self.assertEqual(b1["balance_total"], 1250.0)
        self.assertEqual(b1["balance_hold"], 500.0)
        self.assertEqual(b1["balance_available"], 750.0)

        # 2. Attributes format
        html2 = '<div data-balance-available="800.5" data-balance-hold="300" data-balance-total="1100.5"></div>'
        b2 = FunPayClient.parse_balances(html2)
        self.assertEqual(b2["balance_available"], 800.5)
        self.assertEqual(b2["balance_hold"], 300.0)
        self.assertEqual(b2["balance_total"], 1100.5)

        # 3. Simple balance without hold
        html3 = '<span class="badge-balance">990.0 ₽</span>'
        b3 = FunPayClient.parse_balances(html3)
        self.assertEqual(b3["balance_available"], 990.0)
        self.assertEqual(b3["balance_hold"], 0.0)
        self.assertEqual(b3["balance_total"], 990.0)

    async def test_account_info_cloudflare_and_expired(self):
        client = FunPayClient(golden_key="some_key")

        # Mock Cloudflare 403
        cf_resp = MagicMock()
        cf_resp.status_code = 403
        cf_resp.text = "<html>Just a moment... Attention Required! | Cloudflare</html>"
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = cf_resp
            info = await client.get_account_info()
            self.assertEqual(info["session_status"], "cloudflare")
            self.assertFalse(info["is_authenticated"])

        # Mock expired session (200 OK but guest, no username)
        guest_resp = MagicMock()
        guest_resp.status_code = 200
        guest_resp.text = '<html><div class="guest-nav">Войти на сайт</div></html>'
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = guest_resp
            info = await client.get_account_info()
            self.assertEqual(info["session_status"], "expired")
            self.assertFalse(info["is_authenticated"])

    def test_extract_chat_node_id(self):
        html1 = '<div class="chat-wrapper" data-node="987654"></div>'
        self.assertEqual(FunPayClient.extract_chat_node_id(html1), 987654)

        html2 = '<input type="hidden" name="node" value="112233">'
        self.assertEqual(FunPayClient.extract_chat_node_id(html2), 112233)

        # Reversed attribute order
        html3 = '<input type="hidden" value="998877" name="node">'
        self.assertEqual(FunPayClient.extract_chat_node_id(html3), 998877)

        # Chat tag with data-id before class
        html4 = '<div data-id="445566" class="chat-box"></div>'
        self.assertEqual(FunPayClient.extract_chat_node_id(html4), 445566)

        # Script JSON config
        html5 = '<script>var config = {"nodeId": 778899};</script>'
        self.assertEqual(FunPayClient.extract_chat_node_id(html5), 778899)

        self.assertIsNone(FunPayClient.extract_chat_node_id("<div>no node here</div>"))

    async def test_resolve_chat_node_id(self):
        client = FunPayClient(golden_key="some_key")
        # Numeric order IDs also require the actual chat node from the order page.
        order_resp = MagicMock()
        order_resp.status_code = 200
        order_resp.text = '<div class="chat-window" data-node="554433"></div>'
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = order_resp
            node = await client.resolve_chat_node_id("123456")
            self.assertEqual(node, 554433)
            mock_get.assert_awaited_once_with('https://funpay.com/orders/123456/')
            # Alphanumeric order_id resolved from HTML
            node = await client.resolve_chat_node_id("ORD-ALPHA99")
            self.assertEqual(node, 554433)
            # Second call uses cache
            self.assertEqual(client._order_node_cache.get("ALPHA99"), 554433)

    def test_parse_lots(self):
        client = FunPayClient()
        html_sample = """
        <a href="https://funpay.com/lots/offer?id=12345" class="tc-item" data-f-subscription="Plus" data-f-type="plus">
            <div class="tc-desc-text">ChatGPT Plus 1 месяц (30 дней) 🔑 Личный аккаунт 📩 Почта в комплекте</div>
            <div class="media-user-name">TopSeller</div>
            <div class="rating-stars rating-5"></div>
            <div class="rating-mini-count">50</div>
            <div class="tc-price" data-s="299.0"><span class="unit">₽</span></div>
        </a>
        <a href="https://funpay.com/lots/offer?id=67890" class="tc-item">
            <div class="tc-desc-text">【 ОБЩИЙ АККАУНТ 】 CHAT GPT PLUS 7 ДНЕЙ</div>
            <div class="media-user-name">SharedSeller</div>
            <div class="tc-price" data-s="99.0"><span class="unit">₽</span></div>
        </a>
        """
        lots = client.parse_lots(html_sample, node_id=1355)
        self.assertEqual(len(lots), 2)

        lot1 = lots[0]
        self.assertEqual(lot1["lot_id"], "funpay_12345")
        self.assertEqual(lot1["price"], 299.0)
        self.assertEqual(lot1["seller"], "TopSeller")
        self.assertEqual(lot1["seller_rating"], 5.0)
        self.assertEqual(lot1["seller_reviews"], 50)
        self.assertTrue(lot1["is_plus"])
        self.assertTrue(lot1["is_personal"])

        lot2 = lots[1]
        self.assertEqual(lot2["lot_id"], "funpay_67890")
        self.assertFalse(lot2["is_personal"])

    async def test_fetch_market_lots_mocked(self):
        client = FunPayClient()
        html_sample = """
        <a href="https://funpay.com/lots/offer?id=99911" class="tc-item">
            <div class="tc-desc-text">ChatGPT Plus Личный аккаунт с почтой</div>
            <div class="media-user-name">SuperSeller</div>
            <div class="tc-price" data-s="320.0"><span class="unit">₽</span></div>
        </a>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_sample

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            lots = await client.fetch_market_lots(node_ids=[1355])
            self.assertEqual(len(lots), 1)
            self.assertEqual(lots[0]["lot_id"], "funpay_99911")
            self.assertEqual(lots[0]["price"], 320.0)


class TestFlipperEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for key,value in (('is_emergency_stopped','0'),('flipper_mode','ASSIST'),('auto_buy','0')):
            db.set_setting(key,value)
        with db._get_connection() as conn:
            for table in ('purchase_intents','purchase_claims','trade_money_events','trade_candidates','flipper_inventory'):
                conn.execute('DELETE FROM '+table)
        self.engine = FlipperEngine()
        self.engine.dry_run = True
        self.engine.turbo_mode = False
        db.set_setting("turbo_mode", "0")
        with db._lock, db._get_connection() as conn:
            conn.cursor().execute("UPDATE flipper_inventory SET status = 'sold' WHERE status IN ('listed', 'ready_for_sale', 'emergency_paused')")
            conn.commit()

    def test_settings_toggles(self):
        old_dry = self.engine.dry_run
        new_dry = self.engine.toggle_dry_run()
        self.assertNotEqual(old_dry, new_dry)

        old_buy = self.engine.auto_buy
        new_buy = self.engine.toggle_auto_buy()
        self.assertNotEqual(old_buy, new_buy)

        old_turbo = self.engine.turbo_mode
        new_turbo = self.engine.toggle_turbo_mode()
        self.assertNotEqual(old_turbo, new_turbo)

    async def test_execute_boost(self):
        res = await self.engine.execute_boost()
        self.assertTrue(res["success"])
        self.assertTrue(res["turbo_mode"])

    def test_emergency_stop_and_resume(self):
        stop_res = self.engine.emergency_stop()
        self.assertTrue(stop_res["success"])
        self.assertTrue(self.engine.is_emergency_stopped)
        self.assertFalse(self.engine.auto_buy)

        resumed = self.engine.resume_from_emergency()
        self.assertFalse(self.engine.is_emergency_stopped)
        self.assertIsInstance(resumed, int)

    async def test_process_candidate_deal_eligible(self):
        self.engine.auto_buy = True
        self.engine.is_emergency_stopped = False
        self.engine.max_budget = 350.0
        unique_lot = f"fp_test_{uuid.uuid4().hex[:8]}"

        item_uuid = await self.engine.process_candidate_deal(
            lot_id=unique_lot,
            title="ChatGPT Plus Личный аккаунт с почтой",
            price=290.0,
            seller="GreatSeller",
            seller_rating=5.0,
            seller_reviews=20,
            is_personal=True,
            is_plus=True,
        )
        # Even a large paper spread needs a persisted reviewed candidate.
        self.assertIsNone(item_uuid)
        self.assertIsNone(db.get_inventory_by_lot(unique_lot))

    async def test_process_candidate_deal_ineligible(self):
        self.engine.auto_buy = True
        # Exceeds budget
        res = await self.engine.process_candidate_deal(
            lot_id="fp_overbudget",
            title="ChatGPT Plus Личный",
            price=999.0,
            seller="Seller",
            seller_rating=5.0,
            seller_reviews=20,
        )
        self.assertIsNone(res)

    async def test_scan_and_autobuy_cycle(self):
        self.engine.auto_buy = True
        self.engine.is_emergency_stopped = False
        self.engine.max_budget = 350.0
        unique_cycle_lot = f"fp_cycle_{uuid.uuid4().hex[:8]}"

        sample_lots = [
            {
                "lot_id": unique_cycle_lot,
                "title": "ChatGPT Plus Личный аккаунт с почтой",
                "price": 280.0,
                "seller": "GoodSeller",
                "seller_rating": 5.0,
                "seller_reviews": 15,
                "is_personal": True,
                "is_plus": True,
            }
        ]
        with patch.object(self.engine.client, "fetch_market_lots", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = sample_lots
            bought = await self.engine.scan_and_autobuy_cycle()
            self.assertEqual(len(bought), 0)
            self.assertIsNotNone(db.get_candidate(unique_cycle_lot))

    async def test_check_and_fulfill_buyer_orders_mocked(self):
        self.engine.dry_run = False
        unique_fulfill_lot = f"fp_fulfill_{uuid.uuid4().hex[:8]}"
        unique_order_id = f"ORD-FULFILL-{uuid.uuid4().hex[:8]}"

        # Add listed item to inventory
        item_uuid = db.add_inventory(
            lot_id=unique_fulfill_lot,
            title="ChatGPT Plus Account",
            buy_price=250.0,
            sell_price=700.0,
            net_profit_expected=366.0,
            status="listed",
        )
        db.update_inventory(item_uuid, credentials_parsed="LOGIN: login@mail.com\nPASS: secret")

        offer='EXACT-'+uuid.uuid4().hex[:8]
        db.update_inventory(item_uuid,is_dry_run=False,resale_lot_id=offer)
        with db._get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO inventory_checks VALUES(?,?,?,?)',(item_uuid,'test intake',0,12345))
        incoming_orders = [
            {
                "order_id": unique_order_id,
                "desc": "ChatGPT Plus Account",
                "buyer": "BuyerPro",
                "status": "Оплачен",
                "is_paid": True,
                "offer_id": offer,
            }
        ]
        with patch.object(self.engine.client, "fetch_incoming_orders", new_callable=AsyncMock) as mock_orders, \
             patch.object(self.engine.client, "send_chat_message", new_callable=AsyncMock) as mock_send:
            mock_orders.return_value = incoming_orders
            mock_send.return_value = {"success": True, "dry_run": False}

            fulfilled = await self.engine.check_and_fulfill_buyer_orders()
            self.assertEqual(fulfilled, 1)

            sold_item = db.get_inventory_item(item_uuid)
            self.assertEqual(sold_item["status"], "delivered")
            self.assertEqual(sold_item["buyer_username"], "BuyerPro")

    async def test_background_boost_scheduler_and_turbo_reset(self):
        scheduler = self.engine.boost_scheduler
        self.assertFalse(self.engine.turbo_mode)

        # Trigger boost in dry run -> activates turbo window and schedules next
        res = await scheduler.trigger_boost()
        self.assertTrue(res["success"])
        self.assertTrue(self.engine.turbo_mode)
        status = scheduler.get_scheduler_info()
        self.assertTrue(status["turbo_active"])
        self.assertGreater(status["remaining_turbo_seconds"], 0)

        # Test turbo auto-reset with fast 0.05s timer
        scheduler.activate_turbo_window(duration_seconds=0.05)
        self.assertTrue(self.engine.turbo_mode)
        await asyncio.sleep(0.1)
        self.assertFalse(self.engine.turbo_mode)

    async def test_check_orphan_bought_orders(self):
        self.engine.dry_run = False
        unique_lot = f"fp_orphan_{uuid.uuid4().hex[:8]}"
        unique_ord = f"ORD-ORPHAN-{uuid.uuid4().hex[:8]}"

        # Add item stuck in 'bought' state
        item_uuid = db.add_inventory(
            lot_id=unique_lot,
            title="ChatGPT Plus Account",
            buy_price=250.0,
            sell_price=690.0,
            net_profit_expected=357.0,
            order_id=unique_ord,
            status="bought",
            is_dry_run=False,
        )

        # Mock seller late delivery after initial window
        late_chat_msg = ["Вот ваши данные:\nlogin_gpt@mail.ru:SecretPass2026\nПочта: MailPass!"]
        with patch.object(self.engine.client, "fetch_order_chat", new_callable=AsyncMock) as mock_chat, \
             patch.object(self.engine.client, "save_offer", new_callable=AsyncMock) as mock_save:
            mock_chat.return_value = late_chat_msg
            mock_save.return_value = {"success": True, "offer_id": "OFFER-RECOVERED-123"}

            recovered = await self.engine.check_orphan_bought_orders()
            self.assertEqual(recovered, 0)
            mock_save.assert_not_awaited()

            updated = db.get_inventory_item(item_uuid)
            self.assertEqual(updated["status"], "awaiting_intake")
            self.assertIsNone(updated["resale_lot_id"])


class TestFlipperHandlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        flipper_engine.dry_run = True
        db.set_setting("dry_run", "1")
        flipper_engine.turbo_mode = False
        db.set_setting("turbo_mode", "0")
        db.get_or_create_user(user_id=12345, username="test_user", first_name="Test")
        db.set_user_admin(12345, is_admin=True)
        admins=patch("auto_flipper.database.ADMIN_IDS",[12345]);admins.start();self.addCleanup(admins.stop)
        flipper_engine.is_emergency_stopped=False
        flipper_engine.set_mode("ASSIST")

    def _create_mock_message(self, text="/start", user_id=12345):
        msg = AsyncMock()
        msg.text = text
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
        msg.from_user.username = "test_user"
        msg.from_user.first_name = "Test"
        return msg

    def _create_mock_callback(self, data="flip_goal", user_id=12345):
        cb = AsyncMock()
        cb.data = data
        cb.from_user = MagicMock()
        cb.from_user.id = user_id
        cb.from_user.username = "test_user"
        cb.from_user.first_name = "Test"
        cb.message = AsyncMock()
        return cb

    async def test_cmd_start(self):
        msg = self._create_mock_message("/start")
        state = AsyncMock()
        await cmd_start(msg, state)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Автономный бот-флипер FunPay", call_text)

    async def test_cmd_status(self):
        msg = self._create_mock_message("/status")
        await cmd_status(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Авто-выкуп:", call_text)

    async def test_cmd_pnl(self):
        msg = self._create_mock_message("/pnl")
        await cmd_pnl(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Финансовый отчёт и баланс флипера (P&L)", call_text)

    async def test_cmd_inventory(self):
        msg = self._create_mock_message("/inventory")
        await cmd_inventory(msg)
        self.assertTrue(msg.answer.called)

    async def test_cmd_boost(self):
        msg = self._create_mock_message("/boost")
        await cmd_boost(msg)
        self.assertTrue(msg.answer.called)
        # Verify response mentions FunPay Raise or boost
        call_text = msg.answer.call_args_list[-1][0][0]
        self.assertIn("РЕЗУЛЬТАТ БУСТА ЛОТОВ", call_text)

    async def test_cmd_browser(self):
        msg = self._create_mock_message("/browser")
        await cmd_browser(msg)
        self.assertTrue(msg.answer.called)

    async def test_cmd_goal(self):
        # 1. View goal
        msg = self._create_mock_message("/goal")
        await cmd_goal(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Цель прибыли", call_text)

        # 2. Set goal via argument
        msg_set = self._create_mock_message("/goal 15000")
        await cmd_goal(msg_set)
        self.assertTrue(msg_set.answer.called)

    async def test_cmd_emergency_stop(self):
        msg = self._create_mock_message("/emergency_stop")
        await cmd_emergency_stop(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("ЭКСТРЕННЫЙ СТОП ВЫПОЛНЕН", call_text)

    async def test_cmd_resume(self):
        msg = self._create_mock_message("/resume")
        await cmd_resume(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Проверка возобновления", call_text)

    async def test_cmd_settings(self):
        msg = self._create_mock_message("/settings")
        state = AsyncMock()
        await cmd_settings(msg, state)
        self.assertTrue(msg.answer.called)

    async def test_cmd_help(self):
        msg = self._create_mock_message("/help")
        await cmd_help(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("рабочий пилот ASSIST", call_text)
        self.assertIn('/capital', call_text)
        self.assertIn('/prepare', call_text)

    async def test_cb_set_goal_preset(self):
        cb = self._create_mock_callback("flip_set_goal_10000")
        await cb_set_goal_preset(cb)
        self.assertTrue(cb.message.edit_text.called)
        edit_text = cb.message.edit_text.call_args[0][0]
        self.assertIn("10 000 ₽", edit_text)
        self.assertEqual(db.get_profit_goal(12345), 10000.0)

    async def test_cb_raise_now(self):
        cb = self._create_mock_callback("flip_raise_now")
        await cb_raise_now(cb)
        self.assertTrue(cb.message.answer.called)
        ans = cb.message.answer.call_args[0][0]
        self.assertIn("DRY RUN", ans)
        self.assertIn("в симуляции", ans)
        self.assertNotIn("Лоты успешно подняты", ans)

    async def test_cmd_categories(self):
        msg = self._create_mock_message("/categories")
        await cmd_categories(msg)
        self.assertTrue(msg.answer.called)
        call_text = msg.answer.call_args[0][0]
        self.assertIn("Целевые ликвидные позиции FunPay", call_text)
        self.assertIn("Steam", call_text)
        self.assertIn("Discord", call_text)

    async def test_cb_categories_menu(self):
        cb = self._create_mock_callback("flip_categories")
        await cb_categories_menu(cb)
        self.assertTrue(cb.message.edit_text.called)
        edit_text = cb.message.edit_text.call_args[0][0]
        self.assertIn("Целевые ликвидные позиции FunPay", edit_text)

    async def test_cb_toggle_category(self):
        cb = self._create_mock_callback("flip_toggle_cat_steam")
        initial_state = db.is_category_enabled("steam")
        await cb_toggle_category(cb)
        self.assertNotEqual(db.is_category_enabled("steam"), initial_state)
        self.assertTrue(cb.message.edit_text.called)
        # Restore state so downstream tests are not polluted
        db.set_category_enabled("steam", initial_state)

    async def test_cb_enable_all_categories(self):
        cb = self._create_mock_callback("flip_cat_enable_all")
        db.set_category_enabled("steam", False)
        await cb_enable_all_categories(cb)
        for cat_id, cat in CATEGORY_REGISTRY.items():
            from auto_flipper.safety import EXCLUDED_CATEGORIES
            expected_enabled = cat_id not in EXCLUDED_CATEGORIES and not getattr(cat, 'is_deprecated', False)
            self.assertEqual(db.is_category_enabled(cat_id), expected_enabled)

    async def test_cb_pnl_categories(self):
        cb = self._create_mock_callback("flip_pnl_categories")
        await cb_pnl_categories(cb)
        self.assertTrue(cb.message.edit_text.called)
        edit_text = cb.message.edit_text.call_args[0][0]
        self.assertIn("Финансовый отчёт по категориям товаров", edit_text)


class TestCategoryRegistry(unittest.TestCase):
    def test_registry_categories_present(self):
        expected = ["steam", "discord", "cursor", "exitlag", "tg_premium", "chatgpt"]
        for cat_id in expected:
            self.assertIn(cat_id, CATEGORY_REGISTRY)
            cat = CATEGORY_REGISTRY[cat_id]
            self.assertIsInstance(cat, CategoryDefinition)
            self.assertGreater(cat.node_id, 0)
            self.assertGreater(cat.max_buy_price, 0)
            self.assertGreater(cat.market_benchmark, cat.max_buy_price)

    def test_get_category_by_node(self):
        self.assertEqual(get_category_by_node(89).id, "steam")
        self.assertEqual(get_category_by_node(923).id, "discord")
        self.assertEqual(get_category_by_node(3734).id, "cursor")
        self.assertEqual(get_category_by_node(1568).id, "exitlag")
        self.assertEqual(get_category_by_node(1391).id, "tg_premium")
        self.assertEqual(get_category_by_node(1355).id, "chatgpt")
        self.assertEqual(get_category_by_node(3559).id, "chatgpt")
        self.assertIsNone(get_category_by_node(999999))

    def test_detect_category_for_lot(self):
        # 1. By node_id
        cat_steam = detect_category_for_lot(node_id=89, title="Авторег с почтой")
        self.assertEqual(cat_steam.id, "steam")

        # 2. By title keyword matching
        cat_discord = detect_category_for_lot(node_id=0, title="Discord Nitro 3 месяца с гарантией")
        self.assertEqual(cat_discord.id, "discord")

        cat_cursor = detect_category_for_lot(node_id=0, title="Cursor AI Pro Аккаунт Fast")
        self.assertEqual(cat_cursor.id, "cursor")

        cat_exitlag = detect_category_for_lot(node_id=0, title="ExitLag ключ на 30 дней код")
        self.assertEqual(cat_exitlag.id, "exitlag")

        cat_tg = detect_category_for_lot(node_id=0, title="Telegram Premium Gift link 3 месяца")
        self.assertEqual(cat_tg.id, "tg_premium")

        cat_gpt = detect_category_for_lot(node_id=0, title="ChatGPT Plus Личный аккаунт")
        self.assertEqual(cat_gpt.id, "chatgpt")

    def test_get_all_target_node_ids(self):
        nodes = get_all_target_node_ids()
        for expected_node in [89, 923, 1355, 1391, 1568, 3559, 3734]:
            self.assertIn(expected_node, nodes)


class TestMultiCategoryCredentialExtractor(unittest.TestCase):
    def test_extract_discord_promo_link(self):
        text = "Ваша ссылка на дискорд: https://discord.com/billing/promotions/AbCdEfGh12345678"
        res = MultiCategoryCredentialExtractor.extract(text, category_id="discord")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "link")
        self.assertEqual(res["link"], "https://discord.com/billing/promotions/AbCdEfGh12345678")
        formatted = MultiCategoryCredentialExtractor.format_delivery_payload(res)
        self.assertIn("Discord Nitro", formatted)
        self.assertIn("https://discord.com/billing/promotions/", formatted)

    def test_extract_discord_gift_link(self):
        text = "https://discord.gift/XyZ12345Token"
        res = MultiCategoryCredentialExtractor.extract(text, category_id="discord")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "link")
        self.assertEqual(res["link"], "https://discord.gift/XyZ12345Token")

    def test_extract_tg_premium_gift(self):
        text = "Забирай премку: https://t.me/giftcode/testgiftcode12345"
        res = MultiCategoryCredentialExtractor.extract(text, category_id="tg_premium")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "link")
        self.assertEqual(res["link"], "https://t.me/giftcode/testgiftcode12345")
        formatted = MultiCategoryCredentialExtractor.format_delivery_payload(res)
        self.assertIn("Telegram Premium", formatted)
        self.assertIn("https://t.me/giftcode/", formatted)

    def test_extract_exitlag_license_key(self):
        text1 = "Ваш код активации ExitLag: EXIT-99AA-BB88-CC77"
        res1 = MultiCategoryCredentialExtractor.extract(text1, category_id="exitlag")
        self.assertIsNotNone(res1)
        self.assertEqual(res1["type"], "license_key")
        self.assertEqual(res1["key"], "EXIT-99AA-BB88-CC77")

        text2 = "Лицензия: AAAA-BBBB-CCCC-DDDD-EEEE"
        res2 = MultiCategoryCredentialExtractor.extract(text2, category_id="exitlag")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["type"], "license_key")
        self.assertEqual(res2["key"], "AAAA-BBBB-CCCC-DDDD-EEEE")
        formatted = MultiCategoryCredentialExtractor.format_delivery_payload(res2)
        self.assertIn("ExitLag", formatted)
        self.assertIn("AAAA-BBBB-CCCC-DDDD-EEEE", formatted)

    def test_extract_steam_account_4part(self):
        text = "steam_gamer_99:SteamP@ss123:mail_gamer@rambler.ru:RamblerP@ss"
        res = MultiCategoryCredentialExtractor.extract(text, category_id="steam")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "steam_account")
        self.assertEqual(res["login"], "steam_gamer_99")
        self.assertEqual(res["password"], "SteamP@ss123")
        self.assertEqual(res["mail_login"], "mail_gamer@rambler.ru")
        self.assertEqual(res["mail_password"], "RamblerP@ss")
        formatted = MultiCategoryCredentialExtractor.format_delivery_payload(res)
        self.assertIn("Steam", formatted)
        self.assertIn("steam_gamer_99", formatted)
        self.assertIn("mail_gamer@rambler.ru", formatted)

    def test_extract_steam_account_multiline_labels(self):
        text = (
            "Логин: my_steam_acc\n"
            "Пароль: StrongPassword1!\n"
            "Почта: steam_mail@mail.ru\n"
            "Пароль от почты: MailSecretPass\n"
        )
        res = MultiCategoryCredentialExtractor.extract(text, category_id="steam")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "steam_account")
        self.assertEqual(res["login"], "my_steam_acc")
        self.assertEqual(res["password"], "StrongPassword1!")
        self.assertEqual(res["mail_login"], "steam_mail@mail.ru")
        self.assertEqual(res["mail_password"], "MailSecretPass")

    def test_backward_compatible_chatgpt_extraction(self):
        text = "test_user@openai.com:SecretPlusPass123"
        res = CredentialExtractor.extract(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["login"], "test_user@openai.com")
        self.assertEqual(res["password"], "SecretPlusPass123")


class TestMultiCategoryDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = os.path.join(self.temp_dir.name, "test_multicat.db")
        self.db = Database(self.db_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_category_inventory_storage(self):
        uuid_steam = self.db.add_inventory(
            lot_id="fp_steam_1",
            title="Steam Авторег CS2",
            buy_price=20.0,
            sell_price=99.0,
            net_profit_expected=67.12,
            category_id="steam",
            node_id=89,
            item_type="steam_account",
            status="listed",
        )
        it = self.db.get_inventory_item(uuid_steam)
        self.assertIsNotNone(it)
        self.assertEqual(it["category_id"], "steam")
        self.assertEqual(it["node_id"], 89)
        self.assertEqual(it["item_type"], "steam_account")

    def test_get_inventory_by_buyer_order(self):
        uuid_item = self.db.add_inventory(
            lot_id="fp_tg_1",
            title="TG Premium",
            buy_price=200.0,
            sell_price=349.0,
            net_profit_expected=107.12,
            category_id="tg_premium",
            node_id=1391,
            status="sold",
            buyer_order_id="BUYER-ORD-999",
        )
        found = self.db.get_inventory_by_buyer_order("BUYER-ORD-999")
        self.assertIsNotNone(found)
        self.assertEqual(found["item_uuid"], uuid_item)
        self.assertIsNone(self.db.get_inventory_by_buyer_order("BUYER-ORD-NONE"))

    def test_get_inventory_for_order_fulfillment(self):
        u_steam = self.db.add_inventory(
            lot_id="s1", title="Steam 1", buy_price=20, sell_price=99, net_profit_expected=67,
            category_id="steam", node_id=89, status="listed",
        )
        u_disc = self.db.add_inventory(
            lot_id="d1", title="Discord 1", buy_price=150, sell_price=299, net_profit_expected=113,
            category_id="discord", node_id=923, status="ready_for_sale",
        )

        match_steam = self.db.get_inventory_for_order_fulfillment(category_id="steam", node_id=89)
        self.assertIsNotNone(match_steam)
        self.assertEqual(match_steam["item_uuid"], u_steam)

        match_disc = self.db.get_inventory_for_order_fulfillment(category_id="discord")
        self.assertIsNotNone(match_disc)
        self.assertEqual(match_disc["item_uuid"], u_disc)

        match_exitlag = self.db.get_inventory_for_order_fulfillment(category_id="exitlag")
        self.assertIsNone(match_exitlag)

    def test_category_toggles(self):
        self.assertTrue(self.db.is_category_enabled("steam"))
        new_state = self.db.toggle_category_enabled("steam")
        self.assertFalse(new_state)
        self.assertFalse(self.db.is_category_enabled("steam"))
        self.assertNotIn("steam", self.db.get_enabled_categories())

        self.db.set_category_enabled("steam", True)
        self.assertTrue(self.db.is_category_enabled("steam"))
        self.assertIn("steam", self.db.get_enabled_categories())

    def test_pnl_stats_by_category(self):
        u1 = self.db.add_inventory(
            "s1", "Steam", buy_price=20.0, sell_price=100.0, net_profit_expected=68.0,
            status="sold", category_id="steam",
        )
        self.db.update_inventory(u1, net_profit_realized=68.0)

        u2 = self.db.add_inventory(
            "d1", "Discord", buy_price=150.0, sell_price=300.0, net_profit_expected=114.0,
            status="sold", category_id="discord",
        )
        self.db.update_inventory(u2, net_profit_realized=114.0)

        verified_receipt(self.db,u1,88)
        verified_receipt(self.db,u2,264)
        pnl = self.db.get_pnl_stats()
        self.assertEqual(pnl["total_sold"], 2)
        self.assertEqual(pnl["total_net_profit"], 182.0)
        self.assertIn("by_category", pnl)
        self.assertEqual(pnl["by_category"]["steam"]["total_sold"], 1)
        self.assertEqual(pnl["by_category"]["steam"]["total_net_profit"], 68.0)
        self.assertEqual(pnl["by_category"]["discord"]["total_sold"], 1)
        self.assertEqual(pnl["by_category"]["discord"]["total_net_profit"], 114.0)


class TestMultiCategoryFlipperEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for key,value in (('is_emergency_stopped','0'),('flipper_mode','ASSIST'),('auto_buy','0')):
            db.set_setting(key,value)
        with db._get_connection() as conn:
            for table in ('purchase_intents','purchase_claims','trade_money_events','trade_candidates','flipper_inventory'):
                conn.execute('DELETE FROM '+table)
        self.engine = FlipperEngine()
        self.engine.dry_run = True
        self.engine.turbo_mode = False
        for cat_id in CATEGORY_REGISTRY.keys():
            db.set_category_enabled(cat_id, True)

    async def test_evaluate_candidate_deal_across_categories(self):
        eval_steam = self.engine.evaluate_candidate_deal(
            node_id=89,
            title="Steam Авторег Новые аккаунты с почтой",
            price=20.0,
            seller="SteamKing",
            seller_rating=5.0,
            seller_reviews=25,
        )
        self.assertFalse(eval_steam.is_eligible)
        self.assertIn("NEEDS_VERIFIED_DEMAND", eval_steam.rejection_reasons)
        self.assertIn("PURCHASE_CURRENCY_UNVERIFIED", eval_steam.rejection_reasons)
        self.assertEqual(eval_steam.category_id, "steam")
        self.assertEqual(eval_steam.expected_profit, 0.0)

        eval_steam_expensive = self.engine.evaluate_candidate_deal(
            node_id=89,
            title="Steam Авторег Дорогой",
            price=50.0,
            seller="SteamKing",
            seller_rating=5.0,
            seller_reviews=25,
        )
        self.assertFalse(eval_steam_expensive.is_eligible)
        self.assertIn("NEEDS_VERIFIED_DEMAND", eval_steam_expensive.rejection_reasons)
        self.assertEqual(eval_steam_expensive.expected_profit, 0.0)

        eval_exitlag = self.engine.evaluate_candidate_deal(
            node_id=1568,
            title="ExitLag ключ на 30 дней",
            price=50.0,
            seller="KeyMaster",
            seller_rating=4.9,
            seller_reviews=50,
        )
        self.assertFalse(eval_exitlag.is_eligible)
        self.assertIn("SUBSCRIPTIONS_EXCLUDED", eval_exitlag.rejection_reasons)
        self.assertEqual(eval_exitlag.category_id, "exitlag")

        db.set_category_enabled("cursor", False)
        eval_cursor_disabled = self.engine.evaluate_candidate_deal(
            node_id=3734,
            title="Cursor AI Pro Fast",
            price=50.0,
            seller="CodeSeller",
            seller_rating=5.0,
            seller_reviews=10,
        )
        self.assertFalse(eval_cursor_disabled.is_eligible)
        self.assertIn("CATEGORY_DISABLED", eval_cursor_disabled.rejection_reasons)
        db.set_category_enabled("cursor", True)

    async def test_multi_category_process_and_fulfillment_idempotency(self):
        self.engine.dry_run = False
        unique_steam_lot = f"fp_steam_{uuid.uuid4().hex[:8]}"
        unique_buyer_order = f"BUYER-ORDER-FIX-{uuid.uuid4().hex[:8]}"

        item_uuid = db.add_inventory(
            lot_id=unique_steam_lot,
            title="Steam Account Fresh",
            buy_price=20.0,
            sell_price=99.0,
            net_profit_expected=67.12,
            category_id="steam",
            node_id=89,
            status="listed",
        )
        db.update_inventory(item_uuid, credentials_parsed="LOGIN: steam_u\nPASS: steam_p")

        offer='EXACT-'+uuid.uuid4().hex[:8]
        db.update_inventory(item_uuid,is_dry_run=False,resale_lot_id=offer)
        with db._get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO inventory_checks VALUES(?,?,?,?)',(item_uuid,'test intake',0,12345))
        incoming_orders = [
            {
                "order_id": unique_buyer_order,
                "desc": "Steam Account Fresh",
                "buyer": "SteamGamer",
                "status": "Оплачен",
                "is_paid": True,
                "offer_id": offer,
            }
        ]

        with patch.object(self.engine.client, "fetch_incoming_orders", new_callable=AsyncMock) as mock_orders, \
             patch.object(self.engine.client, "send_chat_message", new_callable=AsyncMock) as mock_send:
            mock_orders.return_value = incoming_orders
            mock_send.return_value = {"success": True, "dry_run": False}

            count1 = await self.engine.check_and_fulfill_buyer_orders()
            self.assertEqual(count1, 1)
            self.assertEqual(mock_send.call_count, 1)

            sold_item = db.get_inventory_item(item_uuid)
            self.assertEqual(sold_item["status"], "delivered")
            self.assertEqual(sold_item["buyer_order_id"], unique_buyer_order)

            # Second run with same incoming buyer order must NOT deliver again!
            count2 = await self.engine.check_and_fulfill_buyer_orders()
            self.assertEqual(count2, 0)
            self.assertEqual(mock_send.call_count, 1)


class TestFlipperProxyResolution(unittest.TestCase):
    def test_proxy_helpers(self):
        # Check port open on invalid host should safely return False
        self.assertFalse(_is_port_open("127.0.0.1", 59999, timeout=0.1))

    def test_resolve_telegram_session_structure(self):
        session = resolve_telegram_session()
        self.assertIsNotNone(session)

    def test_create_bot_and_dispatcher(self):
        session = resolve_telegram_session()
        test_token = '123456:offline_fixture_not_a_live_bot_token'
        with patch('auto_flipper.bot.BOT_TOKEN', test_token):
            bot, dp = create_bot_and_dispatcher(session=session)
        self.assertEqual(bot.token, test_token)
        self.assertIsNotNone(dp)


class TestVulnerabilityFixesAndTargetCategories(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for key,value in (('is_emergency_stopped','0'),('flipper_mode','ASSIST'),('auto_buy','0')):
            db.set_setting(key,value)
        with db._get_connection() as conn:
            for table in ('purchase_intents','purchase_claims','trade_money_events','trade_candidates','flipper_inventory'):
                conn.execute('DELETE FROM '+table)
        self.engine = FlipperEngine()
        self.engine.dry_run = True
        self.engine.turbo_mode = False
        admins=patch("auto_flipper.database.ADMIN_IDS",[12345]);admins.start();self.addCleanup(admins.stop)

    def _create_mock_message(self, text="/start", user_id=999999):
        msg = AsyncMock()
        msg.text = text
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
        msg.from_user.username = "non_admin_user"
        msg.from_user.first_name = "Attacker"
        return msg

    def _create_mock_callback(self, data="flip_boost", user_id=999999):
        cb = AsyncMock()
        cb.data = data
        cb.from_user = MagicMock()
        cb.from_user.id = user_id
        cb.from_user.username = "non_admin_user"
        cb.from_user.first_name = "Attacker"
        cb.message = AsyncMock()
        return cb

    async def test_admin_access_control_denied_for_non_admin(self):
        non_admin_uid = 999999
        db.get_or_create_user(user_id=non_admin_uid, username="non_admin_user")
        db.set_user_admin(non_admin_uid, is_admin=False)
        self.assertFalse(db.is_user_admin(non_admin_uid))
        self.assertFalse(is_admin(non_admin_uid))

        # 1. Test protected commands
        for cmd_fn in (cmd_emergency_stop, cmd_resume, cmd_boost, cmd_browser, cmd_goal):
            msg = self._create_mock_message("/test", user_id=non_admin_uid)
            await cmd_fn(msg)
            self.assertTrue(msg.answer.called)
            call_text = msg.answer.call_args[0][0]
            self.assertIn("Доступ запрещен", call_text)

        # cmd_settings takes FSMContext
        msg_settings = self._create_mock_message("/settings", user_id=non_admin_uid)
        await cmd_settings(msg_settings, AsyncMock())
        self.assertIn("Доступ запрещен", msg_settings.answer.call_args[0][0])

        # 2. Test protected callbacks
        for cb_fn in (cb_raise_now, cb_toggle_turbo, cb_toggle_buy, cb_emergency_stop, cb_resume):
            cb = self._create_mock_callback("flip_test", user_id=non_admin_uid)
            await cb_fn(cb)
            self.assertTrue(cb.answer.called)
            answer_args = cb.answer.call_args
            self.assertIn("Доступ запрещен", answer_args[0][0])

    def test_strict_category_detection_and_no_chatgpt_fallback(self):
        unknown_cat = detect_category_for_lot(99999, "Random Strange Product 12345")
        self.assertIsNone(unknown_cat)

        eval_res = self.engine.evaluate_candidate_deal(
            node_id=99999,
            title="Random Strange Product 12345",
            price=100.0,
        )
        self.assertFalse(eval_res.is_eligible)
        self.assertEqual(eval_res.category_id, "unknown")
        self.assertTrue(any("NEEDS_EVIDENCE" in r for r in eval_res.rejection_reasons))

    def test_profit_and_condition_enforcement(self):
        # High margin (120%), but profit only 12 RUB (< 50 RUB minimum) -> MUST REJECT due to AND condition
        math_eval = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный",
            price=10.0,
            seller="SellerA",
            seller_rating=5.0,
            seller_reviews=20,
            market_median=30.0,
            price_floor=20.0,
            min_profit=50.0,
            min_margin_pct=35.0,
        )
        self.assertFalse(math_eval.is_eligible)
        self.assertTrue(any("Недостаточный профит" in r for r in math_eval.rejection_reasons))

        # Adequate profit and margin -> MUST ACCEPT
        math_eval_ok = ArbitrageMath.evaluate_deal(
            title="ChatGPT Plus Личный",
            price=250.0,
            seller="SellerA",
            seller_rating=5.0,
            seller_reviews=20,
            market_median=750.0,
            min_profit=100.0,
            min_margin_pct=30.0,
        )
        self.assertTrue(math_eval_ok.is_eligible)
        self.assertEqual(len(math_eval_ok.rejection_reasons), 0)

    async def test_purchase_intents_lifecycle_and_blocking(self):
        unique_lot = f"intent_test_{uuid.uuid4().hex[:8]}"
        intent_id = db.create_purchase_intent(
            lot_id=unique_lot,
            category_id="chatgpt",
            price=290.0,
        )
        self.assertTrue(intent_id.startswith("intent_"))

        intent = db.get_purchase_intent(intent_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["status"], "pending")

        unresolved = db.get_unresolved_purchase_intents()
        self.assertTrue(any(u["intent_id"] == intent_id for u in unresolved))

        # While intent is pending/unknown, process_candidate_deal must safely abort
        self.engine.auto_buy = True
        self.engine.is_emergency_stopped = False
        blocked_run = await self.engine.process_candidate_deal(
            lot_id="blocked_candidate",
            title="ChatGPT Plus Личный",
            price=280.0,
            seller="GoodSeller",
            seller_rating=5.0,
            seller_reviews=25,
        )
        self.assertIsNone(blocked_run)

        # Executed without a finalized inventory link still needs reconciliation
        db.update_purchase_intent(intent_id, status="executed", order_id="ORD-EXECUTED-99")
        unresolved_after = db.get_unresolved_purchase_intents()
        self.assertTrue(any(u["intent_id"] == intent_id for u in unresolved_after))

    def test_target_game_account_categories(self):
        for cid in ("cs2_prime", "valorant_ranked", "minecraft_pc"):
            self.assertIn(cid, CATEGORY_REGISTRY)

        cs = get_category_by_id("cs2_prime")
        self.assertEqual(cs.node_id, 1350)
        self.assertEqual(cs.item_type, "steam_account")
        self.assertEqual(get_category_by_node(1350).id, "cs2_prime")
        self.assertEqual(detect_category_for_lot(1350, "CS2 Prime Аккаунт Личный").id, "cs2_prime")

        val = get_category_by_id("valorant_ranked")
        self.assertEqual(val.node_id, 612)
        self.assertEqual(val.item_type, "riot_account")
        self.assertEqual(get_category_by_node(612).id, "valorant_ranked")
        self.assertEqual(detect_category_for_lot(612, "Valorant с рангом Gold").id, "valorant_ranked")

        mc = get_category_by_id("minecraft_pc")
        self.assertEqual(mc.node_id, 221)
        self.assertEqual(mc.item_type, "microsoft_account")
        self.assertEqual(get_category_by_node(221).id, "minecraft_pc")
        self.assertEqual(detect_category_for_lot(221, "Minecraft PC лицензия Java").id, "minecraft_pc")

        nodes = get_all_target_node_ids()
        for node in (1350, 612, 221):
            self.assertIn(node, nodes)

    def test_exact_resale_lot_matching_and_delivery_status_separation(self):
        item_a = db.add_inventory(
            lot_id=f"lot_a_{uuid.uuid4().hex[:6]}",
            title="Steam CS2 A",
            buy_price=20.0,
            sell_price=90.0,
            net_profit_expected=59.0,
            category_id="steam",
            node_id=89,
            status="listed",
        )
        resale_a = f"RESALE-A-{uuid.uuid4().hex[:6]}"
        db.update_inventory(item_a, resale_lot_id=resale_a, credentials_parsed="CREDS_A")

        item_b = db.add_inventory(
            lot_id=f"lot_b_{uuid.uuid4().hex[:6]}",
            title="Steam CS2 B",
            buy_price=20.0,
            sell_price=95.0,
            net_profit_expected=63.0,
            category_id="steam",
            node_id=89,
            status="listed",
        )
        resale_b = f"RESALE-B-{uuid.uuid4().hex[:6]}"
        db.update_inventory(item_b, resale_lot_id=resale_b, credentials_parsed="CREDS_B")

        # Specific match by resale_lot_id
        matched = db.get_inventory_for_order_fulfillment(category_id="steam", node_id=89, resale_lot_id=resale_b)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["item_uuid"], item_b)

        # Check status separation
        db.update_inventory(
            item_b,
            status="sold",
            delivery_status="delivered",
            settlement_status="unsettled_hold",
            buyer_order_id="BUYER-ORDER-EXACT-1",
        )
        stored_b = db.get_inventory_item(item_b)
        self.assertEqual(stored_b["status"], "sold")
        self.assertEqual(stored_b["delivery_status"], "delivered")
        self.assertEqual(stored_b["settlement_status"], "unsettled_hold")

    def test_ev_and_limits_calculations(self):
        ev_res = ArbitrageMath.calculate_ev_and_limits(
            buy_price=200.0,
            net_receipt=400.0,
            operating_cost=5.0,
            sale_probability=0.85,
        )
        self.assertGreater(ev_res["ev"], 0.0)
        self.assertGreater(ev_res["b_max"], 0.0)
        self.assertGreaterEqual(ev_res["p_min"], 0.0)
        self.assertLessEqual(ev_res["p_min"], 1.0)
        self.assertIn("a_coeff", ev_res)
        self.assertIn("k_coeff", ev_res)

    def test_atomic_inventory_reservation_prevents_double_fulfillment(self):
        item_uuid = db.add_inventory(
            lot_id=f"atomic_{uuid.uuid4().hex[:6]}",
            title="Atomic CS2 Account",
            buy_price=100.0,
            sell_price=300.0,
            net_profit_expected=155.0,
            category_id="cs2_prime",
            node_id=1350,
            status="listed",
            is_dry_run=False,
        )
        resale_id = f"RESALE-ATOMIC-{uuid.uuid4().hex[:6]}"
        db.update_inventory(item_uuid, resale_lot_id=resale_id, credentials_parsed="login:pass")

        with db._get_connection() as conn:
            conn.execute('INSERT INTO inventory_checks VALUES(?,?,?,?)',(item_uuid,'test intake',0,12345))
        # First reservation succeeds
        reserved_1 = db.reserve_inventory_for_fulfillment(
            category_id="cs2_prime",
            node_id=1350,
            buyer_order_id="BUYER-ORDER-FIRST",
            resale_lot_id=resale_id,
        )
        self.assertIsNotNone(reserved_1)
        self.assertEqual(reserved_1["item_uuid"], item_uuid)
        self.assertEqual(reserved_1["status"], "reserved")
        self.assertEqual(reserved_1["delivery_status"], "in_progress")
        self.assertEqual(reserved_1["buyer_order_id"], "BUYER-ORDER-FIRST")

        # Concurrent second reservation for same item/category returns None (double fulfillment prevented)
        reserved_2 = db.reserve_inventory_for_fulfillment(
            category_id="cs2_prime",
            node_id=1350,
            buyer_order_id="BUYER-ORDER-SECOND",
            resale_lot_id=resale_id,
        )
        self.assertIsNone(reserved_2)

    def test_strict_resale_lot_matching_returns_none(self):
        item_uuid = db.add_inventory(
            lot_id=f"strict_{uuid.uuid4().hex[:6]}",
            title="Minecraft PC Account",
            buy_price=150.0,
            sell_price=450.0,
            net_profit_expected=232.5,
            category_id="minecraft_pc",
            node_id=221,
            status="listed",
        )
        resale_id = f"RESALE-STRICT-A-{uuid.uuid4().hex[:6]}"
        db.update_inventory(item_uuid, resale_lot_id=resale_id)

        # Asking for an unrelated resale lot must NOT fall back to this item
        mismatch = db.get_inventory_for_order_fulfillment(
            category_id="minecraft_pc",
            node_id=221,
            resale_lot_id="RESALE-NONEXISTENT",
        )
        self.assertIsNone(mismatch)

        # Atomic reservation with mismatched resale lot also returns None
        mismatch_res = db.reserve_inventory_for_fulfillment(
            category_id="minecraft_pc",
            node_id=221,
            buyer_order_id="BUYER-MISMATCH",
            resale_lot_id="RESALE-NONEXISTENT",
        )
        self.assertIsNone(mismatch_res)

    def test_category_fee_rates_and_pnl_calculation(self):
        # Game accounts have 15% fee, digital goods/keys have 12% fee
        cs2 = get_category_by_id("cs2_prime")
        val = get_category_by_id("valorant_ranked")
        mc = get_category_by_id("minecraft_pc")
        gpt = get_category_by_id("chatgpt")
        disc = get_category_by_id("discord")

        self.assertEqual(cs2.fee_rate, 0.15)
        self.assertEqual(val.fee_rate, 0.15)
        self.assertEqual(mc.fee_rate, 0.15)
        self.assertEqual(gpt.fee_rate, 0.12)
        self.assertEqual(disc.fee_rate, 0.12)

        # Verify evaluate_candidate_deal uses category-specific fee rate
        eval_cs2 = self.engine.evaluate_candidate_deal(
            lot_id="deal_cs2",
            title="CS2 Prime",
            price=200.0,
            seller="SellerA",
            category_id="cs2_prime",
            node_id=1350,
        )
        expected_fee_cs2 = round(eval_cs2.sell_price * 0.15, 2)
        self.assertAlmostEqual(eval_cs2.platform_fee, expected_fee_cs2, places=2)

        # Add sold item for CS2 and verify db.get_pnl_stats() reflects 15% fee
        item_sold = db.add_inventory(
            lot_id=f"sold_{uuid.uuid4().hex[:6]}",
            title="CS2 Prime Sold",
            buy_price=200.0,
            sell_price=500.0,
            net_profit_expected=225.0,
            category_id="cs2_prime",
            node_id=1350,
            status="sold",
            buyer_order_id=f"ORD-SOLD-{uuid.uuid4().hex[:6]}",
        )
        pnl = db.get_pnl_stats()
        cat_pnl = pnl.get("by_category", {}).get("cs2_prime")
        self.assertIsNotNone(cat_pnl)
        self.assertEqual(cat_pnl["fee_rate_pct"], 15.0)

    def test_flipper_operating_modes_enforcement(self):
        # OBSERVE mode blocks automatic checkout
        self.engine.set_mode("OBSERVE")
        self.assertEqual(self.engine.mode, "OBSERVE")
        self.assertFalse(self.engine.auto_buy)

        # ASSIST mode blocks automatic checkout
        self.engine.set_mode("ASSIST")
        self.assertEqual(self.engine.mode, "ASSIST")
        self.assertFalse(self.engine.auto_buy)

        # LIMITED_AUTO activates auto_buy
        self.engine.set_mode("LIMITED_AUTO")
        self.assertEqual(self.engine.mode, "LIMITED_AUTO")
        self.assertTrue(self.engine.auto_buy)

        # PAUSED deactivates auto_buy
        self.engine.set_mode("PAUSED")
        self.assertEqual(self.engine.mode, "PAUSED")
        self.assertFalse(self.engine.auto_buy)

    def test_admin_auth_security_no_backdoor(self):
        fake_uid = 88888888
        self.assertFalse(db.is_user_admin(fake_uid))
        self.assertFalse(is_admin(fake_uid))

        # Legacy database grants do not bypass the explicit ADMIN_IDS allowlist
        db.get_or_create_user(user_id=fake_uid, username="test_admin")
        db.set_user_admin(fake_uid, is_admin=True)
        self.assertFalse(db.is_user_admin(fake_uid))
        self.assertFalse(is_admin(fake_uid))

        # Revoking admin works
        db.set_user_admin(fake_uid, is_admin=False)
        self.assertFalse(db.is_user_admin(fake_uid))
        self.assertFalse(is_admin(fake_uid))

    def test_reconciliation_warning_on_resume(self):
        intent_id = db.create_purchase_intent(
            lot_id=f"reconcile_{uuid.uuid4().hex[:6]}",
            category_id="cs2_prime",
            price=250.0,
        )
        db.update_purchase_intent(intent_id, status="unknown", error_message="Checkout timeout")

        resumed = self.engine.resume_from_emergency()
        self.assertIsInstance(resumed, int)
        self.assertGreaterEqual(resumed.get("unresolved_intents_count"), 1)
        self.assertTrue(any(u["intent_id"] == intent_id for u in resumed.get("unresolved_intents")))

        # Clean up intent
        db.update_purchase_intent(intent_id, status="failed", error_message="Resolved")

    def test_game_accounts_credential_extraction(self):
        # CS2 Prime (Steam format)
        raw_steam = "cs2_pro_user:P@ssw0rd123:cs2_mail@rambler.ru:MailPass#1"
        ext_cs2 = CredentialExtractor.extract_steam(raw_steam, category_id="cs2_prime")
        self.assertIsNotNone(ext_cs2)
        self.assertEqual(ext_cs2["category"], "cs2_prime")
        self.assertEqual(ext_cs2["login"], "cs2_pro_user")
        self.assertIn("CS2 Prime", ext_cs2["formatted"])

        # Valorant Ranked (Riot format)
        raw_riot = "Логин: riot_val_champ\nПароль: ValPass999\nПочта: val@outlook.com (пароль: ValMail#)"
        ext_val = CredentialExtractor.extract_game_account(raw_riot, category_id="valorant_ranked")
        self.assertIsNotNone(ext_val)
        self.assertEqual(ext_val["category"], "valorant_ranked")
        self.assertEqual(ext_val["login"], "riot_val_champ")
        self.assertIn("Valorant", ext_val["formatted"])

        # Minecraft PC (Microsoft format)
        raw_mc = "mc_miner_2026@hotmail.com:DiamondPick!2026"
        ext_mc = CredentialExtractor.extract_game_account(raw_mc, category_id="minecraft_pc")
        self.assertIsNotNone(ext_mc)
        self.assertEqual(ext_mc["category"], "minecraft_pc")
        self.assertEqual(ext_mc["login"], "mc_miner_2026@hotmail.com")
        self.assertIn("Minecraft", ext_mc["formatted"])

    async def test_mode_command_and_callbacks(self):
        msg = AsyncMock()
        msg.from_user = MagicMock()
        msg.from_user.id = 12345
        msg.answer = AsyncMock()

        await cmd_mode(msg)
        msg.answer.assert_called_once()
        text_arg = msg.answer.call_args[0][0]
        self.assertIn("Выбор режима работы", text_arg)

        cb = AsyncMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 12345
        cb.data = "flip_mode_menu"
        cb.message = AsyncMock()
        cb.answer = AsyncMock()

        await cb_mode_menu(cb)
        cb.message.edit_text.assert_called_once()
        self.assertIn("Выбор режима работы", cb.message.edit_text.call_args[0][0])

        cb_set = AsyncMock()
        cb_set.from_user = MagicMock()
        cb_set.from_user.id = 12345
        cb_set.data = "flip_set_mode_LIMITED_AUTO"
        cb_set.message = AsyncMock()
        cb_set.answer = AsyncMock()

        await cb_set_mode(cb_set)
        self.assertEqual(flipper_engine.mode, "LIMITED_AUTO")
        cb_set.answer.assert_called_once()


if __name__ == "__main__":
    unittest.main()

