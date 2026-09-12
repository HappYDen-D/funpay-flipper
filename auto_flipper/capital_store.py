"""Ledger-backed dynamic capital, separate for real trading and simulations.

The initial deposit is entered once (at most 1,000 RUB); realized profit can grow
capital beyond it. Unsold stock has zero equity value, while its full cost remains
exposure. Live purchasing cash needs a fresh independent balance confirmation.
"""
import hashlib
import json
import math
import time
from decimal import Decimal, InvalidOperation
from auto_flipper.economics import stored_kopecks


def _rubles(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Expected rubles, not boolean or container')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Expected nonnegative rubles with at most two decimals')
        return int(amount * 100)
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('Invalid rubles') from error


def _environment(dry_run):
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be boolean')
    return int(dry_run)


class CapitalStore:
    def init_capital_schema(self, conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS capital_seed (
            dry_run INTEGER PRIMARY KEY CHECK(dry_run IN (0,1)),
            amount INTEGER NOT NULL CHECK(amount>0 AND amount<=100000),
            source TEXT NOT NULL, actor INTEGER NOT NULL, recorded_at REAL NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS capital_cash_proof (
            dry_run INTEGER PRIMARY KEY CHECK(dry_run IN (0,1)),
            amount INTEGER NOT NULL CHECK(amount>=0), source TEXT NOT NULL,
            actor INTEGER NOT NULL, recorded_at REAL NOT NULL,
            ledger_fingerprint TEXT NOT NULL, purchasing_available INTEGER NOT NULL)''')

    def _capital_authority(self, source, actor):
        if type(actor) is not int or not self.is_user_admin(actor):
            raise PermissionError('Explicit administrator required')
        if not isinstance(source, str) or not source.strip():
            raise ValueError('Balance/deposit evidence source required')

    def seed_capital(self, amount, source, actor, dry_run):
        self._capital_authority(source, actor)
        environment, value = _environment(dry_run), _rubles(amount)
        if not 0 < value <= 100000:
            raise ValueError('Initial deposit must be above zero and at most 1000 RUB')
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM capital_seed WHERE dry_run=?', (environment,)).fetchone():
                raise ValueError('Initial capital already recorded; it cannot be reset')
            if conn.execute('''SELECT 1 FROM trade_money_events e JOIN flipper_inventory i
                               ON i.item_uuid=e.item_uuid WHERE i.is_dry_run=? LIMIT 1''',
                            (environment,)).fetchone():
                raise ValueError('Seed must precede ledger events; existing history needs reconciliation')
            conn.execute('INSERT INTO capital_seed VALUES(?,?,?,?,?)',
                         (environment, value, source.strip(), actor, time.time()))
        return self.capital_status(dry_run)

    def confirm_cash(self, amount, source, actor, dry_run, purchasing_available=True):
        self._capital_authority(source, actor)
        environment, value = _environment(dry_run), _rubles(amount)
        if type(purchasing_available) is not bool:
            raise ValueError('Purchasing availability must be explicitly boolean')
        with self._lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            status = self.capital_status(dry_run, conn=conn)
            if not status['seeded'] or not status['reconciliation_ok']:
                raise ValueError('Seeded and reconciled ledger required')
            if value > status['ledger_cash']:
                raise ValueError('Confirmed cash cannot exceed ledger-derived cash')
            conn.execute('''INSERT INTO capital_cash_proof VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(dry_run) DO UPDATE SET amount=excluded.amount,source=excluded.source,
                actor=excluded.actor,recorded_at=excluded.recorded_at,
                ledger_fingerprint=excluded.ledger_fingerprint,
                purchasing_available=excluded.purchasing_available''',
                         (environment, value, source.strip(), actor, time.time(),
                          status['ledger_fingerprint'], int(purchasing_available)))
        return self.capital_status(dry_run)

    def capital_status(self, dry_run, supplier_group='', category_id='', conn=None, emergency_stopped=None):
        """Amounts are integer kopecks; ``conn`` permits atomic admission checks.

        Session opening equity is the high water of realized capital, never the
        marked value of unsold inventory. Loss is drawdown from that high water.
        Missing historical purchase debits block admission rather than inventing
        cash. Supplier identity comes from the immutable purchase review; unknown
        identity counts towards every supplier's exposure limit.
        """
        environment = _environment(dry_run)
        if conn is None:
            with self._get_connection() as connection:
                return self.capital_status(dry_run, supplier_group, category_id, conn=connection, emergency_stopped=emergency_stopped)
        now = time.time()
        seed = conn.execute('SELECT * FROM capital_seed WHERE dry_run=?', (environment,)).fetchone()
        proof = conn.execute('SELECT * FROM capital_cash_proof WHERE dry_run=?', (environment,)).fetchone()
        items = [dict(row) for row in conn.execute(
            'SELECT * FROM flipper_inventory WHERE is_dry_run=?', (environment,))]
        events = [dict(row) for row in conn.execute('''SELECT e.rowid AS event_rowid,e.*
            FROM trade_money_events e JOIN flipper_inventory i ON i.item_uuid=e.item_uuid
            WHERE i.is_dry_run=? ORDER BY e.rowid''', (environment,))]
        errors = []
        if not seed:
            errors.append('INITIAL_CAPITAL_MISSING')
        initial = seed['amount'] if seed else 0
        # Include event contents, not just timestamps: two events may share a second.
        fingerprint = hashlib.sha256(json.dumps(events, sort_keys=True, separators=(',', ':'),
                                                  default=str).encode()).hexdigest()
        ledger_cash, realized_pnl, high_water, latest = initial, 0, initial, 0.0
        totals, costs, purchased, receipt_seen, contribution = {}, {}, {}, set(), {}
        writeoff_seen, loss_items = set(), set()
        for event in events:
            item_id, amount, kind = event['item_uuid'], event['amount'], event['kind']
            if type(amount) is not int or not isinstance(event['source'], str) or not event['source'].strip():
                errors.append('INVALID_MONEY_EVENT')
                continue
            when = event['recorded_at']
            if not isinstance(when, (float, int)) or not math.isfinite(when) or when > now or when < 0:
                errors.append('INVALID_EVENT_TIME')
                continue
            latest = max(latest, when)
            if seed and when < seed['recorded_at']:
                errors.append('EVENT_PRECEDES_SEED')
            if kind not in ('purchase', 'receipt', 'refund', 'cost', 'recovery', 'writeoff'):
                errors.append('UNKNOWN_EVENT_KIND')
            if (kind in ('purchase', 'refund', 'cost') and amount > 0) or (kind in ('receipt', 'recovery') and amount < 0):
                errors.append('INVALID_EVENT_SIGN')
            if kind == 'writeoff' and amount != 0:
                errors.append('WRITEOFF_MUST_NOT_DEBIT_CASH')
                continue
            ledger_cash += amount
            totals[item_id] = totals.get(item_id, 0) + amount
            if kind == 'purchase':
                purchased[item_id] = purchased.get(item_id, 0) - amount
            if kind == 'cost':
                costs[item_id] = costs.get(item_id, 0) - amount
            if kind == 'receipt':
                receipt_seen.add(item_id)
            if kind == 'writeoff':
                if item_id in writeoff_seen or item_id in receipt_seen or purchased.get(item_id, 0) <= 0:
                    errors.append('INVALID_WRITEOFF_TRANSITION')
                writeoff_seen.add(item_id)
                loss_items.add(item_id)
            if kind == 'refund' and amount < 0:
                loss_items.add(item_id)
            if kind == 'refund' and item_id not in receipt_seen:
                errors.append('REFUND_WITHOUT_RECEIPT')
            # Open purchase principal is exposure, not an already-realized loss.
            finalized = item_id in receipt_seen or item_id in writeoff_seen
            current = totals[item_id] if finalized else totals[item_id] + purchased.get(item_id, 0)
            realized_pnl += current - contribution.get(item_id, 0)
            contribution[item_id] = current
            high_water = max(high_water, initial + realized_pnl)
        if ledger_cash < 0:
            errors.append('NEGATIVE_LEDGER_CASH')
        if conn.execute('''SELECT 1 FROM trade_money_events e LEFT JOIN flipper_inventory i
                           ON e.item_uuid=i.item_uuid WHERE i.item_uuid IS NULL LIMIT 1''').fetchone():
            errors.append('ORPHAN_MONEY_EVENT')
        evidence_available = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchase_evidence'").fetchone()
        open_risk = supplier_exposure = category_exposure = operating_buffer = 0
        supplier_quarantined = unknown_loss_supplier = False
        for item in items:
            item_id = item['item_uuid']
            try:
                buy = stored_kopecks(item['buy_price'])
            except ValueError:
                errors.append('INVALID_INVENTORY_COST')
                continue
            if item_id not in purchased or purchased[item_id] != buy:
                errors.append('INVENTORY_PURCHASE_NOT_RECONCILED')
            if item.get('status') in ('sold', 'refunded') and item_id not in receipt_seen:
                errors.append('SETTLEMENT_EVENT_MISSING')
            if item.get('status') == 'written_off' and item_id not in writeoff_seen:
                errors.append('WRITEOFF_EVENT_MISSING')
            review = {}
            if evidence_available:
                rows = conn.execute('''SELECT e.review FROM purchase_evidence e
                    JOIN purchase_intents p ON p.intent_id=e.intent_id
                    WHERE p.order_id=? AND p.lot_id=?''', (item.get('order_id'), item['lot_id'])).fetchall()
                if len(rows) == 1:
                    try:
                        review = json.loads(rows[0]['review'])
                        if not isinstance(review, dict):
                            review = {}
                    except (TypeError, ValueError):
                        review = {}
            known_supplier = review.get('supplier_group')
            if item_id in loss_items:
                if isinstance(known_supplier, str) and known_supplier.strip():
                    if not supplier_group or known_supplier.strip().casefold() == supplier_group.strip().casefold():
                        supplier_quarantined = True
                else:
                    unknown_loss_supplier = True
            # Receipt/writeoff realizes original principal exactly once. Neither
            # zero-cash writeoff nor later recovery may subtract it a second time.
            if item_id in receipt_seen or item_id in writeoff_seen:
                continue
            planned = 0
            if 'route_cost' in review:
                try:
                    planned = _rubles(review['route_cost'])
                except ValueError:
                    errors.append('INVALID_RECORDED_ROUTE_COST')
            paid_cost = max(0, costs.get(item_id, 0))
            risk = buy + max(paid_cost, planned)
            operating_buffer += max(0, planned-paid_cost)
            open_risk += risk
            if not supplier_group or not isinstance(known_supplier, str) or not known_supplier.strip() or known_supplier == supplier_group:
                supplier_exposure += risk
            if not category_id or not item.get('category_id') or item['category_id'] == category_id:
                category_exposure += risk
        pending = [dict(row) for row in conn.execute(
            "SELECT * FROM purchase_intents WHERE status IN ('pending','unknown','executed')")]
        pending_debits = 0
        for intent in pending:
            try:
                pending_debits += stored_kopecks(intent['price'])
            except ValueError:
                errors.append('INVALID_PENDING_DEBIT')
        # Unknown checkout outcomes block globally, including legacy claims which
        # have no reliable environment marker; do not guess which wallet paid.
        realized_capital = max(0, initial + realized_pnl)
        reconciled = not errors
        proof_age = max(0.0, now-proof['recorded_at']) if proof else float('inf')
        valid_proof = bool(proof and proof['recorded_at'] <= now and proof['recorded_at'] >= latest
                           and proof_age <= 60 and proof['ledger_fingerprint'] == fingerprint
                           and 0 <= proof['amount'] <= max(0, ledger_cash))
        if dry_run:
            balance_verified, available, purchase_available, age = bool(seed and reconciled), max(0, ledger_cash), True, 0.0
        else:
            balance_verified = bool(seed and reconciled and valid_proof)
            available = min(max(0, ledger_cash), proof['amount']) if balance_verified else 0
            purchase_available = bool(balance_verified and proof['purchasing_available'])
            age = proof_age
        stopped = bool(emergency_stopped) if emergency_stopped is not None else False
        if not stopped:
            try:
                row = conn.execute("SELECT value FROM flipper_settings WHERE key='is_emergency_stopped'").fetchone()
                if row and row[0] is not None:
                    stopped = str(row[0]).strip().lower() in ('1', 'true', 'yes')
            except Exception:
                pass
        if not stopped and hasattr(self, 'is_emergency_stopped'):
            val = getattr(self, 'is_emergency_stopped')
            if callable(val):
                stopped = bool(val())
            elif isinstance(val, bool):
                stopped = val
            elif isinstance(val, str):
                stopped = val.strip().lower() in ('1', 'true', 'yes')

        return {
            'seeded': bool(seed), 'initial_capital': initial, 'ledger_cash': ledger_cash,
            'realized_pnl': realized_pnl, 'realized_capital': realized_capital,
            'capital_high_water': high_water, 'session_loss': max(0, high_water-realized_capital),
            'conservative_equity': max(0, min(ledger_cash, realized_capital)),
            'available_cash': available, 'balance_verified': balance_verified,
            'purchasing_available': purchase_available, 'reconciliation_ok': reconciled,
            'snapshot_age_seconds': age, 'ledger_fingerprint': fingerprint,
            'latest_event_at': latest, 'cash_proof_at': proof['recorded_at'] if proof else None,
            'cash_proof_source': proof['source'] if proof else None,
            'open_risk': open_risk, 'supplier_exposure': supplier_exposure,
            'category_exposure': category_exposure, 'operating_buffer': operating_buffer,
            'pending_purchase_debits': pending_debits, 'unresolved_purchase': bool(pending),
            'supplier_quarantined': supplier_quarantined,
            'unknown_loss_supplier': unknown_loss_supplier,
            'errors': sorted(set(errors)),
            'emergency_stopped': stopped,
        }

    def capital_snapshot(self, dry_run, supplier_group='', category_id='', conn=None, emergency_stopped=None):
        state = self.capital_status(dry_run, supplier_group, category_id, conn=conn, emergency_stopped=emergency_stopped)
        return {
            'available_cash': state['available_cash'],
            'conservative_equity': state['conservative_equity'],
            'session_opening_equity': state['capital_high_water'],
            'session_loss': state['session_loss'],
            'pending_purchase_debits': state['pending_purchase_debits'],
            'refund_liabilities': 0, 'operating_buffer': state['operating_buffer'],
            'supplier_exposure': state['supplier_exposure'],
            'category_exposure': state['category_exposure'],
            'snapshot_age_seconds': state['snapshot_age_seconds'],
            'balance_verified': state['balance_verified'],
            'purchasing_available': state['purchasing_available'],
            'reconciliation_ok': state['reconciliation_ok'],
            'unresolved_purchase': state['unresolved_purchase'],
            'emergency_stopped': state['emergency_stopped'],
            'supplier_quarantined': state['supplier_quarantined'] or state['unknown_loss_supplier'],
        }
