"""Isolated SQLite checks for evidence-only asset handling; no bot globals."""
import contextlib
import json
import sqlite3
import threading
import unittest
from datetime import datetime

from auto_flipper.manual_route import ManualRouteStore


class Store(ManualRouteStore):
    def __init__(self):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE flipper_inventory(item_uuid TEXT PRIMARY KEY,order_id TEXT,
                lot_id TEXT,is_dry_run INTEGER,status TEXT,category_id TEXT,bought_at TEXT,
                resale_lot_id TEXT,delivery_status TEXT,settlement_status TEXT,
                buyer_order_id TEXT,buyer_username TEXT,credentials_parsed TEXT);
            CREATE TABLE purchase_intents(intent_id TEXT PRIMARY KEY,order_id TEXT,
                lot_id TEXT,status TEXT);
            CREATE TABLE purchase_claims(intent_id TEXT PRIMARY KEY,dry_run INTEGER);
            CREATE TABLE purchase_evidence(intent_id TEXT PRIMARY KEY,payload TEXT,review TEXT);
            CREATE TABLE inventory_checks(item_uuid TEXT PRIMARY KEY,source TEXT,
                checked_at REAL,actor INTEGER);
            CREATE TABLE trade_money_events(event_id TEXT PRIMARY KEY,amount INTEGER);
        ''')
        self.init_manual_schema(self.conn)
        self.conn.commit()

    @contextlib.contextmanager
    def _get_connection(self):
        with self.conn:
            yield self.conn

    def is_user_admin(self, actor):
        return actor == 42

    def seed(self, item='one', dry_run=True, sku='tf2:725;6', kind='trade_item', hours=1, status='bought'):
        # Reuse the same order id deliberately: environment + lot provenance matter.
        review = {'sku': sku, 'product': {'sku': sku, 'kind': kind, 'expires_at': None},
                  'exit_quote': {'quote_id': 'quote-'+item, 'sku': sku,
                                 'buyer_id': 'buyer', 'expires_at': 2_000_000_000}}
        with self.conn:
            self.conn.execute('''INSERT INTO flipper_inventory
                (item_uuid,order_id,lot_id,is_dry_run,status,category_id,bought_at,settlement_status)
                VALUES(?,?,?,?,?,?,?,?)''',
                (item, 'order', 'lot-'+item, int(dry_run), status, 'tf2',
                 datetime.fromtimestamp(1_800_000_000-hours*3600).isoformat(), 'pending'))
            self.conn.execute('INSERT INTO purchase_intents VALUES(?,?,?,?)',
                              ('intent-'+item, 'order', 'lot-'+item, 'completed'))
            self.conn.execute('INSERT INTO purchase_claims VALUES(?,?)', ('intent-'+item, int(dry_run)))
            self.conn.execute('INSERT INTO purchase_evidence VALUES(?,?,?)',
                              ('intent-'+item, '{}', json.dumps(review)))
        return item

    def item(self, item='one'):
        return self.conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item,)).fetchone()


class ManualRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = Store()
        self.addCleanup(self.db.conn.close)
        self.db.seed()

    def intake(self, item='one', asset='asset-one', sku='tf2:725;6'):
        return self.db.record_asset_intake(item, asset, sku, 'Steam inventory receipt', 42)

    def transfer(self, item='one', event='trade-one', buyer='buyer'):
        return self.db.record_manual_exit(item, event, buyer, 'Steam trade receipt', 42)

    def test_intake_and_transfer_do_not_fabricate_credentials_or_money(self):
        self.assertTrue(self.intake())
        self.assertEqual(self.db.item()['status'], 'ready_for_sale')
        self.assertIsNone(self.db.item()['credentials_parsed'])
        self.assertTrue(self.transfer())
        item = self.db.item()
        self.assertEqual(item['status'], 'delivered')
        self.assertEqual(item['settlement_status'], 'pending')
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM trade_money_events').fetchone()[0], 0)

    def test_admin_and_evidence_required(self):
        with self.assertRaises(PermissionError):
            self.db.record_asset_intake('one', 'asset', 'tf2:725;6', 'proof', 43)
        for source in ('', ' ', None):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.db.record_asset_intake('one', 'asset', 'tf2:725;6', source, 42)

    def test_wrong_sku_or_buyer_cannot_progress(self):
        with self.assertRaises(ValueError):
            self.intake(sku='tf2:5021;6')
        self.intake()
        with self.assertRaises(ValueError):
            self.transfer(buyer='wrong-buyer')
        self.assertEqual(self.db.item()['status'], 'ready_for_sale')

    def test_exact_replays_are_idempotent_conflicting_replays_rejected(self):
        self.assertTrue(self.intake())
        self.assertFalse(self.intake())
        with self.assertRaises(ValueError):
            self.intake(asset='different-asset')
        self.assertTrue(self.transfer())
        self.assertFalse(self.transfer())
        self.assertFalse(self.intake())
        with self.assertRaises(ValueError):
            self.transfer(event='different-event')

    def test_asset_and_transfer_cannot_be_assigned_to_two_live_items(self):
        self.db.seed('two')
        self.intake()
        with self.assertRaises(ValueError):
            self.intake('two')
        self.intake('two', 'asset-two')
        self.transfer()
        with self.assertRaises(ValueError):
            self.transfer('two')
        self.assertEqual(self.db.item('two')['status'], 'ready_for_sale')

    def test_simulated_asset_is_separate_and_cannot_reuse_transfer_event(self):
        self.db.seed('live', dry_run=False)
        self.intake()
        self.intake('live')
        self.transfer()
        with self.assertRaises(ValueError):
            self.transfer('live')
        self.assertTrue(self.transfer('live', 'live-event'))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM manual_asset_intakes').fetchone()[0], 2)

    def test_mismatched_purchase_environment_cannot_supply_evidence(self):
        with self.db.conn:
            self.db.conn.execute('UPDATE purchase_claims SET dry_run=0')
        with self.assertRaises(ValueError):
            self.intake()

    def test_transfer_requires_intake(self):
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.db.item()['status'], 'bought')

    def test_subscription_and_expiring_entitlement_rejected(self):
        self.db.seed('subscription', kind='subscription')
        with self.assertRaises(ValueError):
            self.intake('subscription')
        self.db.seed('code', kind='permanent_key')
        self.assertTrue(self.intake('code', 'code-fingerprint'))
        review = json.loads(self.db.conn.execute(
            'SELECT review FROM purchase_evidence WHERE intent_id=?', ('intent-one',)).fetchone()[0])
        review['product']['expires_at'] = 1_900_000_000
        with self.db.conn:
            self.db.conn.execute('UPDATE purchase_evidence SET review=? WHERE intent_id=?',
                                 (json.dumps(review), 'intent-one'))
        with self.assertRaises(ValueError):
            self.intake()

    def test_delayed_completed_transfer_keeps_expiry_audit(self):
        self.intake()
        review = json.loads(self.db.conn.execute(
            'SELECT review FROM purchase_evidence WHERE intent_id=?', ('intent-one',)).fetchone()[0])
        review['exit_quote']['expires_at'] = 1
        with self.db.conn:
            self.db.conn.execute('UPDATE purchase_evidence SET review=? WHERE intent_id=?',
                                 (json.dumps(review), 'intent-one'))
        self.transfer()
        self.assertEqual(self.db.conn.execute('SELECT quote_expired FROM manual_exit_events').fetchone()[0], 1)

    def test_listed_asset_transfer_records_required_listing_cleanup(self):
        self.intake()
        with self.db.conn:
            self.db.conn.execute("UPDATE flipper_inventory SET status='listed',resale_lot_id='offer'")
        self.transfer()
        self.assertEqual(self.db.conn.execute(
            'SELECT listing_deactivation_required FROM manual_exit_events').fetchone()[0], 1)

    def test_decimal_string_quote_expiry_is_accepted(self):
        self.intake()
        review = json.loads(self.db.conn.execute(
            'SELECT review FROM purchase_evidence WHERE intent_id=?', ('intent-one',)).fetchone()[0])
        review['exit_quote']['expires_at'] = '2000000000.25'
        with self.db.conn:
            self.db.conn.execute('UPDATE purchase_evidence SET review=? WHERE intent_id=?',
                                 (json.dumps(review), 'intent-one'))
        self.assertTrue(self.transfer())

    def test_invalid_quote_expiry_cannot_be_recorded(self):
        self.intake()
        review = json.loads(self.db.conn.execute(
            'SELECT review FROM purchase_evidence WHERE intent_id=?', ('intent-one',)).fetchone()[0])
        for expiry in (True, 'NaN', 'Infinity', None):
            review['exit_quote']['expires_at'] = expiry
            with self.db.conn:
                self.db.conn.execute('UPDATE purchase_evidence SET review=? WHERE intent_id=?',
                                     (json.dumps(review), 'intent-one'))
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.transfer()

    def test_deadlines_include_unsettled_delivery_and_exact_boundaries(self):
        self.db.seed('target', hours=24)
        self.db.seed('overdue', hours=72)
        self.db.seed('unsettled', hours=73, status='delivered')
        self.db.seed('closed', hours=100, status='sold')
        self.db.seed('writeoff', hours=100, status='written_off')
        self.db.seed('live', dry_run=False, hours=74)
        self.db.seed('invalid', hours=-1)
        items = {item['item_uuid']: item for item in self.db.inventory_deadlines(True, now=1_800_000_000)}
        self.assertEqual(items['one']['urgency'], 'tracking')
        self.assertEqual(items['target']['urgency'], 'target_exceeded')
        self.assertEqual(items['overdue']['urgency'], 'overdue')
        self.assertEqual(items['unsettled']['phase'], 'settlement')
        self.assertEqual(items['invalid']['urgency'], 'unknown')
        self.assertNotIn('closed', items)
        self.assertNotIn('writeoff', items)
        self.assertNotIn('live', items)
        self.assertEqual(len(self.db.inventory_deadlines(False, now=1_800_000_000)), 1)


if __name__ == '__main__':
    unittest.main()
