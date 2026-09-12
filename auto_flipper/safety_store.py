"""Durable operations for the assistant. All monetary event amounts are kopecks."""
import json
import time
import uuid
from datetime import datetime
from auto_flipper.economics import kopecks, stored_kopecks
from auto_flipper.capital_store import CapitalStore
from auto_flipper.manual_route import ManualRouteStore


class SafetyStore(CapitalStore, ManualRouteStore):
    def init_safety_schema(self, conn):
        statements = (
            """CREATE TABLE IF NOT EXISTS trade_candidates (
                lot_id TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at REAL NOT NULL,
                review TEXT, reviewed_at REAL, reviewer INTEGER)""",
            """CREATE TABLE IF NOT EXISTS purchase_claims (
                lot_id TEXT NOT NULL, dry_run INTEGER NOT NULL, intent_id TEXT UNIQUE NOT NULL,
                PRIMARY KEY(lot_id, dry_run))""",
            """CREATE TABLE IF NOT EXISTS inventory_checks (
                item_uuid TEXT PRIMARY KEY, source TEXT NOT NULL, checked_at REAL NOT NULL,
                actor INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS verified_sale_bindings (
                order_id TEXT PRIMARY KEY, offer_id TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL, actor INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS trade_money_events (
                event_id TEXT PRIMARY KEY, item_uuid TEXT NOT NULL, kind TEXT NOT NULL,
                amount INTEGER NOT NULL, source TEXT NOT NULL, recorded_at REAL NOT NULL)""",
        )
        for sql in statements:
            conn.execute(sql)
        conn.execute('''CREATE TABLE IF NOT EXISTS purchase_evidence (
            intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL, review TEXT NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS exit_reservations (
            intent_id TEXT PRIMARY KEY, dry_run INTEGER NOT NULL, quote_id TEXT NOT NULL,
            buyer_id TEXT NOT NULL, sku TEXT NOT NULL, capacity INTEGER NOT NULL)''')
        self.init_capital_schema(conn)
        self.init_manual_schema(conn)

    def observe_candidate(self, payload):
        with self._lock, self._get_connection() as conn:
            previous = conn.execute('SELECT payload FROM trade_candidates WHERE lot_id=?',
                                    (payload['lot_id'],)).fetchone()
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            changed = previous and previous['payload'] != encoded
            conn.execute('''INSERT INTO trade_candidates(lot_id,payload,observed_at)
                VALUES(?,?,?) ON CONFLICT(lot_id) DO UPDATE SET
                payload=excluded.payload, observed_at=excluded.observed_at''',
                         (payload['lot_id'], encoded, time.time()))
            if changed:
                conn.execute('UPDATE trade_candidates SET review=NULL,reviewed_at=NULL,reviewer=NULL WHERE lot_id=?',
                             (payload['lot_id'],))

    def get_candidate(self, lot_id):
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM trade_candidates WHERE lot_id=?', (lot_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['payload'] = json.loads(result['payload'])
            result['review'] = json.loads(result['review']) if result['review'] else None
            return result

    def recent_candidates(self, limit=10):
        with self._get_connection() as conn:
            ids = conn.execute('SELECT lot_id FROM trade_candidates ORDER BY observed_at DESC LIMIT ?',
                               (limit,)).fetchall()
        return [self.get_candidate(row['lot_id']) for row in ids]

    def review_candidate(self, lot_id, review, actor):
        if not self.is_user_admin(actor):
            raise PermissionError('Admin required')
        from auto_flipper.safety import validate_review
        validate_review(review)
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute('UPDATE trade_candidates SET review=?,reviewed_at=?,reviewer=? WHERE lot_id=?',
                                  (json.dumps(review, allow_nan=False), time.time(), actor, lot_id))
            if cursor.rowcount != 1:
                raise ValueError('Unknown candidate')

    def claim_purchase(self, lot_id, category_id, price, dry_run, reviewed_at=None, *, candidate=None, emergency_stopped=None):
        """One unresolved checkout globally, and no automatic second attempt for a lot.

        BEGIN IMMEDIATE serializes the check and claim across processes. A failed
        or unknown attempt remains claimed until an explicit new business decision.
        """
        if not dry_run:
            from auto_flipper.categories import get_category_by_id
            cat = get_category_by_id(category_id)
            if not cat or getattr(cat, 'is_deprecated', False) or category_id == 'mm2_items' or getattr(cat, 'node_id', 0) == 925:
                raise ValueError('CATEGORY_DEPRECATED_FOR_REAL_PURCHASE')
        kopecks(price)
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if reviewed_at is not None and conn.execute(
                'SELECT 1 FROM trade_money_events WHERE recorded_at>? LIMIT 1',(reviewed_at,)).fetchone():
                return None
            if conn.execute("SELECT 1 FROM purchase_intents WHERE status IN ('pending','unknown','executed') LIMIT 1").fetchone():
                return None
            if conn.execute('SELECT 1 FROM purchase_claims WHERE lot_id=? AND dry_run=?',
                            (lot_id, int(dry_run))).fetchone():
                return None
            if conn.execute('SELECT 1 FROM flipper_inventory WHERE lot_id=? AND is_dry_run=?',
                            (lot_id, int(dry_run))).fetchone():
                return None
            intent_id = uuid.uuid4().hex
            if candidate is not None:
                if (candidate['lot_id'] != lot_id or candidate['payload']['lot_id'] != lot_id
                        or candidate['payload'].get('category_id') != category_id
                        or kopecks(candidate['payload']['price']) != kopecks(price)):
                    raise ValueError('PURCHASE_DOES_NOT_MATCH_REVIEW')
                # Review identity, quote capacity and cash are checked in the same write transaction.
                current = conn.execute('SELECT * FROM trade_candidates WHERE lot_id=?', (lot_id,)).fetchone()
                if (not current or current['reviewed_at'] != candidate['reviewed_at']
                        or json.loads(current['payload']) != candidate['payload']
                        or json.loads(current['review'] or 'null') != candidate['review']):
                    raise ValueError('REVIEW_CHANGED')
                from auto_flipper.trade_admission import check_capital
                from auto_flipper.exit_policy import evaluate_exit
                review = candidate['review']
                result = evaluate_exit(review, price, min_profit=10, min_roi=.15)
                if not result['admitted']:
                    raise ValueError('; '.join(result['reasons']))
                check_capital(self, dry_run, review, category_id, price, conn=conn, emergency_stopped=emergency_stopped)
                quote = review['exit_quote']
                used = conn.execute('''SELECT COUNT(*) FROM exit_reservations r
                    JOIN purchase_intents p USING(intent_id)
                    WHERE r.dry_run=? AND r.quote_id=? AND p.status!='failed' ''',
                    (int(dry_run), quote['quote_id'])).fetchone()[0]
                previous = conn.execute('SELECT * FROM exit_reservations WHERE dry_run=? AND quote_id=? LIMIT 1',
                    (int(dry_run), quote['quote_id'])).fetchone()
                if previous and (previous['buyer_id'], previous['sku'], previous['capacity']) != (
                        quote['buyer_id'], review['sku'], quote['quantity']):
                    raise ValueError('CONFLICTING_QUOTE_ID')
                if used >= quote['quantity']:
                    raise ValueError('EXIT_QUOTE_CAPACITY_EXHAUSTED')
                conn.execute('INSERT INTO exit_reservations VALUES(?,?,?,?,?,?)',
                    (intent_id, int(dry_run), quote['quote_id'], quote['buyer_id'], review['sku'], quote['quantity']))
                conn.execute('INSERT INTO purchase_evidence VALUES(?,?,?)',
                    (intent_id, json.dumps(candidate['payload']), json.dumps(review)))
            else:
                # Preserve the original snapshot for manual reconciliation of internal/legacy claims too.
                original = conn.execute('SELECT payload,review FROM trade_candidates WHERE lot_id=?', (lot_id,)).fetchone()
                if original and original['review']:
                    conn.execute('INSERT INTO purchase_evidence VALUES(?,?,?)', (intent_id, original['payload'], original['review']))
            now = datetime.now().isoformat()
            conn.execute('INSERT INTO purchase_claims VALUES(?,?,?)', (lot_id, int(dry_run), intent_id))
            conn.execute('''INSERT INTO purchase_intents
                (intent_id,lot_id,category_id,price,status,created_at,updated_at)
                VALUES(?,?,?,?,'pending',?,?)''', (intent_id, lot_id, category_id, price, now, now))
            return intent_id

    def complete_purchase(self, intent_id, order_id, payload, evaluation, dry_run):
        """Inventory, cash debit and completed intent commit together."""
        if not order_id:
            raise ValueError('Missing confirmed purchase order')
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            intent = conn.execute('SELECT * FROM purchase_intents WHERE intent_id=?', (intent_id,)).fetchone()
            existing = conn.execute('SELECT item_uuid FROM flipper_inventory WHERE order_id=? AND is_dry_run=?',
                                    (order_id, int(dry_run))).fetchone()
            if not intent or intent['status'] not in ('pending', 'unknown', 'executed', 'completed'):
                raise ValueError('Invalid purchase transition')
            if existing:
                if intent['order_id'] != order_id:
                    raise ValueError('Purchase order already bound')
                return existing['item_uuid']
            from auto_flipper.categories import CATEGORY_REGISTRY
            cat = CATEGORY_REGISTRY[intent['category_id']]
            item_id, now = 'flip_'+uuid.uuid4().hex[:12], datetime.now().isoformat()
            conn.execute('''INSERT INTO flipper_inventory
                (item_uuid,lot_id,order_id,title,seller,buy_price,sell_price,net_profit_expected,
                status,category_id,node_id,item_type,is_dry_run,bought_at)
                VALUES(?,?,?,?,?,?,?,?,'bought',?,?,?,?,?)''',
                (item_id, intent['lot_id'], order_id, payload['title'], payload['seller'], intent['price'],
                 evaluation.sell_price, evaluation.expected_profit, cat.id, cat.node_id, cat.item_type, int(dry_run), now))
            conn.execute("UPDATE purchase_intents SET status='completed',order_id=?,updated_at=? WHERE intent_id=?",
                         (order_id, now, intent_id))
            conn.execute('INSERT INTO trade_money_events VALUES(?,?,?,?,?,?)',
                         ('buy:'+intent_id,item_id,'purchase',-stored_kopecks(intent['price']),order_id,time.time()))
            return item_id

    def record_intake(self, item_id, source, actor):
        if not self.is_user_admin(actor) or not source.strip():
            raise ValueError('Admin and intake evidence required')
        with self._lock, self._get_connection() as conn:
            row = conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item_id,)).fetchone()
            if not row or row['status'] != 'awaiting_intake' or not row['credentials_parsed']:
                raise ValueError('Item is not awaiting intake')
            conn.execute('INSERT OR REPLACE INTO inventory_checks VALUES(?,?,?,?)',
                         (item_id,source,time.time(),actor))
            conn.execute("UPDATE flipper_inventory SET status='ready_for_sale' WHERE item_uuid=?", (item_id,))

    def bind_sale(self, order_id, offer_id, source, actor):
        if not self.is_user_admin(actor) or not source.strip():
            raise ValueError('Admin and order evidence required')
        with self._lock,self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            items=conn.execute("SELECT item_uuid FROM flipper_inventory WHERE resale_lot_id=? AND is_dry_run=0 AND status='listed'",(offer_id,)).fetchall()
            if len(items)!=1:
                raise ValueError('Exactly one live listed instance required')
            conn.execute('INSERT INTO verified_sale_bindings VALUES(?,?,?,?)',(order_id,offer_id,source,actor))

    def get_verified_offer_for_order(self, order_id):
        with self._get_connection() as conn:
            row=conn.execute('SELECT offer_id FROM verified_sale_bindings WHERE order_id=?',(order_id,)).fetchone()
            return row['offer_id'] if row else None

    def record_receipt(self, item_id, event_id, amount, source, actor, refund=False):
        """Manual verified settlement/refund, idempotent by external event id."""
        if not self.is_user_admin(actor) or not source.strip() or not event_id.strip():
            raise ValueError('Admin, event id and statement source required')
        value = kopecks(amount) * (-1 if refund else 1)
        if value == 0:
            raise ValueError('Receipt/refund must be positive; use writeoff for a total loss')
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item_id,)).fetchone()
            if not item or item['status'] not in ('delivered', 'sold', 'refunded'):
                raise ValueError('No delivered sale to settle')
            old = conn.execute('SELECT * FROM trade_money_events WHERE event_id=?', (event_id,)).fetchone()
            kind = 'refund' if refund else 'receipt'
            if old:
                if old['item_uuid'] != item_id or old['amount'] != value or old['kind'] != kind:
                    raise ValueError('Conflicting event replay')
                return False
            if refund and not conn.execute("SELECT 1 FROM trade_money_events WHERE item_uuid=? AND kind='receipt'",(item_id,)).fetchone():
                raise ValueError('Reconcile original receipt before its refund')
            if not refund and conn.execute("SELECT 1 FROM trade_money_events WHERE item_uuid=? AND kind='receipt'",(item_id,)).fetchone():
                raise ValueError('Receipt already recorded')
            conn.execute('INSERT INTO trade_money_events VALUES(?,?,?,?,?,?)',
                         (event_id,item_id,kind,value,source,time.time()))
            received = conn.execute("SELECT COALESCE(SUM(amount),0) FROM trade_money_events WHERE item_uuid=? AND kind IN ('receipt','refund','cost')", (item_id,)).fetchone()[0]
            conn.execute('''UPDATE flipper_inventory SET status=?,settlement_status=?,net_profit_realized=?
                WHERE item_uuid=?''', ('refunded' if refund else 'sold', 'refunded' if refund else 'settled',
                                      (received-stored_kopecks(item['buy_price']))/100, item_id))
            if not refund:
                conn.execute('UPDATE flipper_inventory SET sold_at=COALESCE(sold_at,?) WHERE item_uuid=?',
                             (datetime.now().isoformat(), item_id))
            return True

    def resolve_purchase(self, intent_id, proof, actor):
        """Manual reconciliation records evidence; never sends a checkout request."""
        if not self.is_user_admin(actor) or not proof.get('source','').strip():
            raise ValueError('Admin and transaction evidence required')
        intent = self.get_purchase_intent(intent_id)
        if not intent or intent['status'] not in ('pending','unknown','executed'):
            raise ValueError('No unresolved intent')
        if proof.get('outcome') == 'not_paid':
            if intent.get('order_id'):
                raise ValueError('Known purchase order must be reconciled, not discarded')
            self.update_purchase_intent(intent_id,status='failed',error_message=proof['source'])
            return None
        if proof.get('outcome') != 'paid' or not proof.get('order_id'):
            raise ValueError('Provide paid/order_id/debit or not_paid')
        actual = kopecks(proof['debit'])
        if actual <= 0:
            raise ValueError('Confirmed purchase debit must be positive')
        with self._get_connection() as conn:
            original = conn.execute('SELECT payload,review FROM purchase_evidence WHERE intent_id=?', (intent_id,)).fetchone()
        candidate = {'payload': json.loads(original['payload']), 'review': json.loads(original['review'])} if original else None
        if not candidate or not candidate['review']:
            raise ValueError('Original reviewed candidate missing; manual migration required')
        from auto_flipper.math_engine import ArbitrageEvaluation
        with self._lock,self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            claim = conn.execute('SELECT dry_run FROM purchase_claims WHERE intent_id=?',(intent_id,)).fetchone()
            if not claim:
                raise ValueError('Legacy intent has no environment proof')
            current=conn.execute('SELECT status,order_id FROM purchase_intents WHERE intent_id=?',(intent_id,)).fetchone()
            if current['status'] not in ('pending','unknown','executed') or (current['order_id'] and current['order_id']!=proof['order_id']):
                raise ValueError('Conflicting reconciliation')
            conn.execute("UPDATE purchase_intents SET price=?,order_id=?,status='executed',error_message=? WHERE intent_id=?",
                         (actual/100,proof['order_id'],proof['source'],intent_id))
        review = candidate['review']
        quoted_net = kopecks(review['exit_quote']['unit_net_receipt'])
        route_cost = kopecks(review['route_cost'])
        evaluation=ArbitrageEvaluation(is_eligible=False,sell_price=float(review.get('listing_price',0)),
            expected_profit=(quoted_net-actual-route_cost)/100,ev_rub=0)
        return self.complete_purchase(intent_id,proof['order_id'],candidate['payload'],evaluation,bool(claim['dry_run']))

    def record_expense(self, item_id, event_id, amount, source, actor):
        if not self.is_user_admin(actor) or not source.strip() or not event_id.strip():
            raise ValueError('Admin and expense evidence required')
        value=-kopecks(amount)
        with self._lock,self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item=conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?',(item_id,)).fetchone()
            if not item:
                raise ValueError('Unknown inventory item')
            old=conn.execute('SELECT * FROM trade_money_events WHERE event_id=?',(event_id,)).fetchone()
            if old:
                if (old['item_uuid'],old['kind'],old['amount'])!=(item_id,'cost',value):
                    raise ValueError('Conflicting expense replay')
                return False
            conn.execute('INSERT INTO trade_money_events VALUES(?,?,?,?,?,?)',
                         (event_id,item_id,'cost',value,source,time.time()))
            if item['settlement_status'] in ('settled','refunded'):
                conn.execute('UPDATE flipper_inventory SET net_profit_realized=net_profit_realized+? WHERE item_uuid=?',
                             (value/100,item_id))
            return True

    def record_writeoff(self, item_id, event_id, source, actor):
        """Recognize a verified total loss; purchase cash was already debited."""
        if not self.is_user_admin(actor) or not source.strip() or not event_id.strip():
            raise ValueError('Admin, event identity and loss evidence required')
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM trade_money_events WHERE event_id=?', (event_id,)).fetchone()
            if previous:
                if (previous['item_uuid'], previous['kind'], previous['amount']) != (item_id,'writeoff',0):
                    raise ValueError('Conflicting loss event')
                return False
            item = conn.execute('SELECT * FROM flipper_inventory WHERE item_uuid=?', (item_id,)).fetchone()
            if not item or item['status'] in ('sold','refunded','written_off'):
                raise ValueError('Open inventory required')
            if item['resale_lot_id']:
                raise ValueError('Reconcile and deactivate the listing before recording a loss')
            paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM trade_money_events WHERE item_uuid=? AND kind='purchase'", (item_id,)).fetchone()[0]
            if paid != -stored_kopecks(item['buy_price']):
                raise ValueError('Original purchase debit must be reconciled')
            conn.execute('INSERT INTO trade_money_events VALUES(?,?,?,?,?,?)', (event_id,item_id,'writeoff',0,source,time.time()))
            total = conn.execute('SELECT SUM(amount) FROM trade_money_events WHERE item_uuid=?', (item_id,)).fetchone()[0]
            conn.execute("UPDATE flipper_inventory SET status='written_off',settlement_status='settled',net_profit_realized=? WHERE item_uuid=?", (total/100,item_id))
            return True
