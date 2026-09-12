"""
test_discovery_and_blind_ranking.py — Unit tests for autonomous discovery, normalization,
candidate qualification, blind liquidity ranking, and checkout safety gates.

Verifies:
1. Deterministic title normalization (idempotence, emoji/junk/promo/quantity stripping)
2. Distinct items are never merged
3. Variations of the same item group together into the same canonical discovery SKU
4. Too small group does not become candidate (lots, sellers, samples, price ratio)
5. Discovery candidate cannot reach checkout, claims, or purchase admission
6. Benchmark status does not affect score
7. Ranking is strictly deterministic
8. LOW confidence on insufficient history
9. Telegram /liquidity top and /liquidity bottom handlers
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from auto_flipper.database import Database
from auto_flipper.discovery import (
    DISCOVERY_SKU_PREFIX,
    DiscoveryConfig,
    DiscoveryCandidate,
    check_candidate_qualification,
    extract_discovery_sku,
    is_discovery_sku,
    is_service_or_pack,
    normalize_title,
    pick_best_display_name,
    rank_market_liquidity,
)
from auto_flipper.liquidity import (
    LiquidityConfig,
    LiquidityMetrics,
    compute_liquidity_metrics,
    evaluate_liquidity,
)
from auto_flipper.sku_matcher import (
    BENCHMARK_SKUS,
    SKU_TF2_KEY,
    SKU_TF2_TICKET,
    SKU_UNKNOWN,
    get_sku_display_name,
    is_benchmark_sku,
    match_sku,
)


class TestDiscoveryNormalization(unittest.TestCase):
    """Verifies deterministic title normalization and clustering behavior."""

    def test_deterministic_title_normalization(self):
        variations = [
            "  ●▬▬▬✅⭐️Священная клятва  ",
            "Священная клятва",
            "🌟 СВЯЩЕННАЯ КЛЯТВА 🌟 [от 1 шт.] + ПОДАРОК 🎁",
            "【Священная клятва】⚡ БЫСТРАЯ ВЫДАЧА ⚡ 1 шт",
            "&quot;Священная клятва&quot; - Достану любые вещи",
        ]
        normalized = [normalize_title(v) for v in variations]
        expected = "священная клятва"
        for idx, norm in enumerate(normalized):
            self.assertEqual(norm, expected, f"Variation {idx} failed: '{variations[idx]}' -> '{norm}'")

        # Idempotence: normalizing already normalized string produces identical result
        self.assertEqual(normalize_title(expected), expected)

    def test_quantity_and_promo_stripping(self):
        titles = [
            ("[от 5 шт.] Насмешка: Конга", "насмешка конга"),
            ("Насмешка: Конга (от 1 шт.)", "насмешка конга"),
            ("Расширитель рюкзака x2 + подарок", "расширитель рюкзака"),
            ("Расширитель рюкзака 5 шт бонус за отзыв", "расширитель рюкзака"),
            ("Очищенный металл ref [10 шт] СМОТРИ ОПИСАНИЕ", "очищенный металл ref"),
            ("Кепка Эллиса 1 lvl автовыдача", "кепка эллиса"),
        ]
        for raw, exp in titles:
            self.assertEqual(normalize_title(raw), exp, f"Failed on '{raw}'")

    def test_distinct_items_not_merged(self):
        """Distinct weapons, taunts, crates, or cosmetics MUST NOT be merged."""
        distinct_pairs = [
            ("Насмешка: Конга", "Насмешка: Вприсядку"),
            ("Кепка Эллиса", "Кепка Манн Ко"),
            ("Шумелка — Зимний праздник", "Шумелка — Хэллоуин"),
            ("Зимний кейс с аксессуарами 2023", "Зимний кейс с аксессуарами 2025 года"),
            ("Strange Black Box", "Black Box"),
            ("Праздничный удар", "Воинский дух"),
            ("Очищенный металл", "Восстановленный металл"),
            ("Куртка на застежках", "Полярный пуловер"),
        ]
        for item1, item2 in distinct_pairs:
            norm1 = normalize_title(item1)
            norm2 = normalize_title(item2)
            self.assertNotEqual(norm1, norm2, f"Distinct items improperly merged: '{item1}' and '{item2}' -> '{norm1}'")

            sku1 = extract_discovery_sku(1808, item1)
            sku2 = extract_discovery_sku(1808, item2)
            self.assertNotEqual(sku1, sku2, f"Distinct items produced identical discovery SKU: {sku1}")

    def test_variations_of_one_obvious_item_grouped(self):
        """Variations of the same physical item must group into the exact same discovery SKU."""
        taunt_group = [
            "Насмешка: Злорадство",
            "🟨🪙🟨Насмешка: Злорадство🟨🪙🟨",
            "❤️Насмешка: Злорадство ❤️ Достану любые вещи ❤️",
            "🌟Насмешка: Злорадство🌟-🖤БОНУС ЗА ОТЗЫВ🖤",
        ]
        skus = {extract_discovery_sku(1808, t) for t in taunt_group}
        self.assertEqual(len(skus), 1, f"Expected 1 grouped SKU, got: {skus}")
        self.assertEqual(list(skus)[0], "tf2_disc:насмешка_злорадство")

        backpack_group = [
            "Расширитель рюкзака",
            "🎒✨РАСШИРИТЕЛЬ РЮКЗАКА✨ 🎒АВТОВЫДАЧА ✨ 🎒",
            "🟥⚡️Расширитель рюкзака🟥TF 2 🟥⚡️🟥без бана",
            "Расширитель рюкзака [от 1 шт.] ⚡ СРАЗУ ПОСЛЕ ОПЛАТЫ",
        ]
        skus_bp = {extract_discovery_sku(1808, t) for t in backpack_group}
        self.assertEqual(len(skus_bp), 1, f"Expected 1 grouped SKU for backpack expander, got: {skus_bp}")
        self.assertEqual(list(skus_bp)[0], "tf2_disc:расширитель_рюкзака")

    def test_services_and_packs_excluded_from_discovery(self):
        """Multi-item menus, choice services, and accounts must not create discovery item SKUs."""
        invalid_titles = [
            "➡️ Рескин на выбор: Слонобой / Сковорода / Одетый с иголочки ⬅️",
            "👥все оружия на все классы👥Пулемётчик хеви Снайпер Скаут и т.д🎁",
            "СЕТ НА НАЁМНИКОВ НА ВЫБОР: Тёмный рыцарь / Чумной док",
            "►Любой предмет TF2 по низкой цене.",
            "TF2 Аккаунт с инвентарем",
            "Ключ от рандомной игры Steam",
        ]
        for title in invalid_titles:
            sku = extract_discovery_sku(1808, title)
            self.assertIsNone(sku, f"Expected None for service/pack '{title}', got '{sku}'")

    def test_benchmarks_not_captured_as_discovery(self):
        """Benchmark Key and Ticket must be handled by benchmark matcher, returning None for discovery."""
        self.assertIsNone(extract_discovery_sku(1808, "Mann Co. Supply Crate Key"))
        self.assertIsNone(extract_discovery_sku(1808, "Tour of Duty Ticket"))
        self.assertIsNone(extract_discovery_sku(1808, "Командировочный билет"))
        self.assertIsNone(extract_discovery_sku(1808, "Ключ от ящика Манн Ко"))

    def test_pick_best_display_name(self):
        raws = [
            "●▬▬▬✅⭐️Священная клятва",
            "Священная клятва",
            "【Священная клятва】⚡ БЫСТРАЯ ВЫДАЧА ⚡ 1 шт",
        ]
        name = pick_best_display_name(raws, "священная клятва")
        self.assertEqual(name, "Священная клятва")


class TestDiscoveryQualification(unittest.TestCase):
    """Verifies candidate minimal filtering thresholds."""

    def setUp(self):
        self.cfg = DiscoveryConfig(
            min_active_lots=3,
            min_unique_sellers=3,
            min_market_samples=2,
            max_price_ratio=5.0,
            min_title_token_consistency=0.55,
        )

    def test_too_few_lots_rejected(self):
        lots = [
            {"price": 100.0, "seller": "S1", "title": "Item A"},
            {"price": 102.0, "seller": "S2", "title": "Item A"},
        ]
        samples = [{"sampled_at": 1}, {"sampled_at": 2}]
        is_qual, reasons = check_candidate_qualification(lots, samples, "item a", self.cfg)
        self.assertFalse(is_qual)
        self.assertTrue(any("Insufficient active lots" in r for r in reasons))

    def test_too_few_sellers_rejected(self):
        lots = [
            {"price": 100.0, "seller": "S1", "title": "Item B"},
            {"price": 101.0, "seller": "S1", "title": "Item B"},
            {"price": 102.0, "seller": "S1", "title": "Item B"},
        ]
        samples = [{"sampled_at": 1}, {"sampled_at": 2}]
        is_qual, reasons = check_candidate_qualification(lots, samples, "item b", self.cfg)
        self.assertFalse(is_qual)
        self.assertTrue(any("Insufficient unique sellers" in r for r in reasons))

    def test_too_few_samples_rejected(self):
        lots = [
            {"price": 100.0, "seller": "S1", "title": "Item C"},
            {"price": 101.0, "seller": "S2", "title": "Item C"},
            {"price": 102.0, "seller": "S3", "title": "Item C"},
        ]
        samples = [{"sampled_at": 1}]  # Only 1 sample < min_market_samples (2)
        is_qual, reasons = check_candidate_qualification(lots, samples, "item c", self.cfg)
        self.assertFalse(is_qual)
        self.assertTrue(any("Insufficient market samples" in r for r in reasons))

    def test_extreme_price_spread_rejected(self):
        lots = [
            {"price": 10.0, "seller": "S1", "title": "Item D"},
            {"price": 15.0, "seller": "S2", "title": "Item D"},
            {"price": 100.0, "seller": "S3", "title": "Item D"},  # 10x ratio > 5.0
        ]
        samples = [{"sampled_at": 1}, {"sampled_at": 2}]
        is_qual, reasons = check_candidate_qualification(lots, samples, "item d", self.cfg)
        self.assertFalse(is_qual)
        self.assertTrue(any("Price ratio too wide" in r for r in reasons))

    def test_qualified_candidate_passes(self):
        lots = [
            {"price": 500.0, "seller": "S1", "title": "Насмешка: Злорадство"},
            {"price": 520.0, "seller": "S2", "title": "Насмешка: Злорадство"},
            {"price": 550.0, "seller": "S3", "title": "Насмешка: Злорадство - Выгодная цена"},
        ]
        samples = [{"sampled_at": 1}, {"sampled_at": 2}]
        is_qual, reasons = check_candidate_qualification(lots, samples, "насмешка злорадство", self.cfg)
        self.assertTrue(is_qual)
        self.assertEqual(len(reasons), 0)


class TestCheckoutSafetyGates(unittest.TestCase):
    """Verifies that discovery candidates can NEVER reach checkout, claims, or admission."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test-disc-safety-")
        self.db_path = os.path.join(self.test_dir, "test_safety.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_claim_purchase_rejects_discovery_candidate(self):
        """safety_store.claim_purchase must raise PermissionError if category_id or candidate is a discovery item."""
        discovery_sku = "tf2_disc:насмешка_злорадство"
        with self.assertRaises(PermissionError) as ctx:
            self.db.claim_purchase(
                lot_id="funpay_disc_1",
                category_id=discovery_sku,
                price=500.0,
                dry_run=True,
            )
        self.assertIn("DISCOVERY_CANDIDATE_NOT_PURCHASABLE", str(ctx.exception))

    def test_check_capital_rejects_discovery_candidate(self):
        """trade_admission.check_capital must raise PermissionError on discovery SKUs."""
        from auto_flipper.trade_admission import check_capital

        review = {"route_cost": 0, "supplier_group": "default"}
        discovery_sku = "tf2_disc:расширитель_рюкзака"
        with self.assertRaises(PermissionError) as ctx:
            check_capital(self.db, dry_run=True, review=review, category_id=discovery_sku, price=100.0)
        self.assertIn("DISCOVERY_CANDIDATE_NOT_PURCHASABLE", str(ctx.exception))

    def test_assistant_engine_rejects_discovery_candidate(self):
        """assistant_engine.evaluate_reviewed_candidate must raise PermissionError on discovery candidates."""
        from auto_flipper.assistant_engine import AssistantWorkflow

        engine = AssistantWorkflow()
        engine.mode = "ASSIST"
        engine.is_emergency_stopped = False
        candidate = {
            "lot_id": "lot_disc_99",
            "observed_at": time.time(),
            "reviewed_at": time.time(),
            "payload": {
                "lot_id": "lot_disc_99",
                "canonical_sku": "tf2_disc:насмешка_конга",
                "category_id": "tf2_disc:насмешка_конга",
                "title": "Насмешка: Конга",
                "price": 600.0,
            },
            "review": {
                "route_cost": 0,
                "supplier_group": "test",
                "risk_profile": "safe",
            },
        }
        with self.assertRaises(PermissionError) as ctx:
            engine.evaluate_reviewed_candidate(candidate)
        self.assertIn("DISCOVERY_CANDIDATE_NOT_PURCHASABLE", str(ctx.exception))


class TestBlindRankingAndConfidence(unittest.TestCase):
    """Verifies blind ranking, deterministic sorting, and score isolation."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test-disc-rank-")
        self.db_path = os.path.join(self.test_dir, "test_rank.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_benchmark_status_does_not_affect_score(self):
        """Benchmark SKU vs discovery SKU with identical metrics receives identical score."""
        m_bm = LiquidityMetrics(
            canonical_sku=SKU_TF2_KEY, active_listings=10, unique_sellers=8, observed_stock=50,
            min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=190.0,
            price_dispersion=0.05, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.01,
            observation_duration_hours=2.0, sample_count=15, new_listings_count=3, new_listings_rate=1.5,
            disappearance_count=3, disappearance_rate=1.5, reappearance_count=0, reappearance_rate=0.0,
            reappearance_ratio=0.0, turnover_proxy=1.5, median_listing_age_hours=1.0,
            competition_depth_bottom=4, competition_depth_sellers=3,
        )
        m_disc = LiquidityMetrics(
            canonical_sku="tf2_disc:насмешка_злорадство", active_listings=10, unique_sellers=8, observed_stock=50,
            min_price=170.0, p10_price=171.0, p25_price=172.0, p50_price=175.0, p90_price=180.0, max_price=190.0,
            price_dispersion=0.05, p25_change_pct=0.0, p50_change_pct=0.0, price_volatility=0.01,
            observation_duration_hours=2.0, sample_count=15, new_listings_count=3, new_listings_rate=1.5,
            disappearance_count=3, disappearance_rate=1.5, reappearance_count=0, reappearance_rate=0.0,
            reappearance_ratio=0.0, turnover_proxy=1.5, median_listing_age_hours=1.0,
            competition_depth_bottom=4, competition_depth_sellers=3,
        )
        res_bm = evaluate_liquidity(m_bm)
        res_disc = evaluate_liquidity(m_disc)
        self.assertEqual(res_bm.score, res_disc.score)
        self.assertEqual(res_bm.sub_scores, res_disc.sub_scores)

    def test_ranking_is_strictly_deterministic(self):
        """Running rank_market_liquidity twice on same DB state produces identical ordering."""
        # Populate market with keys and two discovery candidates
        lots = [
            # 1. Benchmark Key
            {"lot_id": f"k_{i}", "node_id": 1808, "title": "Mann Co. Supply Crate Key", "price": 170.0 + i, "seller": f"S_k_{i}"}
            for i in range(5)
        ] + [
            # 2. Discovery A: Schadenfreude
            {"lot_id": f"s_{i}", "node_id": 1808, "title": "Насмешка: Злорадство", "price": 450.0 + i * 10, "seller": f"S_s_{i}"}
            for i in range(4)
        ] + [
            # 3. Discovery B: Conga
            {"lot_id": f"c_{i}", "node_id": 1808, "title": "Насмешка: Конга", "price": 580.0 + i * 5, "seller": f"S_c_{i}"}
            for i in range(3)
        ]

        t0 = 1700000000.0
        self.db.record_market_observation(lots, now=t0, force_sample=True)
        self.db.record_market_observation(lots, now=t0 + 300, force_sample=True)

        rank1 = rank_market_liquidity(self.db, now=t0 + 300)
        rank2 = rank_market_liquidity(self.db, now=t0 + 300)

        skus1 = [c.canonical_sku for c in rank1["all_ranked"]]
        skus2 = [c.canonical_sku for c in rank2["all_ranked"]]
        self.assertEqual(skus1, skus2)
        self.assertEqual(len(skus1), 3)

        scores1 = [c.liquidity_score for c in rank1["all_ranked"]]
        scores2 = [c.liquidity_score for c in rank2["all_ranked"]]
        self.assertEqual(scores1, scores2)

    def test_low_confidence_on_insufficient_history(self):
        """Candidate observed for only 10 minutes and 2 samples must receive LOW confidence."""
        lots = [
            {"lot_id": f"s_{i}", "node_id": 1808, "title": "Насмешка: Злорадство", "price": 450.0, "seller": f"S_{i}"}
            for i in range(4)
        ]
        t0 = 1700000000.0
        self.db.record_market_observation(lots, now=t0, force_sample=True)
        self.db.record_market_observation(lots, now=t0 + 600, force_sample=True)

        ranking = rank_market_liquidity(self.db, now=t0 + 600)
        self.assertGreater(len(ranking["all_ranked"]), 0)
        cand = ranking["all_ranked"][0]
        self.assertEqual(cand.confidence, "LOW")
        self.assertTrue(cand.observation_duration_hours < 1.0)


class TestTelegramLiquiditySubcommands(unittest.IsolatedAsyncioTestCase):
    """Verifies /liquidity top and /liquidity bottom handler responses."""

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test-tg-subcmd-")
        self.db_path = os.path.join(self.test_dir, "test_tg_sub.db")
        self.db = Database(self.db_path)

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_cmd_liquidity_top_and_bottom(self):
        from auto_flipper.assistant_handlers import cmd_liquidity

        # 1. When empty
        msg = AsyncMock()
        msg.text = "/liquidity top"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            self.assertTrue(msg.answer.called)
            reply = msg.answer.call_args[0][0]
            self.assertIn("Кандидаты для рейтинга пока не найдены", reply)

        # 2. Populate market
        lots = [
            # Benchmark Key
            {"lot_id": f"k_{i}", "node_id": 1808, "title": "Mann Co. Supply Crate Key", "price": 170.0 + i, "seller": f"S_k_{i}"}
            for i in range(5)
        ] + [
            # Qualified Discovery: Schadenfreude
            {"lot_id": f"s_{i}", "node_id": 1808, "title": "Насмешка: Злорадство", "price": 450.0 + i * 5, "seller": f"S_s_{i}"}
            for i in range(4)
        ]
        t0 = 1700000000.0
        self.db.record_market_observation(lots, now=t0, force_sample=True)
        self.db.record_market_observation(lots, now=t0 + 300, force_sample=True)

        # Test /liquidity top
        msg.reset_mock()
        msg.text = "/liquidity top"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            reply = msg.answer.call_args[0][0]
            self.assertIn("TOP-", reply)
            self.assertIn("Score:", reply)
            self.assertIn("Conf:", reply)
            self.assertIn("Лотов:", reply)
            self.assertIn("Продавцов:", reply)
            self.assertIn("P25/P50:", reply)
            self.assertIn("Turnover:", reply)
            self.assertIn("Reappear:", reply)
            self.assertIn("Vol:", reply)
            self.assertIn("Depth:", reply)
            self.assertIn("Набл:", reply)

        # Test /liquidity bottom
        msg.reset_mock()
        msg.text = "/liquidity bottom 5"
        with patch("auto_flipper.assistant_handlers.db", self.db):
            await cmd_liquidity(msg)
            reply = msg.answer.call_args[0][0]
            self.assertIn("BOTTOM-", reply)
            self.assertIn("Score:", reply)


if __name__ == "__main__":
    unittest.main()
