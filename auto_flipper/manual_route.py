"""Evidence-only intake and transfer records for individually identified goods.

These methods reconcile actions performed by an operator. They neither transfer
an asset nor confirm receipt of money. The purchase review is immutable evidence,
not the current (possibly edited) market candidate.
"""
import json
import math
import sqlite3
import time
from datetime import datetime

from auto_flipper.economics import decimal


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} required')
    return value.strip()


class ManualRouteStore:
    def init_manual_schema(self, conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS manual_asset_intakes (
            item_uuid TEXT PRIMARY KEY, asset_id TEXT NOT NULL, sku TEXT NOT NULL,
            is_dry_run INTEGER NOT NULL CHECK(is_dry_run IN (0,1)),
            source TEXT NOT NULL, actor INTEGER NOT NULL, recorded_at REAL NOT NULL,
            UNIQUE(is_dry_run, asset_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS manual_exit_events (
            event_id TEXT PRIMARY KEY, item_uuid TEXT UNIQUE NOT NULL,
            buyer_id TEXT NOT NULL, quote_id TEXT NOT NULL, is_dry_run INTEGER NOT NULL,
            source TEXT NOT NULL, actor INTEGER NOT NULL, recorded_at REAL NOT NULL,
            quote_expired INTEGER NOT NULL, listing_deactivation_required INTEGER NOT NULL)''')

    @staticmethod
    def _manual_purchase_review(conn, item):
        rows = conn.execute('''SELECT e.review FROM purchase_evidence e
            JOIN purchase_intents p ON p.intent_id=e.intent_id
            JOIN purchase_claims c ON c.intent_id=p.intent_id
            WHERE p.order_id=? AND p.lot_id=? AND p.status='completed'
              AND c.dry_run=?''',
            (item['order_id'], item['lot_id'], item['is_dry_run'])).fetchall()
        if len(rows) != 1:
            raise ValueError('Exactly one original purchase review in this environment required')
        try:
            review = json.loads(rows[0]['review'])
            product = review['product']
            if not isinstance(product, dict) or product.get('kind') not in ('trade_item', 'permanent_key'):
                raise ValueError('Manual asset route requires a trade item or permanent key')
            sku = _text(review['sku'], 'Reviewed SKU')
            if product.get('sku') != sku or product.get('expires_at', 'missing') is not None:
                raise ValueError('Original product SKU and permanent entitlement required')
            return review
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Original purchase review is incomplete') from exc

    def record_asset_intake(self, item_id, asset_id, sku, source, actor):
        """Record inspected ownership of one asset; never generate credentials."""
        if not self.is_user_admin(actor):
            raise PermissionError('Admin required')
        asset_id, sku, source = (_text(asset_id, 'Asset id'), _text(sku, 'SKU'),
                                 _text(source, 'Intake evidence'))
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item_id,)).fetchone()
            if not item:
                raise ValueError('Unknown inventory item')
            review = self._manual_purchase_review(conn, item)
            if sku != review['sku']:
                raise ValueError('Received asset does not match purchased SKU')
            old = conn.execute('SELECT * FROM manual_asset_intakes WHERE item_uuid=?', (item_id,)).fetchone()
            if old:
                if (old['asset_id'], old['sku'], old['source'], old['is_dry_run']) != (
                        asset_id, sku, source, item['is_dry_run']):
                    raise ValueError('Conflicting intake replay')
                return False
            if item['status'] not in ('bought', 'awaiting_intake'):
                raise ValueError('Item is not awaiting asset intake')
            timestamp = time.time()
            try:
                conn.execute('INSERT INTO manual_asset_intakes VALUES(?,?,?,?,?,?,?)',
                             (item_id, asset_id, sku, item['is_dry_run'], source, actor, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError('Asset already assigned to inventory in this environment') from exc
            conn.execute('INSERT INTO inventory_checks VALUES(?,?,?,?)',
                         (item_id, source, timestamp, actor))
            conn.execute("UPDATE flipper_inventory SET status='ready_for_sale' WHERE item_uuid=?", (item_id,))
            return True

    def record_manual_exit(self, item_id, event_id, buyer_id, source, actor):
        """Record a completed transfer to the reserved buyer, without cash credit.

        A delayed transfer must still be reconciled even after the original quote
        expires; that fact is retained in the audit. This is not an authorization
        to initiate a new transfer at an expired price.
        """
        if not self.is_user_admin(actor):
            raise PermissionError('Admin required')
        event_id, buyer_id, source = (_text(event_id, 'Transfer event id'),
                                      _text(buyer_id, 'Buyer id'), _text(source, 'Transfer evidence'))
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item_id,)).fetchone()
            if not item:
                raise ValueError('Unknown inventory item')
            review = self._manual_purchase_review(conn, item)
            quote = review.get('exit_quote')
            if not isinstance(quote, dict) or quote.get('buyer_id') != buyer_id or quote.get('sku') != review['sku']:
                raise ValueError('Transfer must match the original reserved buyer and SKU')
            quote_id = _text(quote.get('quote_id'), 'Reserved quote id')
            old = conn.execute('SELECT * FROM manual_exit_events WHERE event_id=?', (event_id,)).fetchone()
            if old:
                if (old['item_uuid'], old['buyer_id'], old['source'], old['is_dry_run']) != (
                        item_id, buyer_id, source, item['is_dry_run']):
                    raise ValueError('Conflicting transfer event replay')
                return False
            intake = conn.execute('''SELECT * FROM manual_asset_intakes
                WHERE item_uuid=? AND is_dry_run=? AND sku=?''',
                (item_id, item['is_dry_run'], review['sku'])).fetchone()
            if not intake or item['status'] not in ('ready_for_sale', 'listed'):
                raise ValueError('Inspected, undelivered asset required')
            now = time.time()
            try:
                expires_at = decimal(quote.get('expires_at'))
            except ValueError as exc:
                raise ValueError('Original quote expiry required') from exc
            listing_open = bool(item['resale_lot_id'])
            conn.execute('INSERT INTO manual_exit_events VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (event_id, item_id, buyer_id, quote_id, item['is_dry_run'], source, actor,
                          now, int(decimal(now) > expires_at), int(listing_open)))
            conn.execute('''UPDATE flipper_inventory SET status='delivered',
                delivery_status='confirmed',buyer_order_id=?,buyer_username=? WHERE item_uuid=?''',
                (event_id, buyer_id, item_id))
            return True

    def inventory_deadlines(self, dry_run, now=None):
        """Report elapsed holding/settlement time; never assume a sale occurred."""
        if not isinstance(dry_run, bool):
            raise ValueError('Explicit dry_run environment required')
        timestamp = time.time() if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError('Finite timestamp required')
        with self._get_connection() as conn:
            rows = conn.execute('''SELECT item_uuid,category_id,status,bought_at,is_dry_run
                FROM flipper_inventory WHERE is_dry_run=?
                AND status NOT IN ('sold','refunded','cancelled','written_off') ORDER BY bought_at,item_uuid''',
                (int(dry_run),)).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            try:
                purchased_at = datetime.fromisoformat(row['bought_at']).timestamp()
                if purchased_at > timestamp:
                    raise ValueError('Future purchase time')
                age = (timestamp - purchased_at) / 3600
                entry.update(age_hours=round(age, 2), target_at=purchased_at + 24 * 3600,
                             deadline_at=purchased_at + 72 * 3600,
                             urgency='overdue' if age >= 72 else 'target_exceeded' if age >= 24 else 'tracking')
            except (TypeError, ValueError, OverflowError, OSError):
                entry.update(age_hours=None, target_at=None, deadline_at=None, urgency='unknown')
            entry['phase'] = 'settlement' if row['status'] == 'delivered' else 'inventory'
            result.append(entry)
        priorities = {'unknown': 0, 'overdue': 1, 'target_exceeded': 2, 'tracking': 3}
        return sorted(result, key=lambda entry: (priorities[entry['urgency']], -(entry['age_hours'] or 0)))
