"""
auto_flipper/market_store.py — Lightweight, non-bloating Market History Store for tracked benchmark SKUs.

Rules:
1. Does NOT record every 6-second poll to disk.
2. Maintains individual lot state in `market_history_lots`.
3. Discrete events (NEW, PRICE_CHANGE, STOCK_CHANGE, DISAPPEARED, REAPPEARED) recorded only on changes.
4. Periodic aggregate samples written approximately once every 3–5 minutes in `market_sku_samples`.
5. Strict terminology: disappearance, reappearance, turnover_proxy. Never 'sold' or 'confirmed_sale'.
6. Read-only observation. No live purchasing or checkout logic here.
"""
import json
import logging
import statistics
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from auto_flipper.discovery import extract_discovery_sku, is_discovery_sku
from auto_flipper.sku_matcher import (
    BENCHMARK_SKUS,
    SKU_UNKNOWN,
    match_sku,
)

logger = logging.getLogger("MarketStore")


def compute_quantile(sorted_data: List[float], q: float) -> float:
    """Computes empirical quantile with linear interpolation on sorted non-empty data."""
    if not sorted_data:
        return 0.0
    data = sorted(sorted_data)
    if len(data) == 1:
        return float(data[0])
    n = len(data)
    idx = q * (n - 1)
    low = int(idx)
    high = min(low + 1, n - 1)
    weight = idx - low
    val = data[low] * (1.0 - weight) + data[high] * weight
    return round(float(val), 2)


def compute_mad(data: List[float]) -> float:
    """Computes Median Absolute Deviation (MAD) for robust dispersion measurement."""
    if not data:
        return 0.0
    med = statistics.median(data)
    deviations = [abs(x - med) for x in data]
    return round(float(statistics.median(deviations)), 2)


class MarketHistoryStore:
    """SQLite store mixin for market history and liquidity metrics."""

    def init_market_history_schema(self, conn):
        """Initializes tables and indexes for market history tracking."""
        statements = (
            # 1. State of tracked listings
            """CREATE TABLE IF NOT EXISTS market_history_lots (
                lot_id TEXT PRIMARY KEY,
                canonical_sku TEXT NOT NULL,
                title TEXT NOT NULL,
                seller TEXT NOT NULL,
                seller_rating REAL DEFAULT 0.0,
                seller_reviews INTEGER DEFAULT 0,
                price REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                stock INTEGER DEFAULT 1,
                url TEXT NOT NULL,
                node_id INTEGER DEFAULT 1808,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                observed_at REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                disappeared_at REAL,
                reappearance_count INTEGER NOT NULL DEFAULT 0,
                price_changes INTEGER NOT NULL DEFAULT 0,
                last_price REAL
            )""",
            # 2. Discrete state transition events
            """CREATE TABLE IF NOT EXISTS market_lot_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id TEXT NOT NULL,
                canonical_sku TEXT NOT NULL,
                event_type TEXT NOT NULL,
                old_price REAL,
                new_price REAL,
                old_stock INTEGER,
                new_stock INTEGER,
                timestamp REAL NOT NULL
            )""",
            # 3. Periodic aggregated market samples (every ~3-5 min)
            """CREATE TABLE IF NOT EXISTS market_sku_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_sku TEXT NOT NULL,
                sampled_at REAL NOT NULL,
                active_listings INTEGER NOT NULL,
                unique_sellers INTEGER NOT NULL,
                total_stock INTEGER NOT NULL,
                min_price REAL NOT NULL,
                p10_price REAL NOT NULL,
                p25_price REAL NOT NULL,
                p50_price REAL NOT NULL,
                p90_price REAL NOT NULL,
                max_price REAL NOT NULL,
                price_dispersion REAL NOT NULL,
                mad_price REAL NOT NULL,
                metrics_json TEXT NOT NULL
            )""",
            # Indexes
            "CREATE INDEX IF NOT EXISTS idx_mhl_sku_active ON market_history_lots (canonical_sku, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_mhl_first_seen ON market_history_lots (first_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_mhl_last_seen ON market_history_lots (last_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_mhe_sku_time ON market_lot_events (canonical_sku, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_mss_sku_time ON market_sku_samples (canonical_sku, sampled_at)",
            # Account observation extends this same history store. Composite keys
            # make disappearance semantics strictly market-local.
            """CREATE TABLE IF NOT EXISTS account_market_lots (
                market_id TEXT NOT NULL,
                lot_id TEXT NOT NULL,
                node_id INTEGER NOT NULL,
                seller TEXT NOT NULL,
                seller_rating REAL DEFAULT 0.0,
                seller_reviews INTEGER DEFAULT 0,
                price REAL,
                currency TEXT NOT NULL DEFAULT 'UNKNOWN',
                stock INTEGER DEFAULT 1,
                title TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                cohort_id TEXT NOT NULL,
                cohort_confidence REAL NOT NULL DEFAULT 0.0,
                classified INTEGER NOT NULL DEFAULT 0,
                features_json TEXT NOT NULL,
                risk_flags_json TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'ACCOUNT_OBSERVATION',
                purchase_eligible INTEGER NOT NULL DEFAULT 0 CHECK (purchase_eligible = 0),
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                observed_at REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                disappeared_at REAL,
                reappearance_count INTEGER NOT NULL DEFAULT 0,
                price_change_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (market_id, lot_id)
            )""",
            """CREATE TABLE IF NOT EXISTS account_market_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                lot_id TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                old_price REAL,
                new_price REAL,
                old_stock INTEGER,
                new_stock INTEGER,
                timestamp REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS account_market_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                market_id TEXT NOT NULL,
                active_lots INTEGER NOT NULL,
                independent_sellers INTEGER NOT NULL,
                cheap_lot_count INTEGER NOT NULL,
                cheap_independent_sellers INTEGER NOT NULL,
                classified_lots INTEGER NOT NULL,
                metrics_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS account_cohort_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                market_id TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                active_lots INTEGER NOT NULL,
                independent_sellers INTEGER NOT NULL,
                p10 REAL, p25 REAL, p50 REAL, p75 REAL, p90 REAL,
                min_price REAL, max_price REAL, mad REAL, dispersion REAL,
                cheap_lot_count INTEGER NOT NULL,
                new_count INTEGER NOT NULL,
                disappearance_count INTEGER NOT NULL,
                reappearance_count INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS account_market_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                node_id INTEGER NOT NULL,
                started_at REAL NOT NULL,
                advertised_market_count INTEGER,
                parsed_count INTEGER NOT NULL,
                coverage_ratio REAL,
                fetch_success INTEGER NOT NULL,
                parse_success INTEGER NOT NULL,
                snapshot_quality TEXT NOT NULL,
                scan_duration REAL NOT NULL,
                http_status INTEGER,
                failure_reason TEXT NOT NULL DEFAULT ''
            )""",
            "CREATE INDEX IF NOT EXISTS idx_aml_market_active ON account_market_lots (market_id, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_ame_market_time ON account_market_events (market_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_ams_market_time ON account_market_samples (market_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_acs_cohort_time ON account_cohort_samples (market_id, cohort_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_am_scan_market_time ON account_market_scans (market_id, started_at)",
        )
        for sql in statements:
            conn.execute(sql)

        # Migration check for observed_at if table was created in an older run
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(market_history_lots)").fetchall()]
            if "observed_at" not in cols:
                conn.execute("ALTER TABLE market_history_lots ADD COLUMN observed_at REAL DEFAULT 0.0")
        except Exception:
            pass

    def record_market_observation(
        self,
        observed_lots: List[Dict[str, Any]],
        now: Optional[float] = None,
        sample_interval_seconds: float = 180.0,
        force_sample: bool = False,
        scanned_nodes: Optional[Set[int]] = None,
    ) -> Dict[str, Any]:
        """
        Records a single market observation scan.
        Matches lots to canonical benchmark SKUs; unverified lots ('UNKNOWN') are excluded.
        Emits events (NEW, PRICE_CHANGE, STOCK_CHANGE, DISAPPEARED, REAPPEARED).
        Writes periodic aggregate samples to `market_sku_samples` at the configured interval.
        """
        ts = time.time() if now is None else float(now)

        # 1. Filter and classify lots by SKU (exact benchmarks and autonomous discovery)
        sku_lots: Dict[str, List[Dict[str, Any]]] = {}
        for lot in observed_lots:
            node_id = int(lot.get("node_id", 1808))
            title = lot.get("title", "")
            sku = match_sku(node_id, title)
            if sku == SKU_UNKNOWN:
                disc_sku = extract_discovery_sku(node_id, title)
                if disc_sku:
                    sku = disc_sku
                else:
                    continue
            sku_lots.setdefault(sku, []).append(lot)

        # Determine which node_ids were actually scanned in this observation
        if scanned_nodes is not None:
            active_scanned_nodes = set(scanned_nodes)
        elif observed_lots:
            active_scanned_nodes = {int(l.get("node_id", 1808)) for l in observed_lots}
        else:
            # Fallback for empty scans without explicit nodes (e.g. test harness simulating empty market)
            active_scanned_nodes = {1808}

        from auto_flipper.sku_matcher import SKU_NODE_MAP

        summary = {
            "timestamp": ts,
            "new_count": 0,
            "disappeared_count": 0,
            "reappeared_count": 0,
            "price_changed_count": 0,
            "stock_changed_count": 0,
            "samples_created": 0,
        }

        # Process each observed SKU and evaluate active SKUs whose node was part of this scan
        candidate_skus = set(sku_lots.keys())
        for b_sku in BENCHMARK_SKUS:
            if SKU_NODE_MAP.get(b_sku, 1808) in active_scanned_nodes:
                candidate_skus.add(b_sku)

        with self._lock, self._get_connection() as conn:
            if active_scanned_nodes:
                placeholders = ",".join("?" for _ in active_scanned_nodes)
                prev_active = conn.execute(
                    f"SELECT DISTINCT canonical_sku FROM market_history_lots WHERE is_active = 1 AND node_id IN ({placeholders})",
                    list(active_scanned_nodes),
                ).fetchall()
                for r in prev_active:
                    candidate_skus.add(r[0])
            for sku in candidate_skus:
                current_lots = sku_lots.get(sku, [])
                current_map = {l["lot_id"]: l for l in current_lots if "lot_id" in l}

                # Query currently active lots in DB for this SKU
                prev_rows = conn.execute(
                    "SELECT * FROM market_history_lots WHERE canonical_sku = ? AND is_active = 1",
                    (sku,),
                ).fetchall()
                prev_map = {row["lot_id"]: dict(row) for row in prev_rows}

                # Disappeared lots: were active, now absent from current scan
                disappeared_ids = set(prev_map.keys()) - set(current_map.keys())
                for d_id in disappeared_ids:
                    p = prev_map[d_id]
                    conn.execute(
                        """UPDATE market_history_lots
                           SET is_active = 0, disappeared_at = ?
                           WHERE lot_id = ?""",
                        (ts, d_id),
                    )
                    conn.execute(
                        """INSERT INTO market_lot_events
                           (lot_id, canonical_sku, event_type, old_price, new_price, old_stock, new_stock, timestamp)
                           VALUES (?, ?, 'DISAPPEARED', ?, NULL, ?, NULL, ?)""",
                        (d_id, sku, p["price"], p["stock"], ts),
                    )
                    summary["disappeared_count"] += 1

                # Active, new, or reappeared lots
                for lot_id, lot in current_map.items():
                    price = float(lot.get("price", 0.0))
                    stock = int(lot.get("stock", 1))
                    title = str(lot.get("title", ""))
                    seller = str(lot.get("seller", "Unknown"))
                    seller_rating = float(lot.get("seller_rating", 0.0))
                    seller_reviews = int(lot.get("seller_reviews", 0))
                    url = str(lot.get("url", ""))
                    node_id = int(lot.get("node_id", 1808))
                    currency = str(lot.get("currency", "RUB"))

                    existing_row = conn.execute(
                        "SELECT * FROM market_history_lots WHERE lot_id = ?",
                        (lot_id,),
                    ).fetchone()

                    if existing_row is None:
                        # NEW lot
                        conn.execute(
                            """INSERT INTO market_history_lots
                               (lot_id, canonical_sku, title, seller, seller_rating, seller_reviews,
                                price, currency, stock, url, node_id, first_seen_at, last_seen_at,
                                observed_at, is_active, disappeared_at, reappearance_count,
                                price_changes, last_price)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0, 0, ?)""",
                            (
                                lot_id,
                                sku,
                                title,
                                seller,
                                seller_rating,
                                seller_reviews,
                                price,
                                currency,
                                stock,
                                url,
                                node_id,
                                ts,
                                ts,
                                ts,
                                price,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO market_lot_events
                               (lot_id, canonical_sku, event_type, old_price, new_price, old_stock, new_stock, timestamp)
                               VALUES (?, ?, 'NEW', NULL, ?, NULL, ?, ?)""",
                            (lot_id, sku, price, stock, ts),
                        )
                        summary["new_count"] += 1
                    else:
                        existing = dict(existing_row)
                        reappeared = existing["is_active"] == 0
                        reappearance_count = existing["reappearance_count"] + (1 if reappeared else 0)
                        if reappeared:
                            conn.execute(
                                """INSERT INTO market_lot_events
                                   (lot_id, canonical_sku, event_type, old_price, new_price, old_stock, new_stock, timestamp)
                                   VALUES (?, ?, 'REAPPEARED', ?, ?, ?, ?, ?)""",
                                (lot_id, sku, existing["price"], price, existing["stock"], stock, ts),
                            )
                            summary["reappeared_count"] += 1

                        price_changed = abs(existing["price"] - price) > 0.001
                        price_changes = existing["price_changes"] + (1 if price_changed else 0)
                        last_price = existing["price"] if price_changed else existing.get("last_price", price)
                        if price_changed:
                            conn.execute(
                                """INSERT INTO market_lot_events
                                   (lot_id, canonical_sku, event_type, old_price, new_price, old_stock, new_stock, timestamp)
                                   VALUES (?, ?, 'PRICE_CHANGE', ?, ?, ?, ?, ?)""",
                                (lot_id, sku, existing["price"], price, existing["stock"], stock, ts),
                            )
                            summary["price_changed_count"] += 1

                        stock_changed = existing["stock"] != stock
                        if stock_changed and not reappeared:
                            conn.execute(
                                """INSERT INTO market_lot_events
                                   (lot_id, canonical_sku, event_type, old_price, new_price, old_stock, new_stock, timestamp)
                                   VALUES (?, ?, 'STOCK_CHANGE', ?, ?, ?, ?, ?)""",
                                (lot_id, sku, existing["price"], price, existing["stock"], stock, ts),
                            )
                            summary["stock_changed_count"] += 1

                        conn.execute(
                            """UPDATE market_history_lots
                               SET last_seen_at = ?,
                                   observed_at = ?,
                                   is_active = 1,
                                   disappeared_at = NULL,
                                   price = ?,
                                   stock = ?,
                                   title = ?,
                                   seller = ?,
                                   seller_rating = ?,
                                   seller_reviews = ?,
                                   reappearance_count = ?,
                                   price_changes = ?,
                                   last_price = ?
                               WHERE lot_id = ?""",
                            (
                                ts,
                                ts,
                                price,
                                stock,
                                title,
                                seller,
                                seller_rating,
                                seller_reviews,
                                reappearance_count,
                                price_changes,
                                last_price,
                                lot_id,
                            ),
                        )

                # Periodic aggregate sample check (only when there are active listings or forced)
                if current_map:
                    last_sample_row = conn.execute(
                        "SELECT sampled_at FROM market_sku_samples WHERE canonical_sku = ? ORDER BY sampled_at DESC LIMIT 1",
                        (sku,),
                    ).fetchone()
                    last_sampled_at = last_sample_row["sampled_at"] if last_sample_row else 0.0

                    if force_sample or (ts - last_sampled_at) >= sample_interval_seconds:
                        active_prices = sorted(float(l["price"]) for l in current_map.values())
                        min_p = active_prices[0]
                        max_p = active_prices[-1]
                        p10_p = compute_quantile(active_prices, 0.10)
                        p25_p = compute_quantile(active_prices, 0.25)
                        p50_p = compute_quantile(active_prices, 0.50)
                        p90_p = compute_quantile(active_prices, 0.90)
                        dispersion = (p90_p - p10_p) / max(0.01, p50_p)
                        mad_p = compute_mad(active_prices)
                        unique_sellers = len({l.get("seller", "") for l in current_map.values()})
                        total_stock = sum(int(l.get("stock", 1)) for l in current_map.values())

                        sample_meta = {
                            "active_listings": len(current_map),
                            "unique_sellers": unique_sellers,
                            "total_stock": total_stock,
                            "prices_count": len(active_prices),
                        }

                        conn.execute(
                            """INSERT INTO market_sku_samples
                               (canonical_sku, sampled_at, active_listings, unique_sellers, total_stock,
                                min_price, p10_price, p25_price, p50_price, p90_price, max_price,
                                price_dispersion, mad_price, metrics_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                sku,
                                ts,
                                len(current_map),
                                unique_sellers,
                                total_stock,
                                min_p,
                                p10_p,
                                p25_p,
                                p50_p,
                                p90_p,
                                max_p,
                                dispersion,
                                mad_p,
                                json.dumps(sample_meta, ensure_ascii=False),
                            ),
                        )
                        summary["samples_created"] += 1

        return summary

    def get_market_lots_for_sku(
        self, canonical_sku: str, active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """Retrieves recorded lot records for a canonical SKU."""
        with self._get_connection() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM market_history_lots WHERE canonical_sku = ? AND is_active = 1 ORDER BY price ASC",
                    (canonical_sku,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM market_history_lots WHERE canonical_sku = ? ORDER BY last_seen_at DESC",
                    (canonical_sku,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_market_lot(self, lot_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single lot record by lot_id."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_history_lots WHERE lot_id = ?",
                (lot_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_market_events_for_sku(
        self, canonical_sku: str, since_timestamp: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Retrieves event audit log for a canonical SKU."""
        with self._get_connection() as conn:
            if since_timestamp is not None:
                rows = conn.execute(
                    "SELECT * FROM market_lot_events WHERE canonical_sku = ? AND timestamp >= ? ORDER BY timestamp ASC",
                    (canonical_sku, since_timestamp),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM market_lot_events WHERE canonical_sku = ? ORDER BY timestamp ASC",
                    (canonical_sku,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_market_samples_for_sku(
        self, canonical_sku: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Retrieves periodic market aggregate samples for a canonical SKU in descending time order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM market_sku_samples WHERE canonical_sku = ? ORDER BY sampled_at DESC LIMIT ?",
                (canonical_sku, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_sku_reference_price(self, canonical_sku: str) -> Optional[Dict[str, float]]:
        """
        Computes robust market reference price from active listings:
        median (P50), P25, P10, P90, MAD, dispersion, and min/max.
        Does NOT rely on legacy category benchmark or hardcoded constants.
        """
        active_lots = self.get_market_lots_for_sku(canonical_sku, active_only=True)
        if not active_lots:
            # Fallback to latest sample if available
            samples = self.get_market_samples_for_sku(canonical_sku, limit=1)
            if samples:
                s = samples[0]
                return {
                    "count": float(s["active_listings"]),
                    "min": s["min_price"],
                    "p10": s["p10_price"],
                    "p25": s["p25_price"],
                    "median": s["p50_price"],
                    "p90": s["p90_price"],
                    "max": s["max_price"],
                    "dispersion": s["price_dispersion"],
                    "mad": s["mad_price"],
                }
            return None

        prices = sorted(float(l["price"]) for l in active_lots)
        p50 = compute_quantile(prices, 0.50)
        p10 = compute_quantile(prices, 0.10)
        p25 = compute_quantile(prices, 0.25)
        p90 = compute_quantile(prices, 0.90)
        dispersion = round((p90 - p10) / max(0.01, p50), 4)

        return {
            "count": float(len(prices)),
            "min": prices[0],
            "p10": p10,
            "p25": p25,
            "median": p50,
            "p90": p90,
            "max": prices[-1],
            "dispersion": dispersion,
            "mad": compute_mad(prices),
        }

    def get_discovery_skus(self, active_only: bool = True) -> List[str]:
        """Returns list of all discovery canonical SKUs recorded in market history."""
        with self._get_connection() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT DISTINCT canonical_sku FROM market_history_lots WHERE canonical_sku LIKE 'tf2_disc:%' AND is_active = 1 ORDER BY canonical_sku ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT canonical_sku FROM market_history_lots WHERE canonical_sku LIKE 'tf2_disc:%' ORDER BY canonical_sku ASC"
                ).fetchall()
            return [r[0] for r in rows]

    def get_all_tracked_skus(self, active_only: bool = True) -> List[str]:
        """Returns all distinct canonical SKUs (benchmarks + discovery) present in market history."""
        with self._get_connection() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT DISTINCT canonical_sku FROM market_history_lots WHERE is_active = 1 ORDER BY canonical_sku ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT canonical_sku FROM market_history_lots ORDER BY canonical_sku ASC"
                ).fetchall()
            return [r[0] for r in rows]

    # ─────────────────────────────────────────────────────────────
    # Read-only account-market history (v0.5)
    # ─────────────────────────────────────────────────────────────

    def record_account_market_observation(
        self,
        market_id: str,
        observed_lots: List[Dict[str, Any]],
        now: Optional[float] = None,
        sample_interval_seconds: float = 180.0,
        force_sample: bool = False,
        snapshot_quality: str = "COMPLETE",
        snapshot_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record one integrity-qualified scan of exactly one account market.

        Absence can only produce DISAPPEARED for a COMPLETE snapshot inside
        ``market_id``. PARTIAL/FAILED scans preserve prior active state.
        """
        processing_started = time.monotonic()
        from auto_flipper.account_cohort_normalizer import (
            ACCOUNT_OBSERVATION_SOURCE,
            normalize_account_lot,
        )
        from auto_flipper.config import ACCOUNT_CHEAP_RUB_THRESHOLD, ACCOUNT_MARKET_SEEDS

        if market_id not in ACCOUNT_MARKET_SEEDS:
            raise ValueError("unsupported account market")
        node_id = int(ACCOUNT_MARKET_SEEDS[market_id]["node_id"])
        if any(int(lot.get("node_id", node_id)) != node_id for lot in observed_lots):
            raise ValueError("mixed market observation is forbidden")
        ts = time.time() if now is None else float(now)
        quality = str(getattr(snapshot_quality, "value", snapshot_quality)).upper()
        if quality not in ("COMPLETE", "PARTIAL", "FAILED"):
            raise ValueError("invalid snapshot quality")
        diagnostics = dict(snapshot_diagnostics or {})
        diagnostics.setdefault("node_id", node_id)
        diagnostics.setdefault("advertised_market_count", None)
        diagnostics.setdefault("parsed_count", len(observed_lots))
        diagnostics.setdefault("coverage_ratio", None)
        diagnostics.setdefault("fetch_success", quality != "FAILED")
        diagnostics.setdefault("parse_success", quality != "FAILED")
        diagnostics.setdefault("scan_duration", 0.0)
        diagnostics.setdefault("http_status", None)
        diagnostics.setdefault("failure_reason", "")
        normalized = []
        for lot in observed_lots:
            if "lot_id" not in lot:
                continue
            result = normalize_account_lot(market_id, lot)
            normalized.append((lot, result))
        current = {str(lot["lot_id"]): (lot, result) for lot, result in normalized}
        summary = {"timestamp": ts, "market_id": market_id, "new_count": 0,
                   "disappearance_count": 0, "reappearance_count": 0,
                   "price_change_count": 0, "stock_change_count": 0,
                   "samples_created": 0, "cohort_samples_created": 0,
                   "lots_observed": len(current),
                   "classified": sum(1 for _, r in normalized if r.classified),
                   "snapshot_quality": quality,
                   "reconciliation_performed": quality == "COMPLETE"}

        def price_of(lot):
            try:
                return float(lot.get("price")) if lot.get("price") is not None else None
            except (TypeError, ValueError):
                return None

        with self._lock, self._get_connection() as conn:
            conn.execute("""INSERT INTO account_market_scans
                (market_id,node_id,started_at,advertised_market_count,parsed_count,coverage_ratio,
                 fetch_success,parse_success,snapshot_quality,scan_duration,http_status,failure_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (market_id, int(diagnostics["node_id"]), ts, diagnostics["advertised_market_count"],
                 int(diagnostics["parsed_count"]), diagnostics["coverage_ratio"],
                 int(bool(diagnostics["fetch_success"])), int(bool(diagnostics["parse_success"])),
                 quality, float(diagnostics["scan_duration"]), diagnostics["http_status"],
                 str(diagnostics["failure_reason"])))
            if quality == "FAILED":
                return summary
            previous_rows = conn.execute(
                "SELECT * FROM account_market_lots WHERE market_id=? AND is_active=1", (market_id,)
            ).fetchall()
            previous = {row["lot_id"]: dict(row) for row in previous_rows}
            for lot_id in (sorted(set(previous) - set(current)) if quality == "COMPLETE" else ()):
                old = previous[lot_id]
                conn.execute("UPDATE account_market_lots SET is_active=0, disappeared_at=? WHERE market_id=? AND lot_id=?",
                             (ts, market_id, lot_id))
                conn.execute("""INSERT INTO account_market_events
                    (market_id,lot_id,cohort_id,event_type,old_price,new_price,old_stock,new_stock,timestamp)
                    VALUES(?,?,?,'DISAPPEARED',?,NULL,?,NULL,?)""",
                             (market_id, lot_id, old["cohort_id"], old["price"], old["stock"], ts))
                summary["disappearance_count"] += 1

            for lot_id in sorted(current):
                lot, result = current[lot_id]
                price = price_of(lot)
                stock = max(1, int(lot.get("stock") or 1))
                seller = str(lot.get("seller") or "Unknown")
                existing_row = conn.execute(
                    "SELECT * FROM account_market_lots WHERE market_id=? AND lot_id=?", (market_id, lot_id)
                ).fetchone()
                if existing_row is None:
                    conn.execute("""INSERT INTO account_market_lots
                        (market_id,lot_id,node_id,seller,seller_rating,seller_reviews,price,currency,stock,title,url,
                         cohort_id,cohort_confidence,classified,features_json,risk_flags_json,source_type,
                         purchase_eligible,first_seen_at,last_seen_at,observed_at,is_active,disappeared_at,
                         reappearance_count,price_change_count)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,1,NULL,0,0)""",
                        (market_id, lot_id, node_id, seller, float(lot.get("seller_rating") or 0),
                         int(lot.get("seller_reviews") or 0), price, str(lot.get("currency") or "UNKNOWN").upper(),
                         stock, str(lot.get("title") or ""), str(lot.get("url") or ""), result.cohort_id,
                         result.confidence, int(result.classified), json.dumps(result.features, ensure_ascii=False, sort_keys=True),
                         json.dumps(list(result.risk_flags), ensure_ascii=False), ACCOUNT_OBSERVATION_SOURCE, ts, ts, ts))
                    if quality == "COMPLETE":
                        conn.execute("""INSERT INTO account_market_events
                            (market_id,lot_id,cohort_id,event_type,old_price,new_price,old_stock,new_stock,timestamp)
                            VALUES(?,?,?,'NEW',NULL,?,NULL,?,?)""",
                            (market_id, lot_id, result.cohort_id, price, stock, ts))
                        summary["new_count"] += 1
                else:
                    old = dict(existing_row)
                    reappeared = not bool(old["is_active"])
                    price_changed = old["price"] != price
                    stock_changed = int(old["stock"]) != stock
                    if reappeared and quality == "COMPLETE":
                        conn.execute("""INSERT INTO account_market_events
                            (market_id,lot_id,cohort_id,event_type,old_price,new_price,old_stock,new_stock,timestamp)
                            VALUES(?,?,?,'REAPPEARED',?,?,?,?,?)""",
                            (market_id, lot_id, result.cohort_id, old["price"], price, old["stock"], stock, ts))
                        summary["reappearance_count"] += 1
                    if price_changed and quality == "COMPLETE":
                        conn.execute("""INSERT INTO account_market_events
                            (market_id,lot_id,cohort_id,event_type,old_price,new_price,old_stock,new_stock,timestamp)
                            VALUES(?,?,?,'PRICE_CHANGE',?,?,?,?,?)""",
                            (market_id, lot_id, result.cohort_id, old["price"], price, old["stock"], stock, ts))
                        summary["price_change_count"] += 1
                    if stock_changed and not reappeared and quality == "COMPLETE":
                        conn.execute("""INSERT INTO account_market_events
                            (market_id,lot_id,cohort_id,event_type,old_price,new_price,old_stock,new_stock,timestamp)
                            VALUES(?,?,?,'STOCK_CHANGE',?,?,?,?,?)""",
                            (market_id, lot_id, result.cohort_id, old["price"], price, old["stock"], stock, ts))
                        summary["stock_change_count"] += 1
                    conn.execute("""UPDATE account_market_lots SET seller=?,seller_rating=?,seller_reviews=?,price=?,
                        currency=?,stock=?,title=?,url=?,cohort_id=?,cohort_confidence=?,classified=?,features_json=?,
                        risk_flags_json=?,source_type=?,purchase_eligible=0,last_seen_at=?,observed_at=?,is_active=1,
                        disappeared_at=NULL,reappearance_count=?,price_change_count=? WHERE market_id=? AND lot_id=?""",
                        (seller, float(lot.get("seller_rating") or 0), int(lot.get("seller_reviews") or 0), price,
                         str(lot.get("currency") or "UNKNOWN").upper(), stock, str(lot.get("title") or ""),
                         str(lot.get("url") or ""), result.cohort_id, result.confidence, int(result.classified),
                         json.dumps(result.features, ensure_ascii=False, sort_keys=True),
                         json.dumps(list(result.risk_flags), ensure_ascii=False), ACCOUNT_OBSERVATION_SOURCE, ts, ts,
                          int(old["reappearance_count"]) + int(reappeared and quality == "COMPLETE"),
                          int(old["price_change_count"]) + int(price_changed and quality == "COMPLETE"), market_id, lot_id))

            last = conn.execute("SELECT timestamp FROM account_market_samples WHERE market_id=? ORDER BY timestamp DESC LIMIT 1",
                                (market_id,)).fetchone()
            if force_sample or last is None or ts - float(last["timestamp"]) >= sample_interval_seconds:
                if quality == "COMPLETE":
                    rows = conn.execute("SELECT * FROM account_market_lots WHERE market_id=? AND is_active=1", (market_id,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM account_market_lots WHERE market_id=? AND observed_at=?", (market_id, ts)).fetchall()
                active = [dict(row) for row in rows]
                rub = [r for r in active if r["currency"] == "RUB" and r["price"] is not None]
                cheap = [r for r in rub if float(r["price"]) <= ACCOUNT_CHEAP_RUB_THRESHOLD]
                sellers = {r["seller"] for r in active}
                classified = [r for r in active if r["classified"]]
                metadata = {"unknown_currency_count": len(active) - len(rub), "source_type": ACCOUNT_OBSERVATION_SOURCE,
                            "purchase_eligible": False, "snapshot_quality": quality,
                            "advertised_market_count": diagnostics["advertised_market_count"],
                            "coverage_ratio": diagnostics["coverage_ratio"]}
                conn.execute("""INSERT INTO account_market_samples
                    (timestamp,market_id,active_lots,independent_sellers,cheap_lot_count,
                     cheap_independent_sellers,classified_lots,metrics_json) VALUES(?,?,?,?,?,?,?,?)""",
                    (ts, market_id, len(active), len(sellers), len(cheap), len({r["seller"] for r in cheap}),
                     len(classified), json.dumps(metadata, ensure_ascii=False, sort_keys=True)))
                summary["samples_created"] += 1

                events = conn.execute("SELECT cohort_id,event_type FROM account_market_events WHERE market_id=? AND timestamp>? AND timestamp<=?",
                                      (market_id, float(last["timestamp"]) if last else -1, ts)).fetchall()
                counts: Dict[Tuple[str, str], int] = {}
                for event in events:
                    counts[(event["cohort_id"], event["event_type"])] = counts.get((event["cohort_id"], event["event_type"]), 0) + 1
                cohort_ids = sorted({r["cohort_id"] for r in classified})
                for cohort_id in cohort_ids:
                    cohort_rows = [r for r in classified if r["cohort_id"] == cohort_id]
                    prices = sorted(float(r["price"]) for r in cohort_rows if r["currency"] == "RUB" and r["price"] is not None)
                    if prices:
                        p10, p25, p50 = compute_quantile(prices, .10), compute_quantile(prices, .25), compute_quantile(prices, .50)
                        p75, p90 = compute_quantile(prices, .75), compute_quantile(prices, .90)
                        dispersion = round((p90 - p10) / max(.01, p50), 4)
                        min_price, max_price, mad = prices[0], prices[-1], compute_mad(prices)
                    else:
                        p10 = p25 = p50 = p75 = p90 = min_price = max_price = mad = dispersion = None
                    conn.execute("""INSERT INTO account_cohort_samples
                        (timestamp,market_id,cohort_id,active_lots,independent_sellers,p10,p25,p50,p75,p90,
                         min_price,max_price,mad,dispersion,cheap_lot_count,new_count,disappearance_count,reappearance_count)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ts, market_id, cohort_id, len(cohort_rows), len({r["seller"] for r in cohort_rows}),
                         p10, p25, p50, p75, p90, min_price, max_price, mad, dispersion,
                         sum(1 for r in cohort_rows if r["currency"] == "RUB" and r["price"] is not None and float(r["price"]) <= ACCOUNT_CHEAP_RUB_THRESHOLD),
                         counts.get((cohort_id, "NEW"), 0), counts.get((cohort_id, "DISAPPEARED"), 0),
                         counts.get((cohort_id, "REAPPEARED"), 0)))
                    summary["cohort_samples_created"] += 1
            total_scan_duration = float(diagnostics["scan_duration"]) + (time.monotonic() - processing_started)
            conn.execute("UPDATE account_market_scans SET scan_duration=? WHERE market_id=? AND started_at=?",
                         (total_scan_duration, market_id, ts))
            summary["scan_duration"] = total_scan_duration
        return summary

    def get_account_market_lots(self, market_id: str, active_only: bool = True) -> List[Dict[str, Any]]:
        query = "SELECT * FROM account_market_lots WHERE market_id=?"
        if active_only:
            query += " AND is_active=1"
        query += " ORDER BY price IS NULL, price, lot_id"
        with self._get_connection() as conn:
            rows = conn.execute(query, (market_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json"))
                item["risk_flags"] = json.loads(item.pop("risk_flags_json"))
                result.append(item)
            return result

    def get_account_market_events(self, market_id: str, since_timestamp: Optional[float] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if since_timestamp is None:
                rows = conn.execute("SELECT * FROM account_market_events WHERE market_id=? ORDER BY timestamp,event_id",
                                    (market_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM account_market_events WHERE market_id=? AND timestamp>=? ORDER BY timestamp,event_id",
                                    (market_id, since_timestamp)).fetchall()
            return [dict(row) for row in rows]

    def get_account_market_samples(self, market_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM account_market_samples WHERE market_id=? ORDER BY timestamp DESC LIMIT ?",
                                (market_id, int(limit))).fetchall()
            return [dict(row) for row in rows]

    def get_account_cohort_samples(self, market_id: str, cohort_id: Optional[str] = None,
                                   limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if cohort_id is None:
                rows = conn.execute("SELECT * FROM account_cohort_samples WHERE market_id=? ORDER BY timestamp DESC,cohort_id LIMIT ?",
                                    (market_id, int(limit))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM account_cohort_samples WHERE market_id=? AND cohort_id=? ORDER BY timestamp DESC LIMIT ?",
                                    (market_id, cohort_id, int(limit))).fetchall()
            return [dict(row) for row in rows]

    def get_account_market_scans(self, market_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM account_market_scans WHERE market_id=? ORDER BY started_at DESC,id DESC LIMIT ?",
                (market_id, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_latest_account_market_scan(self, market_id: str) -> Optional[Dict[str, Any]]:
        rows = self.get_account_market_scans(market_id, limit=1)
        return rows[0] if rows else None

    def get_last_complete_account_market_count(self, market_id: str) -> Optional[int]:
        with self._get_connection() as conn:
            row = conn.execute(
                """SELECT parsed_count FROM account_market_scans
                   WHERE market_id=? AND snapshot_quality='COMPLETE'
                   ORDER BY started_at DESC,id DESC LIMIT 1""", (market_id,)
            ).fetchone()
            return int(row["parsed_count"]) if row else None

    def get_account_market_lots_observed_at(self, market_id: str, observed_at: float) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM account_market_lots WHERE market_id=? AND observed_at=? ORDER BY price IS NULL,price,lot_id",
                (market_id, float(observed_at)),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json"))
                item["risk_flags"] = json.loads(item.pop("risk_flags_json"))
                result.append(item)
            return result
