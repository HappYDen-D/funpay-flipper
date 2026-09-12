"""Small offline regression set for the Gemini engineering audit."""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

_bootstrap = tempfile.TemporaryDirectory(prefix='engineering-bootstrap-')
os.environ['FLIPPER_DB_PATH'] = str(Path(_bootstrap.name)/'bootstrap.db')
from auto_flipper.database import Database
from auto_flipper.economics import kopecks, stored_kopecks
from auto_flipper.flipper_engine import FlipperEngine
from auto_flipper.funpay_client import FunPayClient
from auto_flipper.handlers import parse_goal_amount, format_boost_result, edit_text_if_changed, cb_browser_menu
from auto_flipper.bot import setup_bot_commands
from auto_flipper.funpay_transport import proxy_options, response_problem
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
import httpx


class EngineeringFixes(unittest.IsolatedAsyncioTestCase):
    def test_money_noise_is_tolerated_only_in_stored_float(self):
        self.assertEqual(stored_kopecks(349.9999999999),35000)
        for value in ('349.9999999999', Decimal('349.9999999999'), 349.999, True, float('nan')):
            with self.assertRaises(ValueError):
                stored_kopecks(value)
        with self.assertRaises(ValueError):
            kopecks(349.9999999999)

    def test_goal_whole_input_and_blocked_boost(self):
        self.assertEqual(parse_goal_amount('500 000'),Decimal('500000'))
        self.assertEqual(parse_goal_amount('1\u202f000,25'),Decimal('1000.25'))
        for value in ('NaN','500 abc','500 12','inf'):
            with self.assertRaises(ValueError):
                parse_goal_amount(value)
        output=format_boost_result({'success':False,'raise_info':{'success':False,'error':'MODE_BLOCKED'},'turbo_mode':False})
        self.assertNotIn('ВКЛЮЧЕН',output)

    async def test_menu_and_repeated_edit(self):
        bot=AsyncMock(); await setup_bot_commands(bot)
        commands={c.command for c in bot.set_my_commands.call_args.args[0]}
        self.assertTrue({'capital','prepare','asset_intake','manual_exit','settle','deadlines','alerts'} <= commands)
        message=AsyncMock()
        method=EditMessageText(chat_id=42,message_id=1,text='same')
        message.edit_text.side_effect=TelegramBadRequest(method=method,message='Bad Request: message is not modified')
        await edit_text_if_changed(message,'same')
        callback=AsyncMock(); callback.message=message
        with patch('auto_flipper.handlers.is_admin',return_value=True), patch(
                'auto_flipper.handlers.flipper_engine.client.get_account_info',
                new=AsyncMock(return_value={'session_status':'expired'})):
            await cb_browser_menu(callback)
        callback.answer.assert_awaited_once()
        message.edit_text.side_effect=TelegramBadRequest(method=method,message='Bad Request: chat not found')
        with self.assertRaises(TelegramBadRequest):
            await edit_text_if_changed(message,'same')

    def test_explicit_proxy_and_challenge_diagnosis(self):
        self.assertEqual(proxy_options(''),{'proxy':None,'trust_env':False})
        self.assertFalse(proxy_options('http://127.0.0.1:8080')['trust_env'])
        self.assertEqual(response_problem(httpx.Response(403,text='forbidden')),'HTTP_403_ACCESS_DENIED')
        self.assertEqual(response_problem(httpx.Response(200,headers={'cf-mitigated':'challenge'},text='challenge')),'CLOUDFLARE_CHALLENGE')

    async def test_scan_trade_items_and_alert_opt_in(self):
        with tempfile.TemporaryDirectory(prefix='engineering-scan-') as folder:
            db=Database(str(Path(folder)/'scan.db'))
            with patch('auto_flipper.flipper_engine.db',db), patch('auto_flipper.candidate_alerts.ADMIN_IDS',[42]):
                engine=FlipperEngine();engine.set_mode('OBSERVE');engine._bot=AsyncMock()
                payload=dict(lot_id='funpay_1',title='TF2 Ticket',price=70,currency='RUB',seller='seller',
                    seller_rating=5,seller_reviews=30,node_id=1808)
                engine.client.fetch_market_lots=AsyncMock(return_value=[payload])
                self.assertEqual(await engine.scan_and_autobuy_cycle(),[])
                self.assertIsNotNone(db.get_candidate('funpay_1'))
                self.assertEqual(db.get_inventory_list(),[])
                engine._bot.send_message.assert_not_awaited()
                db.set_setting('candidate_alerts','1')
                payload['price']=69
                await engine.scan_and_autobuy_cycle()
                await engine.scan_and_autobuy_cycle()
                engine._bot.send_message.assert_awaited_once()

    def test_load_dotenv_behavior(self):
        import auto_flipper.config as cfg
        with tempfile.TemporaryDirectory(prefix='dotenv-test-') as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text(
                "# Comment line\n"
                "TEST_VAR_A=hello_world\n"
                "TEST_VAR_B=\"quoted_string\"\n"
                "TEST_VAR_C='single_quoted'\n"
                "; Semicolon comment\n"
                "\n"
                "TEST_EXISTING=new_value\n",
                encoding="utf-8"
            )
            with patch.dict(os.environ, {"TEST_EXISTING": "original_value"}, clear=False):
                cfg.load_dotenv(env_file)
                self.assertEqual(os.environ.get("TEST_VAR_A"), "hello_world")
                self.assertEqual(os.environ.get("TEST_VAR_B"), "quoted_string")
                self.assertEqual(os.environ.get("TEST_VAR_C"), "single_quoted")
                self.assertEqual(os.environ.get("TEST_EXISTING"), "original_value")

            with patch.object(cfg, 'BASE_DIR', Path(folder)):
                with patch.dict(os.environ, {"FLIPPER_IGNORE_DOTENV": "true"}, clear=False):
                    os.environ.pop("TEST_VAR_A", None)
                    cfg.load_dotenv()
                    self.assertNotIn("TEST_VAR_A", os.environ)

            cfg.load_dotenv(Path(folder) / "non_existent.env")

    async def test_golden_key_telegram_safety(self):
        from auto_flipper.handlers import cb_edit_key_prompt, handle_key_input
        with tempfile.TemporaryDirectory(prefix='key-safety-') as folder:
            db = Database(str(Path(folder) / 'safety.db'))
            with patch('auto_flipper.handlers.db', db), patch('auto_flipper.handlers.is_admin', return_value=True):
                cb = AsyncMock()
                cb.from_user.id = 42
                state = AsyncMock()
                await cb_edit_key_prompt(cb, state)
                state.clear.assert_awaited_once()
                msg_text = cb.message.edit_text.call_args[0][0]
                self.assertIn('FUNPAY_GOLDEN_KEY', msg_text)
                self.assertIn('запрещена', msg_text)

                msg = AsyncMock()
                msg.from_user.id = 42
                msg.text = 'secret_golden_key_test_123456789'
                await handle_key_input(msg, state)
                self.assertIsNone(db.get_setting('golden_key'))

    def test_emergency_stop_propagation_to_risk_gate(self):
        from auto_flipper.trade_admission import check_capital
        from auto_flipper.safety import action_allowed
        from test_pilot_integration import evidence
        with tempfile.TemporaryDirectory(prefix='stop-prop-') as folder:
            db = Database(str(Path(folder) / 'stop.db'))
            with patch('auto_flipper.database.ADMIN_IDS', [42]):
                db.seed_capital('1000', 'test deposit', 42, True)
                review = evidence()

                # 1. Emergency stop OFF: regular assessment permitted
                db.set_setting('is_emergency_stopped', '0')
                snapshot_off = db.capital_snapshot(True)
                self.assertFalse(snapshot_off['emergency_stopped'])
                decision = check_capital(db, True, review, 'tf2_items', 70)
                self.assertTrue(decision.allowed)
                self.assertTrue(action_allowed('ASSIST', False, 'checkout'))

                # 2. Emergency stop ON: new purchase fail-closed in risk gate & action layer
                db.set_setting('is_emergency_stopped', '1')
                snapshot_on = db.capital_snapshot(True)
                self.assertTrue(snapshot_on['emergency_stopped'])
                with self.assertRaisesRegex(ValueError, 'EMERGENCY_STOP'):
                    check_capital(db, True, review, 'tf2_items', 70)
                self.assertFalse(action_allowed('ASSIST', True, 'checkout'))

                # Discrepancy test: passing emergency_stopped=False cannot override active DB kill-switch
                snapshot_discrepancy = db.capital_snapshot(True, emergency_stopped=False)
                self.assertTrue(snapshot_discrepancy['emergency_stopped'])
                with self.assertRaisesRegex(ValueError, 'EMERGENCY_STOP'):
                    check_capital(db, True, review, 'tf2_items', 70, emergency_stopped=False)

                # Action layer discrepancy: can_act is fail-closed even if in-memory attribute is out of sync
                from auto_flipper.flipper_engine import FlipperEngine
                engine = FlipperEngine()
                engine.mode = 'ASSIST'
                engine.is_emergency_stopped = False
                with patch('auto_flipper.assistant_engine.db', db):
                    self.assertFalse(engine.can_act('checkout'))

                # 3. Reconciliation of already existing transactions is preserved
                payload = {'lot_id': 'lot_reconcile', 'category_id': 'tf2_items', 'price': 70.0,
                           'title': 'TF2 Ticket', 'seller': 'Supplier'}
                db.observe_candidate(payload)
                db.review_candidate('lot_reconcile', review, 42)
                candidate = db.get_candidate('lot_reconcile')
                # Claim while temporarily OFF
                db.set_setting('is_emergency_stopped', '0')
                intent_id = db.claim_purchase('lot_reconcile', 'tf2_items', 70.0, True,
                                              candidate['reviewed_at'], candidate=candidate)
                self.assertIsNotNone(intent_id)
                # Re-engage emergency stop
                db.set_setting('is_emergency_stopped', '1')
                # Settle / reconcile existing intent under stop
                item_id = db.resolve_purchase(intent_id, {'outcome': 'paid', 'order_id': 'ORD-STOP-1',
                                                          'debit': 70.0, 'source': 'proof'}, 42)
                self.assertIsNotNone(item_id)

    async def test_mm2_node_925_fail_closed(self):
        from auto_flipper.categories import CATEGORY_REGISTRY, get_all_target_node_ids
        from test_pilot_integration import evidence
        self.assertTrue(CATEGORY_REGISTRY['mm2_items'].is_deprecated)
        self.assertIn('404', CATEGORY_REGISTRY['mm2_items'].deprecated_reason)
        # get_all_target_node_ids must not include node 925 by default
        self.assertNotIn(925, get_all_target_node_ids())

        with tempfile.TemporaryDirectory(prefix='mm2-test-') as folder:
            db = Database(str(Path(folder) / 'mm2.db'))
            with patch('auto_flipper.database.ADMIN_IDS', [42]), patch('auto_flipper.flipper_engine.db', db):
                engine = FlipperEngine()

                # Scanner excludes node 925
                engine.client.fetch_market_lots = AsyncMock(return_value=[])
                await engine.scan_and_autobuy_cycle()
                called_nodes = engine.client.fetch_market_lots.call_args[1].get('node_ids', [])
                self.assertNotIn(925, called_nodes)
                self.assertIn(1808, called_nodes)

                # Real purchase mode (dry_run = False) rejects deprecated MM2 in evaluation
                engine.dry_run = False
                eval_live = engine.evaluate_candidate_deal(category_id='mm2_items', price=50, currency='RUB', node_id=925)
                self.assertFalse(eval_live.is_eligible)
                self.assertIn('CATEGORY_DEPRECATED_FOR_REAL_PURCHASE', eval_live.rejection_reasons)

                # Real purchase mode rejects claim_purchase in database layer
                review = evidence('MM2:Icewing', 'bid-mm2', '125.00')
                payload = {'lot_id': 'lot_mm2_claim', 'category_id': 'mm2_items', 'price': 50.0,
                           'title': 'Icewing', 'seller': 'Supplier', 'node_id': 925}
                db.observe_candidate(payload)
                db.review_candidate('lot_mm2_claim', review, 42)
                candidate_mm2 = db.get_candidate('lot_mm2_claim')
                with self.assertRaisesRegex(ValueError, 'CATEGORY_DEPRECATED_FOR_REAL_PURCHASE'):
                    db.claim_purchase('lot_mm2_claim', 'mm2_items', 50.0, False,
                                      candidate_mm2['reviewed_at'], candidate=candidate_mm2)

                # Unknown category / node is also rejected
                eval_unknown = engine.evaluate_candidate_deal(category_id='unknown_cat', price=50, currency='RUB', node_id=99999)
                self.assertFalse(eval_unknown.is_eligible)

    async def test_enable_all_categories_excludes_deprecated(self):
        from auto_flipper.handlers import cb_enable_all_categories
        with tempfile.TemporaryDirectory(prefix='cat-safety-') as folder:
            db = Database(str(Path(folder) / 'cat.db'))
            db.set_category_enabled('mm2_items', False)
            with patch('auto_flipper.handlers.db', db), patch('auto_flipper.handlers.is_admin', return_value=True):
                cb = AsyncMock()
                cb.from_user.id = 42
                cb.message = AsyncMock()
                await cb_enable_all_categories(cb)
                self.assertFalse(db.is_category_enabled('mm2_items'))
                self.assertTrue(db.is_category_enabled('steam'))
                self.assertTrue(db.is_category_enabled('cs2_prime'))

    def test_mm2_fail_closed_configuration_and_fresh_db(self):
        from auto_flipper.categories import CATEGORY_REGISTRY
        self.assertIn('mm2_items', CATEGORY_REGISTRY)
        self.assertTrue(CATEGORY_REGISTRY['mm2_items'].is_deprecated)
        self.assertFalse(CATEGORY_REGISTRY['mm2_items'].enabled_default)

        with tempfile.TemporaryDirectory(prefix='mm2-fresh-') as folder:
            fresh_db = Database(str(Path(folder) / 'fresh.db'))
            self.assertFalse(fresh_db.is_category_enabled('mm2_items'))

    def test_golden_key_sqlite_purge_migration(self):
        with tempfile.TemporaryDirectory(prefix='purge-sec-') as folder:
            db_path = str(Path(folder) / 'secrets.db')
            test_db = Database(db_path)
            with test_db._lock, test_db._get_connection() as conn:
                conn.execute("INSERT OR REPLACE INTO flipper_settings (key, value) VALUES ('golden_key', 'raw_secret_key_12345')")
                conn.execute("INSERT OR REPLACE INTO flipper_settings (key, value) VALUES ('pilot_capital', '1000')")
                conn.commit()

            purged_count = test_db.purge_legacy_secrets()
            self.assertEqual(purged_count, 1)

            # Confirm secret is removed, but unrelated setting is preserved
            self.assertIsNone(test_db.get_setting('golden_key'))
            self.assertEqual(test_db.get_setting('pilot_capital'), '1000')

            # Idempotency check: second run purges 0
            self.assertEqual(test_db.purge_legacy_secrets(), 0)

            # Check that re-opening database triggers automatic purge migration
            with test_db._lock, test_db._get_connection() as conn:
                conn.execute("INSERT OR REPLACE INTO flipper_settings (key, value) VALUES ('golden_key', 'raw_secret_key_reinserted')")
                conn.commit()
            reopened_db = Database(db_path)
            self.assertIsNone(reopened_db.get_setting('golden_key'))

    def test_funpay_client_repr_and_single_source_key(self):
        client_with_key = FunPayClient(golden_key='super_secret_funpay_token_xyz')
        repr_text = repr(client_with_key)
        self.assertIn('golden_key=set', repr_text)
        self.assertNotIn('super_secret_funpay_token_xyz', repr_text)

        client_without_key = FunPayClient(golden_key='')
        self.assertIn('golden_key=empty', repr(client_without_key))

    async def test_toctou_emergency_stop_guard_before_checkout_post(self):
        client = FunPayClient(golden_key='test_key_abc')
        client.action_authorizer = lambda action: True
        client.checkout_quote_validator = lambda html, lot_id: {
            'currency': 'RUB',
            'debit': 100.0,
            'sku': 'SKU-12345',
            'lot_id': lot_id,
            'quantity': 1,
        }

        offer_html = '<html><input name="csrf_token" value="dummy_csrf_token"/></html>'

        # Case 1: emergency stop triggers right before POST -> FAILED, not UNKNOWN, no POST
        stop_active = False

        def check_stop():
            return stop_active

        client.emergency_stop_checker = check_stop
        post_called = False

        async def handler(request: httpx.Request):
            nonlocal post_called
            if request.method == 'GET':
                return httpx.Response(200, text=offer_html)
            if request.method == 'POST':
                post_called = True
                return httpx.Response(200, json={'redirect': 'https://funpay.com/orders/TEST12345/'})
            return httpx.Response(404)

        with patch.object(client, '_http_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            def preflight():
                nonlocal stop_active
                stop_active = True

            res = await client.checkout_lot('funpay_12345', 100.0, dry_run=False, preflight=preflight, expected_sku='SKU-12345')

            self.assertFalse(res['success'])
            self.assertEqual(res['status'], 'FAILED')
            self.assertIn('EMERGENCY_STOP_BEFORE_PAYMENT', res['error'])
            self.assertFalse(post_called, "Checkout HTTP POST must NEVER be called when emergency stop triggers before POST")

        # Case 2: network failure AFTER post is sent -> preserves UNKNOWN reconciliation status
        stop_active = False

        async def drop_handler(request: httpx.Request):
            if request.method == 'GET':
                return httpx.Response(200, text=offer_html)
            if request.method == 'POST':
                raise httpx.ConnectError("Network dropped during checkout POST")
            return httpx.Response(404)

        with patch.object(client, '_http_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(drop_handler))):
            res_drop = await client.checkout_lot('funpay_12345', 100.0, dry_run=False, expected_sku='SKU-12345')
            self.assertFalse(res_drop['success'])
            self.assertEqual(res_drop['status'], 'UNKNOWN')
            self.assertIn('External reconciliation required', res_drop['error'])


if __name__ == '__main__':
    unittest.main()
