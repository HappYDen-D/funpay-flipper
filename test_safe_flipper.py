"""Offline regression tests for financial loss and duplicate-operation scenarios."""
import asyncio
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Redirect import-time legacy database initialization before importing the bot.
_bootstrap = tempfile.TemporaryDirectory(prefix='flipper-tests-')
os.environ['FLIPPER_DB_PATH'] = str(Path(_bootstrap.name)/'bootstrap.db')

from auto_flipper.database import Database
from auto_flipper.flipper_engine import FlipperEngine
from auto_flipper.funpay_client import FunPayClient
from auto_flipper.math_engine import ArbitrageMath
from auto_flipper.economics import calculate
import httpx


def review_data():
    now = time.time()
    sku = 'CS2-Prime-EU-firstmail'
    return dict(sku=sku,source='offline verified bid fixture',supplier_group='supplier-a',
        purchase_currency='RUB',listing_price=150,route_cost=3,estimated_execution_seconds=30,
        product=dict(kind='account',sku=sku,expires_at=None,transfer_method='account_credentials',
                     transfer_ready_at=now-1),
        exit_quote=dict(schema_version=1,quote_id='test-bid-1',buyer_id='test-buyer',sku=sku,
            source='https://example.test/verified-bid',observed_at=now-1,expires_at=now+120,
            quantity=1,unit_net_receipt=150,settlement_currency='RUB',settlement_hours=1,
            transfer_verified=True,payout_verified=True,executable=True))


class EconomicsTests(unittest.TestCase):
    def test_canonical_hurdles_and_rounding(self):
        result = ArbitrageMath.calculate_ev_and_limits(700,1000,operating_cost=40,
            target_margin_rub=80,target_roi_rate=.15)
        self.assertEqual(result['ev'],100.16)
        self.assertEqual(result['b_max'],695.22)
        self.assertAlmostEqual(result['p_min'],.8092155194488079)
        self.assertFalse(result['meets_targets'])

    def test_late_refund_does_not_write_off_purchase_twice(self):
        result = calculate(buy_price=700,net_receipt=1000,operating_cost=40,
            sale_probability=1,early_defect_rate=0,late_defect_rate=1)
        self.assertEqual(result['ev'],-740)

    def test_general_refund_and_intake_branches(self):
        result = calculate(buy_price=100,net_receipt=200,sale_probability=1,
            early_defect_rate=0,late_defect_rate=1,late_fine=150,late_recovery_rate=.5)
        self.assertEqual(result['ev'],0)
        result = calculate(buy_price=100,net_receipt=200,intake_failure_rate=1,
            intake_recovery_rate=.5,intake_failure_cost=10)
        self.assertEqual(result['ev'],-60)

    def test_unreachable_probability_is_not_clamped(self):
        result = calculate(buy_price=1000,net_receipt=100,target_margin_rub=500)
        self.assertFalse(result['feasible'])
        self.assertTrue(result['p_min'] is None or result['p_min'] > 1)

    def test_invalid_inputs_rejected(self):
        for params in ({'buy_price':0},{'net_receipt':float('nan')},{'sale_probability':1.1},
                       {'intake_failure_rate':-.1},{'late_fine':-1}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                calculate(**dict({'buy_price':100,'net_receipt':200},**params))


class SafeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Database(str(Path(self.folder.name)/'test.db'))
        for target in ('auto_flipper.flipper_engine.db','auto_flipper.assistant_engine.db'):
            mock = patch(target,self.db);mock.start();self.addCleanup(mock.stop)
        admins = patch('auto_flipper.database.ADMIN_IDS',[42]);admins.start();self.addCleanup(admins.stop)
        self.db.seed_capital(1000,'offline initial capital',42,True)
        self.db.seed_capital(1000,'offline initial capital',42,False)
        self.db.confirm_cash(1000,'offline available cash',42,False)
        self.engine = FlipperEngine()
        self.engine.set_mode('ASSIST')
        self.engine.dry_run=True
        self.engine.max_budget=1500
        self.engine.min_profit=10
        self.engine.min_margin_pct=10
        self.db.set_category_enabled('cs2_prime',True)
        self.payload=dict(lot_id='funpay_100',title='CS2 Prime',price=70,seller='Seller',currency='RUB',
            seller_rating=5.0,seller_reviews=30,node_id=1350,category_id='cs2_prime',
            is_personal=True,is_plus=True)

    def candidate(self):
        self.db.observe_candidate(self.payload)
        self.db.review_candidate(self.payload['lot_id'],review_data(),42)
        return self.db.get_candidate(self.payload['lot_id'])

    def live_item(self,offer='501'):
        item = self.db.add_inventory(lot_id='lot-'+offer,title='Account',buy_price=700,sell_price=1500,
            net_profit_expected=400,status='listed',category_id='cs2_prime',node_id=1350,is_dry_run=False)
        self.db.update_inventory(item,resale_lot_id=offer,credentials_parsed='test credentials')
        with self.db._get_connection() as conn:
            conn.execute('INSERT INTO inventory_checks VALUES(?,?,?,?)',(item,'test intake',0,42))
        return item

    def test_first_visitor_never_becomes_admin(self):
        self.assertFalse(self.db.get_or_create_user(123)['is_admin'])
        self.assertFalse(self.db.is_user_admin(123))
        self.assertTrue(self.db.is_user_admin(42))

    def test_missing_review_blocks_large_naive_margin(self):
        result=self.engine.evaluate_candidate_deal(**self.payload)
        self.assertFalse(result.is_eligible)
        self.assertEqual(result.expected_profit,0)
        self.assertIn('NEEDS_VERIFIED_DEMAND',result.rejection_reasons)

    def test_negative_confirmed_exit_profit_blocks_purchase(self):
        review=review_data();review['exit_quote']['unit_net_receipt']=60
        result=self.engine.evaluate_candidate_deal(**self.payload,review=review)
        self.assertFalse(result.is_eligible)
        self.assertEqual(result.expected_profit,-13)
        self.assertTrue(any('net_profit:' in reason for reason in result.rejection_reasons))

    async def test_assist_and_duplicate_click(self):
        self.candidate()
        self.engine._process_post_purchase=AsyncMock()
        self.engine.client.checkout_lot=AsyncMock(return_value={'success':True,'order_id':'SIM-100'})
        item=await self.engine.approve_candidate('funpay_100',42)
        self.assertIsNotNone(item)
        self.assertEqual(self.db.get_inventory_item(item)['order_id'],'SIM-100')
        with self.assertRaises(ValueError):
            await self.engine.approve_candidate('funpay_100',42)
        self.engine.client.checkout_lot.assert_awaited_once()

    async def test_unknown_payment_blocks_other_lot(self):
        self.candidate()
        self.engine.client.checkout_lot=AsyncMock(return_value={'success':False,'status':'UNKNOWN'})
        self.assertIsNone(await self.engine.approve_candidate('funpay_100',42))
        self.assertEqual(self.db.get_unresolved_purchase_intents()[0]['status'],'unknown')
        self.assertIsNone(self.db.claim_purchase('other','cs2_prime',500,True))

    async def test_missing_order_is_unknown(self):
        self.candidate()
        self.engine.client.checkout_lot=AsyncMock(return_value={'success':True})
        await self.engine.approve_candidate('funpay_100',42)
        self.assertEqual(self.db.get_unresolved_purchase_intents()[0]['status'],'unknown')

    def test_executed_intent_still_blocks_after_restart(self):
        intent=self.db.claim_purchase('lot','cs2_prime',500,True)
        self.db.update_purchase_intent(intent,status='executed',order_id='100')
        restarted=Database(self.db.db_path)
        self.assertIsNone(restarted.claim_purchase('other','cs2_prime',500,True))

    async def test_observe_blocks_all_actions_and_still_scans(self):
        self.engine.set_mode('OBSERVE');self.engine.dry_run=False
        self.engine.client.fetch_market_lots=AsyncMock(return_value=[self.payload])
        self.engine.client.fetch_incoming_orders=AsyncMock()
        self.engine.client.save_offer=AsyncMock()
        self.engine.client.raise_lots=AsyncMock()
        await self.engine.scan_and_autobuy_cycle()
        await self.engine.check_and_fulfill_buyer_orders()
        await self.engine.check_orphan_bought_orders()
        await self.engine.execute_boost()
        self.assertIsNotNone(self.db.get_candidate('funpay_100'))
        self.engine.client.fetch_incoming_orders.assert_not_awaited()
        self.engine.client.save_offer.assert_not_awaited()
        self.engine.client.raise_lots.assert_not_awaited()

    def test_atomic_binding_across_connections(self):
        item=self.live_item()
        other=self.live_item('502')
        restarted=Database(self.db.db_path)
        first=self.db.reserve_inventory_for_fulfillment(resale_lot_id='501',buyer_order_id='BUY1')
        second=restarted.reserve_inventory_for_fulfillment(resale_lot_id='502',buyer_order_id='BUY1')
        self.assertEqual(first['item_uuid'],item)
        self.assertIsNone(second)
        self.assertIsNone(self.db.reserve_inventory_for_fulfillment(category_id='cs2_prime',buyer_order_id='BUY2'))

    async def test_unknown_delivery_stays_reserved(self):
        item=self.live_item();self.engine.dry_run=False
        self.engine.client.fetch_incoming_orders=AsyncMock(return_value=[dict(order_id='BUY1',is_paid=True,offer_id='501',node_id=1350)])
        self.engine.client.send_chat_message=AsyncMock(return_value={'success':False})
        await self.engine.check_and_fulfill_buyer_orders()
        await self.engine.check_and_fulfill_buyer_orders()
        row=self.db.get_inventory_item(item)
        self.assertEqual((row['status'],row['delivery_status'],row['buyer_order_id']),('reserved','unknown','BUY1'))
        self.engine.client.send_chat_message.assert_awaited_once()

    async def test_delivered_is_not_profit_settlement_and_refund_are_idempotent(self):
        item=self.live_item();self.engine.dry_run=False
        self.engine.client.fetch_incoming_orders=AsyncMock(return_value=[dict(order_id='BUY1',is_paid=True,offer_id='501',node_id=1350)])
        self.engine.client.send_chat_message=AsyncMock(return_value={'success':True})
        await self.engine.check_and_fulfill_buyer_orders()
        self.assertEqual(self.db.get_inventory_item(item)['net_profit_realized'],0)
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'],0)
        self.db.record_receipt(item,'receipt-1',1000,'statement',42)
        self.assertFalse(self.db.record_receipt(item,'receipt-1',1000,'statement',42))
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'],300)
        self.db.record_receipt(item,'refund-1',1000,'claim',42,refund=True)
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'],-700)

    async def test_stop_reports_remote_failure_and_resume_only_observes(self):
        self.live_item();self.engine.dry_run=False
        self.engine.client.deactivate_offer=AsyncMock(return_value=False)
        result=await self.engine.stop_and_deactivate()
        self.assertFalse(result['success']);self.assertEqual(result['failed_count'],1)
        self.engine.resume_from_emergency()
        self.assertEqual(self.engine.mode,'OBSERVE')

    async def test_regex_credentials_require_intake(self):
        item=self.live_item();self.engine.dry_run=False
        self.db.update_inventory(item,status='bought',order_id='ORDER1')
        self.engine.client.fetch_order_chat=AsyncMock(return_value=['user:pass123:mail@example.com:mailpass'])
        self.engine.client.save_offer=AsyncMock()
        with patch('auto_flipper.flipper_engine.asyncio.sleep',new=AsyncMock()):
            await self.engine._process_post_purchase(item,'ORDER1','CS2',1500,'cs2_prime')
        self.assertEqual(self.db.get_inventory_item(item)['status'],'awaiting_intake')
        self.engine.client.save_offer.assert_not_awaited()

    def test_simultaneous_checkout_claims_serialize(self):
        other=Database(self.db.db_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda pair: pair[0].claim_purchase(pair[1],'cs2_prime',500,True),
                                  [(self.db,'one'),(other,'two')]))
        self.assertEqual(sum(r is not None for r in results),1)

    async def test_live_order_waits_for_actual_debit(self):
        self.candidate();self.engine.dry_run=False
        self.engine.client.checkout_quote_validator=lambda html,lot: None
        self.engine.client.get_account_info=AsyncMock(return_value=dict(is_authenticated=True,balance_available=1000))
        self.engine.client.checkout_lot=AsyncMock(return_value=dict(success=True,order_id='LIVE-ORDER',price=70))
        item=await self.engine.approve_candidate('funpay_100',42)
        self.assertIsNone(item)
        self.assertIsNone(self.db.get_inventory_by_lot('funpay_100'))
        self.assertEqual(self.db.get_unresolved_purchase_intents()[0]['order_id'],'LIVE-ORDER')

    def test_reconcile_paid_restores_inventory_without_checkout(self):
        self.candidate()
        intent=self.db.claim_purchase('funpay_100','cs2_prime',700,True)
        self.db.update_purchase_intent(intent,status='unknown')
        item=self.db.resolve_purchase(intent,dict(outcome='paid',order_id='SIM-PAID',debit=705,source='order statement'),42)
        self.assertEqual(self.db.get_inventory_item(item)['buy_price'],705)
        self.assertFalse(self.db.get_unresolved_purchase_intents())

    def test_expenses_not_forgotten_on_refund(self):
        item=self.live_item()
        self.db.update_inventory(item,status='delivered')
        self.db.record_expense(item,'cost',40,'test expense',42)
        self.db.record_receipt(item,'receipt',1000,'test statement',42)
        self.db.record_receipt(item,'refund',1000,'test refund',42,refund=True)
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'],-740)
        self.assertFalse(self.db.record_expense(item,'cost',40,'test expense',42))

    def test_migration_keeps_legacy_rows_and_backups(self):
        import sqlite3
        from contextlib import closing
        path=Path(self.folder.name)/'old.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE legacy_evidence(note TEXT)')
            conn.execute("INSERT INTO legacy_evidence VALUES('keep')")
            conn.commit()
        Database(str(path))
        self.assertTrue(path.with_name('old.db.pre_safe_v1.bak').exists())
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('SELECT note FROM legacy_evidence').fetchone()[0],'keep')


class PaymentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def run_checkout(self,status=500,quote=100,allow=True):
        client=FunPayClient('test-key')
        client.action_authorizer=lambda action: allow
        client.checkout_quote_validator=lambda html,lot: dict(debit=quote,currency='RUB')
        client.get_csrf_token=AsyncMock(return_value='test-csrf')
        transport=AsyncMock()
        transport.get.return_value=httpx.Response(200,text='<html>checkout</html>')
        transport.post.return_value=httpx.Response(status,text='<html>unrecognized response</html>')
        with patch('auto_flipper.funpay_client.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=transport
            result=await client.checkout_lot('funpay_100',100,dry_run=False)
        return result,transport

    async def test_http_error_after_post_is_unknown(self):
        result,transport=await self.run_checkout()
        self.assertEqual(result['status'],'UNKNOWN')
        transport.post.assert_awaited_once()

    async def test_changed_quote_never_posts(self):
        result,transport=await self.run_checkout(quote=101)
        self.assertFalse(result['success'])
        transport.post.assert_not_awaited()

    async def test_mode_guard_is_checked_after_prefetch(self):
        result,transport=await self.run_checkout(allow=False)
        self.assertFalse(result['success'])
        transport.get.assert_awaited_once()
        transport.post.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
