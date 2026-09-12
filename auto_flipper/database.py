"""
auto_flipper/database.py — Dedicated SQLite database manager for Auto-Flipper (auto_flipper.db)
Supports multi-category inventory, buyer order fulfillment, and financial analytics.
"""
import contextlib
import json
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from auto_flipper.categories import CATEGORY_REGISTRY
from auto_flipper.config import (
    ADMIN_IDS,
    ARBITRAGE_AUTO_BUY_DEFAULT,
    ARBITRAGE_DRY_RUN_DEFAULT,
    ARBITRAGE_MAX_BUDGET_DEFAULT,
    ARBITRAGE_MIN_MARGIN_PCT,
    ARBITRAGE_MIN_PROFIT,
    ARBITRAGE_MIN_SELLER_RATING,
    ARBITRAGE_MIN_SELLER_REVIEWS,
    ARBITRAGE_PRICE_FLOOR,
    DB_PATH,
    DEFAULT_PROFIT_GOAL,
    FUNPAY_DIGITAL_FEE_RATE,
)


from auto_flipper.safety_store import SafetyStore


class DatabaseBusyError(sqlite3.OperationalError):
    """A bounded lock wait expired. No business operation is automatically replayed."""


def _is_lock_error(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return any(message in str(error).lower() for message in
               ('database is locked', 'database table is locked', 'database is busy'))


class Database(SafetyStore):
    """SQLite storage with concurrent WAL readers and one serialized writer.

    RLock coordinates threads in this process. SQLite transactions coordinate
    different processes; WAL does not permit simultaneous writers. A busy error
    rolls back this transaction and is surfaced without replaying checkout work.
    """

    def __init__(self, db_path: str = DB_PATH, *, busy_timeout_ms: int = 5000):
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 30000:
            raise ValueError('busy_timeout_ms must be an integer from 1 to 30000')
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._backup_before_safety_migration()
        self._enable_wal()
        self.init_db()

    def _enable_wal(self):
        # journal_mode persists in the database; do not try changing it in every
        # ordinary connection or during a business transaction.
        with self._get_connection() as conn:
            mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if str(mode).lower() != 'wal':
                raise sqlite3.OperationalError('WAL requires a writable local file-backed database')

    def _backup_before_safety_migration(self):
        from pathlib import Path
        path = Path(self.db_path)
        if not path.is_file() or path.stat().st_size == 0:
            return
        backup = path.with_name(path.name + '.pre_safe_v1.bak')
        with self._get_connection() as source:
            if source.execute("SELECT 1 FROM sqlite_master WHERE name='trade_candidates'").fetchone() or backup.exists():
                return
            temporary = backup.with_name(backup.name + '.tmp-' + uuid.uuid4().hex)
            deadline = time.monotonic() + self.busy_timeout_ms / 1000
            def progress(status, remaining, total):
                # sqlite3.backup otherwise retries SQLITE_BUSY indefinitely,
                # independently of the connection's ordinary busy_timeout.
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                    raise DatabaseBusyError('SQLite busy during migration backup; no migration performed')
            try:
                with contextlib.closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination, pages=128, progress=progress, sleep=.05)
                if not backup.exists():
                    temporary.replace(backup)
            finally:
                # An interrupted copy must never look like a completed backup.
                temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        try:
            # Values are bounded integers validated in __init__, never SQL input.
            conn.execute(f'PRAGMA busy_timeout={self.busy_timeout_ms}')
            conn.execute('PRAGMA synchronous=FULL')
            with conn:
                yield conn
        except sqlite3.OperationalError as error:
            # ``with conn`` has already rolled back pending writes. In
            # particular, never retry a transaction which surrounds an external
            # request: the external result may require manual reconciliation.
            if _is_lock_error(error):
                raise DatabaseBusyError(
                    f'SQLite busy: lock wait limited to {self.busy_timeout_ms} ms; '
                    'transaction rolled back; inspect operation status before retrying'
                ) from error
            raise
        finally:
            conn.close()

    def init_db(self):
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Flipper Inventory Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipper_inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_uuid TEXT UNIQUE NOT NULL,
                    lot_id TEXT NOT NULL,
                    order_id TEXT,
                    title TEXT NOT NULL,
                    seller TEXT,
                    buy_price REAL NOT NULL,
                    sell_price REAL NOT NULL,
                    net_profit_expected REAL NOT NULL,
                    net_profit_realized REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    credentials_raw TEXT,
                    credentials_parsed TEXT,
                    resale_lot_id TEXT,
                    buyer_order_id TEXT,
                    buyer_username TEXT,
                    is_dry_run INTEGER DEFAULT 1,
                    category_id TEXT DEFAULT 'chatgpt',
                    node_id INTEGER DEFAULT 1355,
                    item_type TEXT DEFAULT 'account',
                    bought_at TEXT,
                    listed_at TEXT,
                    sold_at TEXT,
                    notes TEXT
                )
            """)
            # Backwards-compatibility schema migration for existing databases
            cursor.execute("PRAGMA table_info(flipper_inventory)")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            if "buyer_order_id" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN buyer_order_id TEXT")
            if "buyer_username" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN buyer_username TEXT")
            if "category_id" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN category_id TEXT DEFAULT 'chatgpt'")
            if "node_id" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN node_id INTEGER DEFAULT 1355")
            if "item_type" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN item_type TEXT DEFAULT 'account'")
            if "delivery_status" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN delivery_status TEXT DEFAULT 'pending'")
            if "settlement_status" not in existing_cols:
                cursor.execute("ALTER TABLE flipper_inventory ADD COLUMN settlement_status TEXT DEFAULT 'pending'")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_status ON flipper_inventory (status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_order ON flipper_inventory (order_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_resale ON flipper_inventory (resale_lot_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_buyer_order ON flipper_inventory (buyer_order_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_category ON flipper_inventory (category_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_flipper_node ON flipper_inventory (node_id)")

            # 2. Key-Value Settings Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipper_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # 3. Purchase Intents Table (Atomic pre-checkout reservation & reconciliation)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS purchase_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT UNIQUE NOT NULL,
                    lot_id TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_message TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intents_lot ON purchase_intents (lot_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_intents_status ON purchase_intents (status)")

            # 4. Flipper Users / Admin Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipper_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    profit_goal REAL DEFAULT 5000.0,
                    is_admin INTEGER DEFAULT 0,
                    notifications_enabled INTEGER DEFAULT 1,
                    created_at TEXT,
                    last_active_at TEXT
                )
            """)

            # 5. Action Logs Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS flipper_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT,
                    details TEXT,
                    timestamp TEXT
                )
            """)

            self.init_safety_schema(conn)

            # Populate initial default settings if not set
            cursor.execute("SELECT key FROM flipper_settings")
            existing_keys = {row["key"] for row in cursor.fetchall()}
            defaults = {
                "dry_run": "1" if ARBITRAGE_DRY_RUN_DEFAULT else "0",
                "auto_buy": "1" if ARBITRAGE_AUTO_BUY_DEFAULT else "0",
                "flipper_mode": "OBSERVE",
                "max_budget": str(ARBITRAGE_MAX_BUDGET_DEFAULT),
                "min_profit": str(ARBITRAGE_MIN_PROFIT),
                "min_margin_pct": str(ARBITRAGE_MIN_MARGIN_PCT),
                "min_seller_rating": str(ARBITRAGE_MIN_SELLER_RATING),
                "min_seller_reviews": str(ARBITRAGE_MIN_SELLER_REVIEWS),
                "price_floor": str(ARBITRAGE_PRICE_FLOOR),
                "is_emergency_stopped": "0",
                "turbo_mode": "0",
                "profit_goal": str(DEFAULT_PROFIT_GOAL),
            }
            # Add default enable flags for all registered categories
            for cat_id, cat in CATEGORY_REGISTRY.items():
                defaults[f"category_enabled_{cat_id}"] = "1" if cat.enabled_default else "0"

            for k, v in defaults.items():
                if k not in existing_keys:
                    cursor.execute("INSERT INTO flipper_settings (key, value) VALUES (?, ?)", (k, v))

            conn.commit()

    # ─────────────────────────────────────────────────────────────
    # User Management
    # ─────────────────────────────────────────────────────────────

    def get_or_create_user(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> Dict[str, Any]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            now_str = datetime.now().isoformat()

            if row:
                cursor.execute("""
                    UPDATE flipper_users
                    SET username = ?, first_name = ?, last_active_at = ?
                    WHERE user_id = ?
                """, (username or row["username"], first_name or row["first_name"], now_str, user_id))
                conn.commit()
                cursor.execute("SELECT * FROM flipper_users WHERE user_id = ?", (user_id,))
                return dict(cursor.fetchone())
            else:
                cursor.execute("SELECT COUNT(*) as cnt FROM flipper_users")
                cnt_row = cursor.fetchone()
                is_first = (cnt_row["cnt"] == 0) if cnt_row else True
                is_adm = 1 if user_id in ADMIN_IDS and user_id > 0 else 0
                cursor.execute("""
                    INSERT INTO flipper_users (user_id, username, first_name, profit_goal, is_admin, notifications_enabled, created_at, last_active_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """, (user_id, username, first_name, DEFAULT_PROFIT_GOAL, is_adm, now_str, now_str))
                conn.commit()
                cursor.execute("SELECT * FROM flipper_users WHERE user_id = ?", (user_id,))
                return dict(cursor.fetchone())

    def get_all_active_users(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_users WHERE notifications_enabled = 1")
            return [dict(r) for r in cursor.fetchall()]

    # ─────────────────────────────────────────────────────────────
    # Inventory CRUD
    # ─────────────────────────────────────────────────────────────

    def add_inventory(
        self,
        lot_id: str,
        title: str,
        buy_price: float,
        sell_price: float,
        net_profit_expected: float,
        seller: str = "",
        order_id: Optional[str] = None,
        status: str = "bought",
        category_id: str = "chatgpt",
        node_id: int = 1355,
        item_type: str = "account",
        buyer_order_id: Optional[str] = None,
        buyer_username: Optional[str] = None,
        is_dry_run: bool = True,
        notes: str = "",
    ) -> str:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            item_uuid = f"flip_{uuid.uuid4().hex[:12]}"
            now_str = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO flipper_inventory (
                    item_uuid, lot_id, order_id, title, seller,
                    buy_price, sell_price, net_profit_expected, net_profit_realized,
                    status, category_id, node_id, item_type, buyer_order_id, buyer_username,
                    is_dry_run, bought_at, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item_uuid, lot_id, order_id, title, seller,
                buy_price, sell_price, net_profit_expected,
                status, category_id, node_id, item_type, buyer_order_id, buyer_username,
                1 if is_dry_run else 0, now_str, notes
            ))
            conn.commit()
            return item_uuid

    def update_inventory(self, item_uuid: str, **kwargs) -> bool:
        allowed = {
            "order_id", "status", "credentials_raw", "credentials_parsed",
            "resale_lot_id", "buyer_order_id", "buyer_username",
            "net_profit_realized", "sell_price", "buy_price",
            "category_id", "node_id", "item_type",
            "delivery_status", "settlement_status",
            "listed_at", "sold_at", "notes", "is_dry_run"
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            clause = ", ".join([f"{k} = ?" for k in updates.keys()])
            values = list(updates.values()) + [item_uuid]
            cursor.execute(f"UPDATE flipper_inventory SET {clause} WHERE item_uuid = ?", values)
            conn.commit()
            return cursor.rowcount > 0

    def get_inventory_item(self, item_uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_inventory WHERE item_uuid = ?", (item_uuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_inventory_by_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Finds inventory item by PURCHASE order_id (from when bot bought the lot)."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_inventory WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_inventory_by_buyer_order(self, buyer_order_id: str) -> Optional[Dict[str, Any]]:
        """
        Finds inventory item fulfilled for a given BUYER order_id.
        CRITICAL: Prevents duplicate delivery to the same buyer order.
        """
        if not buyer_order_id:
            return None
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM flipper_inventory WHERE buyer_order_id = ? ORDER BY id DESC LIMIT 1",
                (buyer_order_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_inventory_for_order_fulfillment(
        self,
        category_id: Optional[str] = None,
        node_id: Optional[int] = None,
        resale_lot_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Selects an available inventory item to fulfill an incoming buyer order.
        1. If resale_lot_id is provided, strictly matches by exact offer ID first (exact link).
           If not found, returns None to avoid delivering an unrelated item.
        2. Prioritizes items in status='listed'.
        3. Falls back to items in status='ready_for_sale'.
        Matches by category_id and/or node_id if specified.
        """
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if resale_lot_id:
                for status in ("listed", "ready_for_sale"):
                    cursor.execute(
                        "SELECT * FROM flipper_inventory WHERE status = ? AND resale_lot_id = ? ORDER BY id ASC LIMIT 1",
                        (status, resale_lot_id)
                    )
                    row = cursor.fetchone()
                    if row:
                        return dict(row)
                return None

            for status in ("listed", "ready_for_sale"):
                conditions = ["status = ?"]
                params: List[Any] = [status]
                if category_id:
                    conditions.append("category_id = ?")
                    params.append(category_id)
                if node_id:
                    conditions.append("node_id = ?")
                    params.append(node_id)

                where_clause = " AND ".join(conditions)
                query = f"SELECT * FROM flipper_inventory WHERE {where_clause} ORDER BY id ASC LIMIT 1"
                cursor.execute(query, params)
                row = cursor.fetchone()
                if row:
                    return dict(row)

            # If no category or node filter was provided, take the earliest listed/ready item
            if not category_id and not node_id and not resale_lot_id:
                for status in ("listed", "ready_for_sale"):
                    cursor.execute("SELECT * FROM flipper_inventory WHERE status = ? ORDER BY id ASC LIMIT 1", (status,))
                    row = cursor.fetchone()
                    if row:
                        return dict(row)

            return None

    def reserve_inventory_for_fulfillment(
        self,
        category_id: Optional[str] = None,
        node_id: Optional[int] = None,
        resale_lot_id: Optional[str] = None,
        buyer_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically selects and locks an available inventory item to prevent double delivery.
        Sets status='reserved' and delivery_status='in_progress'.
        """
        if not resale_lot_id or not buyer_order_id:
            return None
        with self._lock, self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM flipper_inventory WHERE buyer_order_id=?", (buyer_order_id,)).fetchone():
                return None
            rows = conn.execute("""SELECT * FROM flipper_inventory
                WHERE status IN ('listed','ready_for_sale') AND resale_lot_id=?
                AND buyer_order_id IS NULL AND is_dry_run=0
                AND item_type NOT IN ('trade_item','permanent_key')
                AND EXISTS(SELECT 1 FROM inventory_checks c WHERE c.item_uuid=flipper_inventory.item_uuid)""", (resale_lot_id,)).fetchall()
            if len(rows) != 1:
                return None
            item = dict(rows[0])
            if category_id and item['category_id'] != category_id:
                return None
            if node_id and item['node_id'] != node_id:
                return None
            cursor = conn.execute("""UPDATE flipper_inventory
                SET status='reserved', buyer_order_id=?, delivery_status='in_progress'
                WHERE item_uuid=? AND status IN ('listed','ready_for_sale') AND buyer_order_id IS NULL""",
                (buyer_order_id,item['item_uuid']))
            if cursor.rowcount != 1:
                return None
            item.update(status='reserved',buyer_order_id=buyer_order_id,delivery_status='in_progress')
            return item

    # ─────────────────────────────────────────────────────────────
    # Purchase Intents & Pre-Checkout Atomic State
    # ─────────────────────────────────────────────────────────────

    def create_purchase_intent(
        self,
        lot_id: str,
        category_id: str,
        price: float,
    ) -> str:
        intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        now_str = datetime.now().isoformat()
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO purchase_intents (intent_id, lot_id, category_id, price, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """, (intent_id, lot_id, category_id, price, now_str, now_str))
            conn.commit()
        return intent_id

    def update_purchase_intent(
        self,
        intent_id: str,
        status: str,
        order_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        now_str = datetime.now().isoformat()
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE purchase_intents
                SET status = ?, order_id = COALESCE(?, order_id), error_message = ?, updated_at = ?
                WHERE intent_id = ?
            """, (status, order_id, error_message, now_str, intent_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_purchase_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM purchase_intents WHERE intent_id = ?", (intent_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_unresolved_purchase_intents(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM purchase_intents WHERE status IN ('pending', 'unknown', 'executed') ORDER BY id DESC")
            return [dict(r) for r in cursor.fetchall()]

    def is_user_admin(self, user_id: int) -> bool:
        # Legacy first-user grants are not sufficient authorization.
        return type(user_id) is int and user_id > 0 and user_id in ADMIN_IDS

    def set_user_admin(self, user_id: int, is_admin: bool = True):
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE flipper_users SET is_admin = ? WHERE user_id = ?", (1 if is_admin else 0, user_id))
            conn.commit()

    def get_flipper_mode(self) -> Optional[str]:
        return self.get_setting("flipper_mode")

    def set_flipper_mode(self, mode: str):
        self.set_setting("flipper_mode", mode.upper())

    def get_inventory_by_resale_lot(self, resale_lot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_inventory WHERE resale_lot_id = ? ORDER BY id DESC LIMIT 1", (resale_lot_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_inventory_by_lot(self, lot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM flipper_inventory WHERE lot_id = ? ORDER BY id DESC LIMIT 1", (lot_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_inventory_list(
        self,
        status: Optional[str] = None,
        category_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            clauses = []
            params: List[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if category_id:
                clauses.append("category_id = ?")
                params.append(category_id)

            where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            query = f"SELECT * FROM flipper_inventory {where_str} ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_inventory_counts(self) -> Dict[str, int]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) as cnt FROM flipper_inventory GROUP BY status")
            counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}
            return {
                "bought": counts.get("bought", 0),
                "ready_for_sale": counts.get("ready_for_sale", 0),
                "listed": counts.get("listed", 0),
                "emergency_paused": counts.get("emergency_paused", 0),
                "sold": counts.get("sold", 0),
                "total": sum(counts.values()),
            }

    def deactivate_all_active_listings(self) -> int:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE flipper_inventory
                SET status = 'emergency_paused'
                WHERE status = 'listed'
            """)
            conn.commit()
            return cursor.rowcount

    def reactivate_emergency_listings(self) -> int:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE flipper_inventory
                SET status = 'listed'
                WHERE status = 'emergency_paused'
            """)
            conn.commit()
            return cursor.rowcount

    # ─────────────────────────────────────────────────────────────
    # Category Toggles & Configuration
    # ─────────────────────────────────────────────────────────────

    def is_category_enabled(self, category_id: str) -> bool:
        from auto_flipper.safety import EXCLUDED_CATEGORIES
        if category_id in EXCLUDED_CATEGORIES:
            return False
        default_enabled = "1" if CATEGORY_REGISTRY.get(category_id) and CATEGORY_REGISTRY[category_id].enabled_default else "0"
        val = self.get_setting(f"category_enabled_{category_id}", default_enabled)
        return val in ("1", "true", "True")

    def set_category_enabled(self, category_id: str, enabled: bool):
        self.set_setting(f"category_enabled_{category_id}", "1" if enabled else "0")

    def toggle_category_enabled(self, category_id: str) -> bool:
        new_state = not self.is_category_enabled(category_id)
        self.set_category_enabled(category_id, new_state)
        return new_state

    def get_enabled_categories(self) -> List[str]:
        return [cat_id for cat_id in CATEGORY_REGISTRY.keys() if self.is_category_enabled(cat_id)]

    # ─────────────────────────────────────────────────────────────
    # P&L and Financial Analytics (Multi-Category Aware)
    # ─────────────────────────────────────────────────────────────

    def get_pnl_stats(self, category_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            if category_id:
                cursor.execute("SELECT * FROM flipper_inventory WHERE category_id = ?", (category_id,))
                all_rows = [dict(r) for r in cursor.fetchall()]
                cursor.execute("SELECT * FROM flipper_inventory")
                full_rows = [dict(r) for r in cursor.fetchall()]
            else:
                cursor.execute("SELECT * FROM flipper_inventory")
                all_rows = [dict(r) for r in cursor.fetchall()]
                full_rows = all_rows

        all_rows = [r for r in all_rows if not r['is_dry_run']]
        full_rows = [r for r in full_rows if not r['is_dry_run']]
        with self._get_connection() as conn:
            receipts = {r['item_uuid']: r['amount']/100 for r in conn.execute(
                "SELECT item_uuid,SUM(amount) amount FROM trade_money_events WHERE kind IN ('receipt','refund','writeoff','recovery') GROUP BY item_uuid")}
            costs = {r['item_uuid']: r['amount']/100 for r in conn.execute("SELECT item_uuid,SUM(amount) amount FROM trade_money_events WHERE kind='cost' GROUP BY item_uuid")}
        closed_rows = [r for r in all_rows if r['status'] in ('sold','refunded','written_off') and r['item_uuid'] in receipts]
        sold_rows = [r for r in closed_rows if r['status'] != 'written_off']
        listed_rows = [r for r in all_rows if r['status'] == 'listed']
        holding_rows = [r for r in all_rows if r['status'] in ('bought','awaiting_intake','ready_for_sale','reserved','delivered','emergency_paused')]
        total_sold = len(sold_rows)
        total_spent_sold = sum(r['buy_price'] for r in closed_rows)
        total_revenue_gross = sum(receipts[r['item_uuid']] for r in closed_rows)  # Compatibility key: net receipts.
        total_fees = 0.0  # Already included in verified net receipts; never subtract a guessed rate.
        total_net_profit = round(sum(receipts[r['item_uuid']]+costs.get(r['item_uuid'],0)-r['buy_price'] for r in closed_rows),2)
        closed_ids = {r['item_uuid'] for r in closed_rows}
        # An actual operating expense is a loss immediately, even before sale.
        total_net_profit = round(total_net_profit + sum(costs.get(r['item_uuid'],0)
            for r in all_rows if r['item_uuid'] not in closed_ids), 2)

        roi_pct = round((total_net_profit / total_spent_sold) * 100.0, 2) if total_spent_sold > 0 else 0.0

        active_listed_count = len(listed_rows)
        active_listed_value = sum(r["sell_price"] for r in listed_rows)

        holding_count = len(holding_rows)
        holding_cost = sum(r["buy_price"] for r in holding_rows)

        turnover_times = []
        for r in sold_rows:
            if r.get("bought_at") and r.get("sold_at"):
                try:
                    t_b = datetime.fromisoformat(r["bought_at"])
                    t_s = datetime.fromisoformat(r["sold_at"])
                    delta_sec = (t_s - t_b).total_seconds()
                    if delta_sec >= 0:
                        turnover_times.append(delta_sec)
                except Exception:
                    pass

        avg_turnover_seconds = statistics.mean(turnover_times) if turnover_times else 0.0

        # Multi-Category P&L Breakdown
        by_category: Dict[str, Dict[str, Any]] = {}
        for cat_key in CATEGORY_REGISTRY.keys():
            by_category[cat_key] = {
                "name": CATEGORY_REGISTRY[cat_key].name,
                "fee_rate_pct": round(CATEGORY_REGISTRY[cat_key].fee_rate * 100.0, 1),
                "total_sold": 0,
                "total_revenue_gross": 0.0,
                "total_spent_on_sold": 0.0,
                "total_net_profit": 0.0,
                "roi_pct": 0.0,
                "active_listed": 0,
                "holding_count": 0,
            }

        for r in full_rows:
            c_id = r.get("category_id") or "chatgpt"
            if c_id not in by_category:
                by_category[c_id] = {
                    "name": c_id,
                    "total_sold": 0,
                    "total_revenue_gross": 0.0,
                    "total_spent_on_sold": 0.0,
                    "total_net_profit": 0.0,
                    "roi_pct": 0.0,
                    "active_listed": 0,
                    "holding_count": 0,
                }
            cat_stat = by_category[c_id]
            st = r.get("status")
            if st not in ('sold','refunded','written_off') or r['item_uuid'] not in receipts:
                cat_stat['total_net_profit'] += costs.get(r['item_uuid'],0)
            if st in ("sold", "refunded", "written_off") and r["item_uuid"] in receipts:
                cat_stat["total_sold"] += int(st != "written_off")
                cat_stat["total_revenue_gross"] += receipts[r["item_uuid"]]
                cat_stat["total_spent_on_sold"] += r["buy_price"]
                cat_stat["total_net_profit"] += receipts[r["item_uuid"]]+costs.get(r["item_uuid"],0)-r["buy_price"]
            elif st == "listed":
                cat_stat["active_listed"] += 1
            elif st in ("bought", "awaiting_intake", "ready_for_sale", "reserved", "delivered", "emergency_paused"):
                cat_stat["holding_count"] += 1

        for c_id, c_stat in by_category.items():
            c_stat["total_revenue_gross"] = round(c_stat["total_revenue_gross"], 2)
            c_stat["total_spent_on_sold"] = round(c_stat["total_spent_on_sold"], 2)
            c_stat["total_net_profit"] = round(c_stat["total_net_profit"], 2)
            c_stat["roi_pct"] = round((c_stat["total_net_profit"] / c_stat["total_spent_on_sold"]) * 100.0, 2) if c_stat["total_spent_on_sold"] > 0 else 0.0

        return {
            "total_items": len(all_rows),
            "total_sold": total_sold,
            "total_spent_on_sold": round(total_spent_sold, 2),
            "total_revenue_gross": round(total_revenue_gross, 2),
            "total_fees": total_fees,
            "total_net_profit": total_net_profit,
            "roi_pct": roi_pct,
            "active_listed_count": active_listed_count,
            "active_listed_value": round(active_listed_value, 2),
            "holding_count": holding_count,
            "holding_cost": round(holding_cost, 2),
            "avg_turnover_seconds": round(avg_turnover_seconds, 1),
            "by_category": by_category,
        }

    # ─────────────────────────────────────────────────────────────
    # Profit Goal Management
    # ─────────────────────────────────────────────────────────────

    # Benchmark realistic net profit fallback: buy 300 ₽, sell 590 ₽, 12% fee -> 219.20 ₽
    BENCHMARK_NET_PROFIT = 219.20

    def get_profit_goal(self, user_id: Optional[int] = None) -> float:
        if user_id:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT profit_goal FROM flipper_users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if row and row["profit_goal"]:
                    return float(row["profit_goal"])

        setting_val = self.get_setting("profit_goal")
        return float(setting_val) if setting_val else DEFAULT_PROFIT_GOAL

    def set_profit_goal(self, goal: float, user_id: Optional[int] = None):
        clean_goal = max(100.0, float(goal))
        self.set_setting("profit_goal", str(clean_goal))
        if user_id:
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE flipper_users SET profit_goal = ? WHERE user_id = ?", (clean_goal, user_id))
                conn.commit()
            self.set_setting(f"milestone_last_notified_{user_id}", "0")
        self.set_setting("milestone_last_notified_global", "0")

    def get_goal_progress(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        goal = self.get_profit_goal(user_id)
        pnl = self.get_pnl_stats()
        realized = pnl["total_net_profit"]
        percent = round((realized / goal) * 100.0, 1) if goal > 0 else 0.0
        remaining = max(0.0, round(goal - realized, 2))

        count = pnl['total_sold']
        avg_per_flip = round(realized/count,2) if count else 0.0
        has_dynamic_avg = count > 0
        import math
        flips_needed = (max(0,math.ceil(remaining/avg_per_flip)) if avg_per_flip > 0 else None)
        filled = min(10,max(0,int(percent//10)))
        bar = '█'*filled+'░'*(10-filled)
        # Sale duration is not the time until funds can be spent again.
        eta_seconds = eta_hours = eta_days = None
        eta_text = 'Цель достигнута' if remaining == 0 else 'Недостаточно данных о доступности денег'

        milestones = {
            25: percent >= 25.0,
            50: percent >= 50.0,
            75: percent >= 75.0,
            100: percent >= 100.0,
        }

        return {
            "goal": goal,
            "realized": realized,
            "remaining": remaining,
            "percent": percent,
            "bar": bar,
            "avg_per_flip": avg_per_flip,
            "has_dynamic_avg": has_dynamic_avg,
            "flips_needed": flips_needed,
            "eta_seconds": eta_seconds,
            "eta_hours": eta_hours,
            "eta_days": eta_days,
            "eta_text": eta_text,
            "milestones": milestones,
        }

    def check_and_update_milestones(self, user_id: Optional[int] = None) -> Optional[int]:
        """
        Checks if profit progress has crossed 25%, 50%, 75%, or 100% milestone thresholds.
        Returns the milestone threshold (25, 50, 75, or 100) if newly crossed, else None.
        """
        progress = self.get_goal_progress(user_id)
        current_percent = progress["percent"]
        key = f"milestone_last_notified_{user_id}" if user_id else "milestone_last_notified_global"
        last_val = int(self.get_setting(key, "0") or "0")

        thresholds = [25, 50, 75, 100]
        reached_threshold = 0
        for t in thresholds:
            if current_percent >= t and t > last_val:
                reached_threshold = t

        if reached_threshold > 0:
            self.set_setting(key, str(reached_threshold))
            return reached_threshold

        return None

    # ─────────────────────────────────────────────────────────────
    # Settings Key-Value Store
    # ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM flipper_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: Any):
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO flipper_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, str(value)))
            conn.commit()

    def get_all_settings(self) -> Dict[str, str]:
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM flipper_settings")
            return {row["key"]: row["value"] for row in cursor.fetchall()}

    def log_action(self, action: str, details: str):
        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO flipper_logs (action, details, timestamp) VALUES (?, ?, ?)",
                (action, details, datetime.now().isoformat())
            )
            conn.commit()


# Global database instance
db = Database()
