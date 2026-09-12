"""Isolated SQLite tests for dynamic, evidence-backed capital."""
import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_flipper.capital_store import CapitalStore
from resale_intelligence.models.risk_gate import RiskSnapshot


class Harness(CapitalStore):
    def __init__(self, path):
        self.path, self._lock = path, threading.RLock()
        with self._get_connection() as conn:
            conn.executescript('''
                CREATE TABLE flipper_inventory(item_uuid TEXT PRIMARY KEY,lot_id TEXT,
                    order_id TEXT,buy_price REAL,status TEXT,is_dry_run INTEGER,category_id TEXT);
                CREATE TABLE trade_money_events(event_id TEXT PRIMARY KEY,item_uuid TEXT,
                    kind TEXT,amount INTEGER,source TEXT,recorded_at REAL);
                CREATE TABLE purchase_intents(intent_id TEXT PRIMARY KEY,lot_id TEXT,
                    order_id TEXT,price REAL,status TEXT);
                CREATE TABLE purchase_evidence(intent_id TEXT PRIMARY KEY,payload TEXT,review TEXT);
            ''')
            self.init_capital_schema(conn)

    def is_user_admin(self, actor):
        return actor == 7

    @contextlib.contextmanager
    def _get_connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class CapitalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Harness(str(Path(self.temp.name) / 'capital.db'))
        self.clock = patch('auto_flipper.capital_store.time.time', return_value=1000)
        self.now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def seed(self, dry=False):
        return self.db.seed_capital('1000', 'deposit:verified', 7, dry)

    def item(self, name='one', price=100, dry=False, supplier='supplier-A', category='tf2', route_cost=5):
        with self.db._get_connection() as conn:
            conn.execute('INSERT INTO flipper_inventory VALUES(?,?,?,?,?,?,?)',
                         (name, 'lot-'+name, 'order-'+name, price, 'bought', int(dry), category))
            conn.execute('INSERT INTO purchase_intents VALUES(?,?,?,?,?)',
                         ('intent-'+name, 'lot-'+name, 'order-'+name, price, 'completed'))
            conn.execute('INSERT INTO purchase_evidence VALUES(?,?,?)',
                         ('intent-'+name, '{}', json.dumps({'supplier_group': supplier, 'route_cost': route_cost})))
        self.event(name, 'purchase', -int(price*100))

    def event(self, name, kind, amount, event_id=None):
        with self.db._get_connection() as conn:
            conn.execute('INSERT INTO trade_money_events VALUES(?,?,?,?,?,?)',
                         (event_id or name+':'+kind, name, kind, amount, 'bank:verified', self.now.return_value))

    def test_unseeded_is_blocked(self):
        state = self.db.capital_snapshot(False)
        self.assertFalse(state['reconciliation_ok'])
        self.assertFalse(state['balance_verified'])
        self.assertEqual(state['available_cash'], 0)
        RiskSnapshot(**state)

    def test_seed_once_admin_and_strict_amounts(self):
        for value in [0, 1000.01, True, 'NaN', '1.001', -1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.seed_capital(value, 'deposit', 7, False)
        with self.assertRaises(PermissionError):
            self.db.seed_capital(1000, 'deposit', 8, False)
        with self.assertRaises(ValueError):
            self.db.seed_capital(1000, '', 7, False)
        state = self.seed()
        self.assertEqual(state['initial_capital'], 100000)
        self.assertFalse(state['balance_verified'])
        with self.assertRaises(ValueError):
            self.seed()

    def test_confirm_cash_limits_and_purchase_availability(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.db.confirm_cash(1000.01, 'balance', 7, False)
        state = self.db.confirm_cash(900, 'balance', 7, False, purchasing_available=False)
        self.assertTrue(state['balance_verified'])
        self.assertEqual(state['available_cash'], 90000)
        self.assertFalse(state['purchasing_available'])

    def test_proof_stales_and_same_second_event_invalidates(self):
        self.seed()
        self.db.confirm_cash(1000, 'balance', 7, False)
        self.now.return_value = 1061
        self.assertFalse(self.db.capital_status(False)['balance_verified'])
        self.now.return_value = 1000
        self.item()
        self.assertFalse(self.db.capital_status(False)['balance_verified'])
        with self.assertRaises(ValueError):
            self.db.confirm_cash(1000, 'balance', 7, False)
        self.assertTrue(self.db.confirm_cash(900, 'balance', 7, False)['balance_verified'])

    def test_open_inventory_reduces_cash_not_realized_capital(self):
        self.seed(True)
        self.item(dry=True)
        state = self.db.capital_status(True, 'supplier-A', 'tf2')
        self.assertEqual(state['ledger_cash'], 90000)
        self.assertEqual(state['conservative_equity'], 90000)
        self.assertEqual(state['realized_capital'], 100000)
        self.assertEqual(state['session_loss'], 0)
        self.assertEqual(state['open_risk'], 10500)
        self.assertEqual(state['operating_buffer'], 500)

    def test_realized_profit_grows_capital_then_refund_drawdown(self):
        self.seed(True)
        self.item(dry=True)
        self.event('one', 'receipt', 15000)
        state = self.db.capital_status(True)
        self.assertEqual(state['ledger_cash'], 105000)
        self.assertEqual(state['realized_capital'], 105000)
        self.assertEqual(state['capital_high_water'], 105000)
        self.assertEqual(state['open_risk'], 0)
        self.event('one', 'refund', -15000)
        state = self.db.capital_status(True)
        self.assertEqual(state['realized_capital'], 90000)
        self.assertEqual(state['session_loss'], 15000)
        self.assertEqual(self.db.capital_snapshot(True)['session_opening_equity'], 105000)

    def test_open_cost_is_realized_once_and_reservation_not_doubled(self):
        self.seed(True)
        self.item(dry=True)
        self.event('one', 'cost', -300)
        state = self.db.capital_status(True)
        self.assertEqual(state['realized_pnl'], -300)
        self.assertEqual(state['session_loss'], 300)
        self.assertEqual(state['open_risk'], 10500)
        self.assertEqual(state['operating_buffer'], 200)
        self.event('one', 'receipt', 12000)
        state = self.db.capital_status(True)
        self.assertEqual(state['realized_pnl'], 1700)
        self.assertEqual(state['ledger_cash'], 101700)

    def test_environments_separate(self):
        self.seed(False)
        self.seed(True)
        self.db.confirm_cash(1000, 'live:balance', 7, False)
        self.item(dry=True)
        self.event('one', 'receipt', 50000)
        self.assertEqual(self.db.capital_status(True)['realized_capital'], 140000)
        live = self.db.capital_status(False)
        self.assertEqual(live['available_cash'], 100000)
        self.assertTrue(live['balance_verified'])

    def test_supplier_category_and_unknown_exposure(self):
        self.seed(True)
        self.item(dry=True)
        self.assertEqual(self.db.capital_status(True, 'supplier-B', 'other')['supplier_exposure'], 0)
        self.assertEqual(self.db.capital_status(True, 'supplier-B', 'other')['category_exposure'], 0)
        with self.db._get_connection() as conn:
            conn.execute('DELETE FROM purchase_evidence')
        self.assertEqual(self.db.capital_status(True, 'supplier-B', 'other')['supplier_exposure'], 10000)

    def test_legacy_inventory_and_orphan_events_block(self):
        self.seed()
        self.item()
        with self.db._get_connection() as conn:
            conn.execute('DELETE FROM trade_money_events')
        self.assertFalse(self.db.capital_status(False)['reconciliation_ok'])
        self.event('orphan', 'cost', -100)
        self.assertIn('ORPHAN_MONEY_EVENT', self.db.capital_status(False)['errors'])

    def test_seed_cannot_reset_preexisting_ledger(self):
        self.item()
        with self.assertRaises(ValueError):
            self.seed()

    def test_pending_blocks_globally_and_conn_supports_atomic_snapshot(self):
        self.seed(True)
        with self.db._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO purchase_intents VALUES('pending','lot',NULL,99,'unknown')")
            state = self.db.capital_snapshot(True, conn=conn)
            self.assertTrue(state['unresolved_purchase'])
            self.assertEqual(state['pending_purchase_debits'], 9900)
            self.assertEqual(set(state), set(RiskSnapshot.__dataclass_fields__))

    def test_ledger_mutation_invalidates_balance_fingerprint(self):
        self.seed()
        self.item()
        self.db.confirm_cash(900, 'balance', 7, False)
        with self.db._get_connection() as conn:
            conn.execute("UPDATE trade_money_events SET source='different proof'")
        self.assertFalse(self.db.capital_status(False)['balance_verified'])

    def test_writeoff_realizes_principal_without_second_cash_debit(self):
        self.seed(True)
        self.item(price=70, dry=True)
        self.event('one', 'cost', -500)
        self.event('one', 'writeoff', 0)
        with self.db._get_connection() as conn:
            conn.execute("UPDATE flipper_inventory SET status='written_off'")
        state = self.db.capital_status(True, 'supplier-A')
        self.assertTrue(state['reconciliation_ok'], state['errors'])
        self.assertEqual(state['ledger_cash'], 92500)
        self.assertEqual(state['realized_capital'], 92500)
        self.assertEqual(state['session_loss'], 7500)
        self.assertEqual(state['open_risk'], 0)
        self.assertEqual(state['operating_buffer'], 0)
        self.assertTrue(state['supplier_quarantined'])
        self.assertTrue(self.db.capital_snapshot(True, 'supplier-A')['supplier_quarantined'])
        self.assertFalse(self.db.capital_status(True, 'supplier-B')['supplier_quarantined'])
        self.event('one', 'recovery', 1000)
        recovered = self.db.capital_status(True)
        self.assertEqual(recovered['ledger_cash'], 93500)
        self.assertEqual(recovered['realized_capital'], 93500)
        self.assertEqual(recovered['session_loss'], 6500)
        # Even corrupt duplicate evidence cannot debit cash/principal twice.
        self.event('one', 'writeoff', 0, event_id='duplicate-writeoff')
        duplicate = self.db.capital_status(True)
        self.assertFalse(duplicate['reconciliation_ok'])
        self.assertEqual(duplicate['ledger_cash'], 93500)
        self.assertEqual(duplicate['realized_capital'], 93500)

    def test_writeoff_requires_zero_cash_amount(self):
        self.seed(True)
        self.item(price=70, dry=True)
        for amount in [1, -7000]:
            self.event('one', 'writeoff', amount, event_id='bad-writeoff:'+str(amount))
        state = self.db.capital_status(True)
        self.assertFalse(state['reconciliation_ok'])
        self.assertIn('WRITEOFF_MUST_NOT_DEBIT_CASH', state['errors'])
        self.assertEqual(state['ledger_cash'], 93000)
        self.assertEqual(state['realized_capital'], 100000)

    def test_written_off_status_without_event_is_not_proof(self):
        self.seed(True)
        self.item(price=70, dry=True)
        with self.db._get_connection() as conn:
            conn.execute("UPDATE flipper_inventory SET status='written_off'")
        state = self.db.capital_status(True)
        self.assertIn('WRITEOFF_EVENT_MISSING', state['errors'])
        self.assertFalse(state['reconciliation_ok'])
        self.assertEqual(state['open_risk'], 7500)

    def test_actual_refund_quarantines_original_supplier_only(self):
        self.seed(True)
        self.item(price=70, dry=True)
        self.event('one', 'receipt', 9000)
        self.event('one', 'refund', -9000)
        self.assertTrue(self.db.capital_status(True, 'supplier-A')['supplier_quarantined'])
        self.assertFalse(self.db.capital_status(True, 'supplier-B')['supplier_quarantined'])
        with self.db._get_connection() as conn:
            conn.execute('DELETE FROM purchase_evidence')
        state = self.db.capital_status(True, 'supplier-B')
        self.assertTrue(state['unknown_loss_supplier'])
        self.assertFalse(state['supplier_quarantined'])


if __name__ == '__main__':
    unittest.main()
