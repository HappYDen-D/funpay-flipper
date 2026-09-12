"""Offline full-cycle checks: no test input is evidence of a real buyer."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from concurrent.futures import ThreadPoolExecutor

_bootstrap = tempfile.TemporaryDirectory(prefix='pilot-bootstrap-')
os.environ['FLIPPER_DB_PATH'] = str(Path(_bootstrap.name) / 'bootstrap.db')
from auto_flipper.database import Database
from auto_flipper.flipper_engine import FlipperEngine


def evidence(sku='TF2:725;6', quote='bid-1', net='95.00'):
    now = time.time()
    return dict(sku=sku, source='TEST ONLY purchase inspection', supplier_group='supplier-1',
        purchase_currency='RUB', route_cost='5.00', estimated_execution_seconds=30,
        product=dict(kind='trade_item', sku=sku, expires_at=None, transfer_method='manual', transfer_ready_at=now-1),
        exit_quote=dict(schema_version=1, quote_id=quote, sku=sku, buyer_id='buyer-1',
            source='TEST ONLY executable cash bid', observed_at=now, expires_at=now+300,
            quantity=1, unit_net_receipt=net, settlement_currency='RUB', settlement_hours=1,
            transfer_verified=True, payout_verified=True, executable=True))


class PilotIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Database(str(Path(self.folder.name) / 'pilot.db'))
        for target, value in [('auto_flipper.database.ADMIN_IDS', [42]),
                              ('auto_flipper.flipper_engine.db', self.db),
                              ('auto_flipper.assistant_engine.db', self.db)]:
            patched = patch(target, value); patched.start(); self.addCleanup(patched.stop)
        self.engine = FlipperEngine()
        self.engine.dry_run = True
        self.engine.set_mode('ASSIST')
        self.engine.min_profit = 10
        self.engine.min_margin_pct = 15
        self.db.seed_capital('1000', 'TEST seed', 42, True)

    def candidate(self, lot='funpay_1', price=70, review=None, category='tf2_items'):
        review = evidence() if review is None else review
        self.db.observe_candidate(dict(lot_id=lot, title='Ticket', price=price, seller='Supplier',
            seller_rating=5, seller_reviews=100, node_id=1808 if category=='tf2_items' else 925,
            category_id=category, currency='RUB'))
        self.db.review_candidate(lot, review, 42)
        return self.db.get_candidate(lot)

    def buy(self, lot='funpay_1', order='SIM-1'):
        intent = self.engine.prepare_manual_purchase(lot, 42)
        item = self.db.resolve_purchase(intent, dict(outcome='paid', order_id=order,
            debit=self.db.get_candidate(lot)['payload']['price'], source='TEST debit'), 42)
        return item

    def settle(self, item, sku='TF2:725;6', receipt=95):
        self.db.record_asset_intake(item, 'asset-'+item, sku, 'TEST inventory proof', 42)
        self.db.record_manual_exit(item, 'transfer-'+item, 'buyer-1', 'TEST completed transfer', 42)
        self.assertEqual(self.db.get_inventory_item(item)['net_profit_realized'], 0)
        self.db.record_expense(item, 'cost-'+item, 5, 'TEST actual expense', 42)
        self.db.record_receipt(item, 'cash-'+item, receipt, 'TEST cash receipt', 42)

    async def test_two_product_cycle_reinvests_real_profit(self):
        self.candidate(); item = self.buy(); self.settle(item)
        state = self.db.capital_status(True)
        self.assertEqual((state['realized_capital'], state['available_cash'], state['open_risk']), (102000,102000,0))
        # 96+5 exceeds the original 100 risk limit, fits the earned 102 limit.
        self.db.set_setting('category_enabled_mm2_items', '1')
        self.candidate('funpay_2', 96, evidence('MM2:Icewing', 'bid-2', '125'), 'mm2_items')
        second = self.buy('funpay_2', 'SIM-2'); self.settle(second, 'MM2:Icewing', 125)
        self.assertEqual(self.db.capital_status(True)['realized_capital'], 104400)
        self.assertEqual(self.db.capital_status(False)['initial_capital'], 0)

    async def test_open_inventory_and_promised_profit_cannot_fund_next_purchase(self):
        self.candidate(); self.buy()
        self.candidate('funpay_2', review=evidence(quote='bid-2'))
        with self.assertRaisesRegex(ValueError, 'TOTAL_OPEN_LOSS_LIMIT'):
            self.engine.prepare_manual_purchase('funpay_2', 42)

    async def test_live_receipt_requires_new_available_cash_proof(self):
        self.db.seed_capital(1000, 'TEST live seed', 42, False)
        self.db.confirm_cash(1000, 'TEST available cash', 42, False)
        self.engine.dry_run = False
        self.candidate(); item = self.buy(order='LIVE-1')
        self.settle(item)
        self.assertFalse(self.db.capital_status(False)['balance_verified'])
        self.db.confirm_cash(1020, 'TEST funds available after hold', 42, False)
        self.assertEqual(self.db.capital_status(False)['available_cash'], 102000)

    async def test_quote_capacity_consumed_even_after_successful_sale(self):
        self.candidate(); item = self.buy(); self.settle(item)
        self.candidate('funpay_2')
        with self.assertRaisesRegex(ValueError, 'CAPACITY_EXHAUSTED'):
            self.engine.prepare_manual_purchase('funpay_2', 42)

    async def test_concurrent_distinct_lots_cannot_share_one_reservation(self):
        first = self.candidate()
        second = self.candidate('funpay_2')
        other = Database(self.db.db_path)
        def claim(args):
            database, candidate = args
            return database.claim_purchase(candidate['lot_id'], 'tf2_items', 70, True,
                candidate['reviewed_at'], candidate=candidate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [(self.db,first), (other,second)]))
        self.assertEqual(sum(r is not None for r in results), 1)

    async def test_original_review_survives_candidate_replacement(self):
        self.candidate()
        intent = self.engine.prepare_manual_purchase('funpay_1', 42)
        self.candidate(review=evidence('WRONG-SKU', 'other-bid', '9999'))
        item = self.db.resolve_purchase(intent, dict(outcome='paid', order_id='SIM-1', debit=70, source='TEST debit'),42)
        self.db.record_asset_intake(item, 'asset-1', 'TF2:725;6', 'TEST proof', 42)

    async def test_bad_currency_subscription_and_stale_quote_block(self):
        candidate = self.candidate()
        candidate['payload']['currency'] = 'EUR'
        with self.assertRaisesRegex(ValueError, 'CURRENCY'):
            self.engine.evaluate_reviewed_candidate(candidate)
        candidate['payload']['currency'] = 'RUB'
        candidate['payload']['category_id'] = 'chatgpt'
        with self.assertRaisesRegex(ValueError, 'SUBSCRIPTIONS'):
            self.engine.evaluate_reviewed_candidate(candidate)
        bad = evidence(); bad['exit_quote']['observed_at'] -= 100
        with self.assertRaisesRegex(ValueError, 'VERIFIED_DEMAND'):
            self.db.review_candidate('funpay_1', bad, 42)

    async def test_trade_item_postpurchase_does_not_fabricate_credentials(self):
        self.candidate(); item = self.buy()
        self.engine.client.save_offer = AsyncMock()
        await self.engine._process_post_purchase(item, 'SIM-1', 'Ticket', 0, 'tf2_items')
        self.assertFalse(self.db.get_inventory_item(item)['credentials_parsed'])
        self.assertEqual(self.db.get_inventory_item(item)['status'], 'awaiting_intake')
        self.engine.client.save_offer.assert_not_awaited()

    async def test_writeoff_reduces_live_capital_and_profit_once(self):
        self.db.seed_capital(1000, 'TEST live seed', 42, False)
        self.db.confirm_cash(1000, 'TEST cash', 42, False)
        self.engine.dry_run = False
        self.candidate(); item = self.buy(order='LIVE-LOSS')
        self.db.record_expense(item, 'cost-1', 5, 'TEST cost', 42)
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'], -5)
        self.assertTrue(self.db.record_writeoff(item, 'loss-1', 'TEST unrecoverable asset', 42))
        self.assertFalse(self.db.record_writeoff(item, 'loss-1', 'TEST unrecoverable asset', 42))
        status = self.db.capital_status(False, supplier_group='supplier-1')
        self.assertEqual((status['realized_capital'], status['ledger_cash'], status['open_risk']), (92500,92500,0))
        self.assertTrue(status['supplier_quarantined'])
        self.assertEqual(self.db.get_pnl_stats()['total_net_profit'], -75)
        self.assertEqual(self.db.get_pnl_stats()['total_sold'], 0)
        self.assertEqual(self.db.inventory_deadlines(False), [])

    async def test_asset_is_not_reserved_by_credential_delivery(self):
        self.db.seed_capital(1000, 'TEST live seed', 42, False)
        self.db.confirm_cash(1000, 'TEST cash', 42, False)
        self.engine.dry_run = False
        self.candidate(); item = self.buy(order='LIVE-1')
        self.db.record_asset_intake(item, 'asset-1', 'TF2:725;6', 'TEST asset', 42)
        self.db.update_inventory(item, resale_lot_id='123', status='listed')
        self.assertIsNone(self.db.reserve_inventory_for_fulfillment(resale_lot_id='123', buyer_order_id='buyer-order'))
        self.assertEqual(self.db.get_inventory_item(item)['status'], 'listed')

    async def test_money_change_during_checkout_preflight_blocks(self):
        self.candidate(price=20); first = self.buy()
        self.candidate('funpay_2', price=65, review=evidence(quote='bid-2'))
        calls = []
        async def checkout(lot, price, *, dry_run, preflight, expected_sku):
            self.db.record_expense(first, 'late-cost', 1, 'TEST expense during fetch', 42)
            try:
                preflight()
            except ValueError as error:
                self.assertIn('CAPITAL_CHANGED', str(error))
                return {'success':False, 'status':'FAILED'}
            calls.append('POST')
            return {'success':True, 'order_id':'SIM-UNEXPECTED'}
        self.engine.client.checkout_lot = checkout
        self.assertIsNone(await self.engine.approve_candidate('funpay_2', 42))
        self.assertEqual(calls, [])
        self.assertFalse(self.db.get_unresolved_purchase_intents())


if __name__ == '__main__':
    unittest.main()
