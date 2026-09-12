"""
test_liquidity_calibration.py — Offline test harness for liquidity measurement layer.

Verifies:
1. Exact matching for Mann Co. Supply Crate Key (tf2:5021;6)
2. Exact matching for Tour of Duty Ticket (tf2:725;6)
3. Ambiguous / modified / package titles -> UNKNOWN
4. first_seen_at / last_seen_at tracking
5. Disappearance transition and event logging
6. Reappearance transition, count increment, and event logging
7. Price changes tracking and event logging
8. Absence of false 'sold' / 'sales' labeling for market disappearances
9. Liquidity score bounded within [0, 100] across boundary cases
10. LOW confidence on insufficient history duration or sample count
11. Deterministic score for identical historical inputs
12. Benchmark label does not artificially change or boost score
13. No liquidity score can trigger checkout or bypass trade admission
14. Robust reference price quantiles and MAD calculation
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from auto_flipper.database import Database
from auto_flipper.liquidity import (
    LiquidityConfig,
    LiquidityMetrics,
    compute_liquidity_metrics,
    evaluate_liquidity,
    evaluate_sku_liquidity,
)
from auto_flipper.market_store import compute_mad, compute_quantile
from auto_flipper.sku_matcher import (
    BENCHMARK_SKUS,
    SKU_TF2_KEY,
    SKU_TF2_TICKET,
    SKU_UNKNOWN,
    is_benchmark_sku,
    match_sku,
)


class TestExactSkuMatcher(unittest.TestCase):
    """Verifies strict fail-closed exact matching for benchmark SKUs."""

    def test_exact_matching_key(self):
        valid_key_titles = [
            "Mann Co. Supply Crate Key",
            "mann co supply crate key",
            "Mann Co. Supply Crate Key - Instant Delivery",
            "Mann Box Key",
            "Mann Ko Key",
            "TF2 Key",
            "tf2 key",
            "TF2 Keys",
            "ТФ2 Ключ",
            "тф2 ключ",
            "Ключ от ящика Манн Ко",
            "Ключ от ящика Манн.Ко",
            "Ключ от ящика Манн",
            "Ключ Манн Ко",
            "Ключи от ящика Манн Ко",
            "Манн Ко Ключ",
            "Манко Ключ",
            "Monco Key",
            "🖤 TF2 Ключ от ящика Манн Ко - Mann Co. Supply Crate Key [От 1 шт.]🖤",
            "🔑КЛЮЧ ОТ ЯЩИКА МАНН КО🔑Mann Co. Supply Crate Key🔑 ТФ2🔑TF2 KEY🔑",
            "⚡ [TF2] Ключ от ящика Манн / Mann Co. Supply Crate Key⚡",
        ]
        for title in valid_key_titles:
            sku = match_sku(1808, title)
            self.assertEqual(
                sku,
                SKU_TF2_KEY,
                f"Failed to match valid key title: '{title}' (got {sku})",
            )

    def test_exact_matching_ticket(self):
        valid_ticket_titles = [
            "Tour of Duty Ticket",
            "tour of duty ticket",
            "Tour of Duty",
            "Командировочный билет",
            "командировочный билет",
            "Командировочные билеты",
            "Билет на командировку",
            "билет на командировку",
            "Билет на службу",
            "Билет МВМ",
            "Билет MvM",
            "билет mvm",
            "🎫 КОМАНДИРОВОЧНЫЙ БИЛЕТ TF2 | TOUR OF DUTY | ⚡ МГНОВЕННО | 💸 ВЫГОДНО",
            "⚡[TF2] Командировочный билет / Tour of Duty Ticket⚡",
            "🟩【 Командировочный билет 】🟩【 Tour of Duty Ticket 】🟩билет МВМ🟩",
            "Командировочный билет / Tour of Duty Ticket",
        ]
        for title in valid_ticket_titles:
            sku = match_sku(1808, title)
            self.assertEqual(
                sku,
                SKU_TF2_TICKET,
                f"Failed to match valid ticket title: '{title}' (got {sku})",
            )

    def test_ambiguous_title_returns_unknown(self):
        invalid_titles = [
            # Mann Co Store Package (NOT key)
            "Mann Co. Store Package",
            "Пакет магазина Манн Ко",
            "Пакет магазина Манн Ко / Mann Co. Store Package",
            # Crates/cases without key
            "Ящик со снаряжением Манн Ко тиража №92",
            "Ящик запоздавшего лета ограниченной серии тиража №86",
            "Mann Co. Supply Crate",
            # Weapons with 'Box' or 'Ящик'
            "Strange Professional Killstreak Black Box",
            "Черный ящик странного типа",
            "Direct Hit/Black Box Kit",
            # Wrenches
            "Гаечный ключ странного типа",
            "Specialized Killstreak Wrench Kit",
            # Modified item qualities
            "Unusual Mann Co. Supply Crate Key",
            "Strange Mann Co. Supply Crate Key",
            "Vintage Mann Co. Supply Crate Key",
            "Australium Black Box",
            # Other TF2 items
            "Расширитель рюкзака / Backpack Expander",
            "Ярлык для описания",
            "Ярлык для имени",
            "Билетер / Аксессуар для Разведчика",
            "Билетёр",
            "Ticket Boy",
            "Squad Surplus Voucher",
            "Ваучер отряда MvM",
            # Mixed Key and Ticket bundle
            "Ключ TF2 и Tour of Duty Ticket",
            "Mann Co Key + Командировочный билет",
            # Random games / accounts
            "Ключи TF2 + даю в подарок ключ от рандом игры в Steam",
            "TF2 Аккаунт с инвентарем",
            # Empty / malformed
            "",
            "   ",
            "Random string with no matching items",
        ]
        for title in invalid_titles:
            sku = match_sku(1808, title)
            self.assertEqual(
                sku,
                SKU_UNKNOWN,
                f"Expected UNKNOWN for title '{title}', got '{sku}'",
            )

    def test_non_tf2_node_returns_unknown(self):
        # Even with exact title, different FunPay node must fail closed
        self.assertEqual(match_sku(1355, "Mann Co. Supply Crate Key"), SKU_UNKNOWN)
        self.assertEqual(match_sku(925, "Tour of Duty Ticket"), SKU_UNKNOWN)
        self.assertEqual(match_sku(0, "Mann Co. Supply Crate Key"), SKU_UNKNOWN)


class TestMarketHistoryStorage(unittest.TestCase):
    """Verifies SQLite market history tracking, transitions, and event logging."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test-mkt-history-")
        self.db_path = os.path.join(self.test_dir, "test_market.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_first_seen_and_last_seen_tracking(self):
        t1 = 1700000000.0
        t2 = 1700000300.0

        lot = {
            "lot_id": "funpay_1001",
            "node_id": 1808,
            "title": "Mann Co. Supply Crate Key",
            "price": 170.0,
            "stock": 5,
            "seller": "SellerA",
            "seller_rating": 5.0,
            "seller_reviews": 100,
            "url": "https://funpay.com/lots/offer?id=1001",
        }

        # First observation at t1
        self.db.record_market_observation([lot], now=t1)
        rec = self.db.get_market_lot("funpay_1001")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["first_seen_at"], t1)
        self.assertEqual(rec["last_seen_at"], t1)
        self.assertEqual(rec["is_active"], 1)

        # Second observation at t2
        self.db.record_market_observation([lot], now=t2)
        rec2 = self.db.get_market_lot("funpay_1001")
        self.assertEqual(rec2["first_seen_at"], t1)  # Preserves initial seen time
        self.assertEqual(rec2["last_seen_at"], t2)   # Updates last seen time

    def test_disappearance_transition(self):
        t1 = 1700000000.0
        t2 = 1700000100.0

        lot1 = {
            "lot_id": "funpay_2001",
            "node_id": 1808,
            "title": "Tour of Duty Ticket",
            "price": 80.0,
            "stock": 10,
            "seller": "SellerB",
            "url": "https://funpay.com/lots/offer?id=2001",
        }

        # Lot appears at t1
        self.db.record_market_observation([lot1], now=t1)
        self.assertEqual(len(self.db.get_market_lots_for_sku(SKU_TF2_TICKET, active_only=True)), 1)

        # Next scan at t2 without lot1
        self.db.record_market_observation([], now=t2)

        # Lot must be marked disappeared
        rec = self.db.get_market_lot("funpay_2001")
        self.assertEqual(rec["is_active"], 0)
        self.assertEqual(rec["disappeared_at"], t2)

        # Active listings count is now 0
        self.assertEqual(len(self.db.get_market_lots_for_sku(SKU_TF2_TICKET, active_only=True)), 0)

        # Verify event logged
        events = self.db.get_market_events_for_sku(SKU_TF2_TICKET)
        event_types = [e["event_type"] for e in events]
        self.assertIn("DISAPPEARED", event_types)

    def test_reappearance_transition(self):
        t1 = 1700000000.0
        t2 = 1700000100.0
        t3 = 1700000200.0

        lot = {
            "lot_id": "funpay_3001",
            "node_id": 1808,
            "title": "Mann Co. Supply Crate Key",
            "price": 175.0,
            "stock": 3,
            "seller": "SellerC",
            "url": "https://funpay.com/lots/offer?id=3001",
        }

        # 1. Seen at t1
        self.db.record_market_observation([lot], now=t1)
        self.assertEqual(self.db.get_market_lot("funpay_3001")["reappearance_count"], 0)

        # 2. Disappears at t2
        self.db.record_market_observation([], now=t2)
        self.assertEqual(self.db.get_market_lot("funpay_3001")["is_active"], 0)

        # 3. Reappears at t3
        self.db.record_market_observation([lot], now=t3)
        rec = self.db.get_market_lot("funpay_3001")
        self.assertEqual(rec["is_active"], 1)
        self.assertEqual(rec["reappearance_count"], 1)

        events = self.db.get_market_events_for_sku(SKU_TF2_KEY)
        types = [e["event_type"] for e in events]
        self.assertEqual(types, ["NEW", "DISAPPEARED", "REAPPEARED"])

    def test_price_change_tracking(self):
        t1 = 1700000000.0
        t2 = 1700000300.0

        lot = {
            "lot_id": "funpay_4001",
            "node_id": 1808,
            "title": "Mann Co. Supply Crate Key",
            "price": 170.0,
            "stock": 2,
            "seller": "SellerD",
            "url": "https://funpay.com/lots/offer?id=4001",
        }
        self.db.record_market_observation([lot], now=t1)

        # Price updates to 168.0
        lot_updated = dict(lot, price=168.0)
        self.db.record_market_observation([lot_updated], now=t2)

        rec = self.db.get_market_lot("funpay_4001")
        self.assertEqual(rec["price"], 168.0)
        self.assertEqual(rec["price_changes"], 1)

        events = self.db.get_market_events_for_sku(SKU_TF2_KEY)
        price_events = [e for e in events if e["event_type"] == "PRICE_CHANGE"]
        self.assertEqual(len(price_events), 1)
        self.assertEqual(price_events[0]["old_price"], 170.0)
        self.assertEqual(price_events[0]["new_price"], 168.0)

    def test_absence_of_false_sold_label(self):
        """Disappearance must NEVER be labeled 'sold' or 'confirmed_sale'."""
        t1 = 1700000000.0
        t2 = 1700000100.0

        lot = {
            "lot_id": "funpay_5001",
            "node_id": 1808,
            "title": "Tour of Duty Ticket",
            "price": 79.0,
            "seller": "SellerE",
            "url": "https://funpay.com/lots/offer?id=5001",
        }
        self.db.record_market_observation([lot], now=t1)
        self.db.record_market_observation([], now=t2)

        # Check market history table column names and values
        rec = self.db.get_market_lot("funpay_5001")
        self.assertNotIn("sold", rec)
        self.assertNotIn("is_sold", rec)
        self.assertEqual(rec["is_active"], 0)

        # Check events table
        events = self.db.get_market_events_for_sku(SKU_TF2_TICKET)
        for e in events:
            self.assertNotIn("SOLD", e["event_type"].upper())
            self.assertNotIn("SALE", e["event_type"].upper())


class TestLiquidityEvaluator(unittest.TestCase):
    """Verifies mathematical scoring, confidence boundaries, and isolation."""

    def test_liquidity_score_bounds_0_to_100(self):
        extreme_cases = [
            # 1. Empty market
            LiquidityMetrics(
                canonical_sku=SKU_TF2_KEY, active_listings=0, unique_sellers=0, observed_stock=0,
                min_price=0.0, p10_price=0.0, p25_price=0.0, p50_price=0.0, p90_price=0.0, max_price=0.0,
                price_dispersion=0.0, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.0,
                observation_duration_hours=0.0, sample_count=0, new_listings_count=0, new_listings_rate=0.0,
                disappearance_count=0, disappearance_rate=0.0, reappearance_count=0, reappearance_rate=0.0,
                reappearance_ratio=0.0, turnover_proxy=0.0, median_listing_age_hours=0.0,
                competition_depth_bottom=0, competition_depth_sellers=0,
            ),
            # 2. Hyper-liquid ideal market
            LiquidityMetrics(
                canonical_sku=SKU_TF2_KEY, active_listings=50, unique_sellers=25, observed_stock=500,
                min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=195.0,
                price_dispersion=0.05, p25_change_pct=0.5, p50_change_pct=0.2, price_volatility=0.005,
                observation_duration_hours=12.0, sample_count=120, new_listings_count=40, new_listings_rate=3.3,
                disappearance_count=35, disappearance_rate=2.9, reappearance_count=2, reappearance_rate=0.16,
                reappearance_ratio=0.057, turnover_proxy=2.75, median_listing_age_hours=4.5,
                competition_depth_bottom=10, competition_depth_sellers=8,
            ),
            # 3. Pathological chaotic market (extreme volatility and dispersion)
            LiquidityMetrics(
                canonical_sku=SKU_TF2_KEY, active_listings=3, unique_sellers=1, observed_stock=3,
                min_price=10.0, p10_price=50.0, p25_price=100.0, p50_price=500.0, p90_price=1500.0, max_price=5000.0,
                price_dispersion=2.9, p25_change_pct=-80.0, p50_change_pct=150.0, price_volatility=0.85,
                observation_duration_hours=5.0, sample_count=20, new_listings_count=10, new_listings_rate=2.0,
                disappearance_count=10, disappearance_rate=2.0, reappearance_count=10, reappearance_rate=2.0,
                reappearance_ratio=1.0, turnover_proxy=0.0, median_listing_age_hours=0.5,
                competition_depth_bottom=1, competition_depth_sellers=1,
            ),
        ]

        for m in extreme_cases:
            assessment = evaluate_liquidity(m)
            self.assertGreaterEqual(assessment.score, 0.0, f"Score under 0 for {m}")
            self.assertLessEqual(assessment.score, 100.0, f"Score above 100 for {m}")
            self.assertIn(assessment.confidence, ("LOW", "MEDIUM", "HIGH"))

    def test_low_confidence_on_insufficient_history(self):
        # Good market signals, but only 10 minutes (0.16h) and 2 samples
        metrics_short = LiquidityMetrics(
            canonical_sku=SKU_TF2_KEY, active_listings=20, unique_sellers=10, observed_stock=100,
            min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=190.0,
            price_dispersion=0.05, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.0,
            observation_duration_hours=0.16, sample_count=2, new_listings_count=1, new_listings_rate=1.0,
            disappearance_count=0, disappearance_rate=0.0, reappearance_count=0, reappearance_rate=0.0,
            reappearance_ratio=0.0, turnover_proxy=0.0, median_listing_age_hours=0.1,
            competition_depth_bottom=4, competition_depth_sellers=4,
        )
        assessment = evaluate_liquidity(metrics_short)
        self.assertEqual(assessment.confidence, "LOW")
        self.assertTrue(any("Insufficient history" in r for r in assessment.confidence_reasons))

    def test_deterministic_score_for_identical_inputs(self):
        metrics = LiquidityMetrics(
            canonical_sku=SKU_TF2_TICKET, active_listings=15, unique_sellers=8, observed_stock=80,
            min_price=75.0, p10_price=76.0, p25_price=78.0, p50_price=80.0, p90_price=85.0, max_price=95.0,
            price_dispersion=0.11, p25_change_pct=1.0, p50_change_pct=0.5, price_volatility=0.015,
            observation_duration_hours=2.5, sample_count=25, new_listings_count=8, new_listings_rate=3.2,
            disappearance_count=6, disappearance_rate=2.4, reappearance_count=1, reappearance_rate=0.4,
            reappearance_ratio=0.1667, turnover_proxy=2.0, median_listing_age_hours=1.8,
            competition_depth_bottom=3, competition_depth_sellers=3,
        )
        res1 = evaluate_liquidity(metrics)
        res2 = evaluate_liquidity(metrics)
        self.assertEqual(res1.score, res2.score)
        self.assertEqual(res1.confidence, res2.confidence)
        self.assertEqual(res1.sub_scores, res2.sub_scores)

    def test_benchmark_label_does_not_change_score(self):
        """Benchmark SKU vs non-benchmark SKU with identical metrics must receive identical score."""
        m_benchmark = LiquidityMetrics(
            canonical_sku=SKU_TF2_KEY, active_listings=15, unique_sellers=8, observed_stock=80,
            min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=190.0,
            price_dispersion=0.05, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.01,
            observation_duration_hours=3.0, sample_count=30, new_listings_count=5, new_listings_rate=1.6,
            disappearance_count=5, disappearance_rate=1.6, reappearance_count=0, reappearance_rate=0.0,
            reappearance_ratio=0.0, turnover_proxy=1.67, median_listing_age_hours=2.0,
            competition_depth_bottom=4, competition_depth_sellers=3,
        )
        m_arbitrary = LiquidityMetrics(
            canonical_sku="random_unlisted_game:item_999", active_listings=15, unique_sellers=8, observed_stock=80,
            min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=190.0,
            price_dispersion=0.05, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.01,
            observation_duration_hours=3.0, sample_count=30, new_listings_count=5, new_listings_rate=1.6,
            disappearance_count=5, disappearance_rate=1.6, reappearance_count=0, reappearance_rate=0.0,
            reappearance_ratio=0.0, turnover_proxy=1.67, median_listing_age_hours=2.0,
            competition_depth_bottom=4, competition_depth_sellers=3,
        )

        score_benchmark = evaluate_liquidity(m_benchmark).score
        score_arbitrary = evaluate_liquidity(m_arbitrary).score
        self.assertEqual(score_benchmark, score_arbitrary)

    def test_liquidity_score_cannot_trigger_checkout(self):
        """Liquidity score is strictly read-only analytics and cannot authorize or trigger checkout."""
        test_dir = tempfile.mkdtemp(prefix="test-mkt-eval-")
        try:
            db_path = os.path.join(test_dir, "test_eval.db")
            db = Database(db_path)

            # Record healthy key market
            lots = [
                {
                    "lot_id": f"funpay_lot_{i}",
                    "node_id": 1808,
                    "title": "Mann Co. Supply Crate Key",
                    "price": 170.0 + i,
                    "stock": 5,
                    "seller": f"Seller_{i}",
                    "url": f"https://funpay.com/lots/offer?id={i}",
                }
                for i in range(10)
            ]
            db.record_market_observation(lots, now=1700000000.0, force_sample=True)

            # Evaluate liquidity
            assessment = evaluate_sku_liquidity(db, SKU_TF2_KEY, now=1700000000.0)
            self.assertIsNotNone(assessment)

            # Verify no purchase intents or claims were created
            with db._get_connection() as conn:
                intents = conn.execute("SELECT count(*) FROM purchase_intents").fetchone()[0]
                claims = conn.execute("SELECT count(*) FROM purchase_claims").fetchone()[0]
                inventory = conn.execute("SELECT count(*) FROM flipper_inventory").fetchone()[0]
            self.assertEqual(intents, 0)
            self.assertEqual(claims, 0)
            self.assertEqual(inventory, 0)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)

    def test_reference_price_quantiles_and_mad(self):
        prices = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 114.0, 116.0, 118.0]
        # Median of 10 items: (108 + 110) / 2 = 109.0
        p50 = compute_quantile(prices, 0.50)
        self.assertEqual(p50, 109.0)

        # Deviations: 9, 7, 5, 3, 1, 1, 3, 5, 7, 9 -> sorted: 1, 1, 3, 3, 5, 5, 7, 7, 9, 9 -> median 5.0
        mad = compute_mad(prices)
        self.assertEqual(mad, 5.0)


class TestTelegramLiquidityCommand(unittest.IsolatedAsyncioTestCase):
    """Verifies Telegram /liquidity handler output and options."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test-tg-liq-")
        self.db_path = os.path.join(self.test_dir, "test_tg.db")
        self.db = Database(self.db_path)

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_cmd_liquidity_summary_and_detailed(self):
        from unittest.mock import AsyncMock, patch
        from auto_flipper.assistant_handlers import cmd_liquidity

        # 1. When empty
        msg = AsyncMock()
        msg.text = "/liquidity"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            self.assertTrue(msg.answer.called)
            reply = msg.answer.call_args[0][0]
            self.assertIn("История пока не накоплена", reply)

        # 2. Populate market with sample lots
        lots = [
            {
                "lot_id": f"funpay_key_{i}",
                "node_id": 1808,
                "title": "Mann Co. Supply Crate Key",
                "price": 170.0 + i,
                "stock": 10,
                "seller": f"Seller_{i}",
                "url": f"https://funpay.com/lots/offer?id={i}",
            }
            for i in range(12)
        ]
        self.db.record_market_observation(lots, now=1700000000.0, force_sample=True)

        msg.reset_mock()
        msg.text = "/liquidity"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            reply = msg.answer.call_args[0][0]
            self.assertIn("Mann Co. Supply Crate Key", reply)
            self.assertIn("Score:", reply)
            self.assertIn("Confidence:", reply)
            self.assertIn("Active lots: 12", reply)

        # 3. Detailed mode: /liquidity key
        msg.reset_mock()
        msg.text = "/liquidity key"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            reply = msg.answer.call_args[0][0]
            self.assertIn("Детальный профиль ликвидности", reply)
            self.assertIn("Метрики стакана:", reply)
            self.assertIn("Суб-оценки", reply)


if __name__ == "__main__":
    unittest.main()
