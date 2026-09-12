"""Tests for deterministic read-only account market observation (v0.5)."""
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

from auto_flipper.account_cohort_normalizer import (
    ACCOUNT_OBSERVATION_SOURCE,
    UNCLASSIFIED,
    UNCLASSIFIED_SPECIALTY,
    normalize_account_lot,
)
from auto_flipper.account_market_observer import (
    AccountMarketConfig,
    AccountMarketObserver,
    ActiveMarketSelector,
    assess_account_market,
    evaluate_account_market_risk,
    evaluate_market_observation_priority,
    rank_account_markets,
)
from auto_flipper.database import Database


def lot(lot_id, node, title, price=300, seller="seller", **extra):
    value = {"lot_id": lot_id, "node_id": node, "title": title, "price": price,
             "currency": "RUB", "seller": seller, "seller_rating": 5.0,
             "seller_reviews": 50, "stock": 1, "url": "https://example.invalid"}
    value.update(extra)
    return value


class TestAccountCohorts(unittest.TestCase):
    def test_stable_brawl_and_separation(self):
        a = normalize_account_lot("funpay:account:436", lot("1", 436, "25000 кубков 65 бойцов полный доступ"))
        b = normalize_account_lot("funpay:account:436", lot("1", 436, "25000 кубков 65 бойцов полный доступ"))
        c = normalize_account_lot("funpay:account:436", lot("2", 436, "55000 кубков 105 бойцов"))
        self.assertTrue(a.classified)
        self.assertEqual(a.cohort_id, b.cohort_id)
        self.assertNotEqual(a.cohort_id, c.cohort_id)
        self.assertEqual(a.source_type, ACCOUNT_OBSERVATION_SOURCE)
        self.assertFalse(a.purchase_eligible)

    def test_coc_town_hall_isolation_and_unknown_heroes(self):
        th10 = normalize_account_lot("funpay:account:147", lot("1", 147, "TH10 герои 30/30/20"))
        th12 = normalize_account_lot("funpay:account:147", lot("2", 147, "TH12 герои 30/30/20"))
        unknown = normalize_account_lot("funpay:account:147", lot("3", 147, "TH12 хороший аккаунт"))
        self.assertNotEqual(th10.cohort_id, th12.cohort_id)
        self.assertIn("heroes_unknown", unknown.cohort_id)
        self.assertLess(unknown.confidence, th12.confidence)

    def test_fortnite_sale_rent_specialty_and_ambiguous(self):
        sale = normalize_account_lot("funpay:account:248", lot("1", 248, "PC 45 skins full access native email"))
        rent = normalize_account_lot("funpay:account:248", lot("2", 248, "PC rent 45 skins"))
        rare = normalize_account_lot("funpay:account:248", lot("3", 248, "PC 45 skins Renegade Raider"))
        ambiguous = normalize_account_lot("funpay:account:248", lot("4", 248, "Отличный аккаунт Fortnite"))
        self.assertTrue(sale.classified and rent.classified)
        self.assertNotEqual(sale.cohort_id, rent.cohort_id)
        self.assertEqual(rare.cohort_id, UNCLASSIFIED_SPECIALTY)
        self.assertFalse(rare.classified)
        self.assertEqual(ambiguous.cohort_id, UNCLASSIFIED)

    def test_conflicting_structured_text_fails_closed(self):
        result = normalize_account_lot("funpay:account:147", lot(
            "1", 147, "TH12 heroes 30/30", structured_fields={"town_hall": "10"}))
        self.assertFalse(result.classified)
        self.assertIn("conflict", result.reason)


class TestAccountStoreAndScoring(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="account-observer-")
        self.db = Database(os.path.join(self.root, "observer.db"))
        self.cfg = AccountMarketConfig(min_active_lots=1, min_independent_sellers=1,
            min_cheap_lots=1, min_cheap_sellers=1, min_parseable_ratio=.1,
            max_largest_seller_share=1.0, hysteresis_samples=2)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_market_isolation_and_event_semantics(self):
        brawl = [lot("same", 436, "25000 trophies 65 brawlers", seller="b1")]
        coc = [lot("same", 147, "TH12 heroes 30/30/20", seller="c1")]
        self.db.record_account_market_observation("funpay:account:436", brawl, now=100, force_sample=True)
        self.db.record_account_market_observation("funpay:account:147", coc, now=100, force_sample=True)
        self.db.record_account_market_observation("funpay:account:436", [], now=400, force_sample=True)
        self.assertEqual(len(self.db.get_account_market_lots("funpay:account:147")), 1)
        self.assertEqual(self.db.get_account_market_events("funpay:account:436")[-1]["event_type"], "DISAPPEARED")
        self.assertNotIn("SOLD", {e["event_type"] for e in self.db.get_account_market_events("funpay:account:436")})

    def _populate(self, market_id, node, title_template, count=4, t0=1_700_000_000):
        rows = [lot(f"{node}-{i}", node, title_template.format(i=i), 100 + i * 10, f"s{i}") for i in range(count)]
        self.db.record_account_market_observation(market_id, rows, now=t0, force_sample=True)
        self.db.record_account_market_observation(market_id, rows, now=t0 + 3600, force_sample=True)

    def test_deterministic_ranking_and_single_scan_not_high(self):
        self._populate("funpay:account:436", 436, "25000 trophies 65 brawlers")
        one = rank_account_markets(self.db, self.cfg, now=1_700_003_600)
        two = rank_account_markets(self.db, self.cfg, now=1_700_003_600)
        self.assertEqual([(a.market_id, a.mops) for a in one], [(a.market_id, a.mops) for a in two])
        self.assertNotEqual(one[0].confidence, "HIGH")

    def test_first_snapshot_is_not_turnover(self):
        rows = [lot(f"b-{i}", 436, "25000 trophies 65 brawlers", 100 + i, f"s{i}") for i in range(4)]
        self.db.record_account_market_observation("funpay:account:436", rows, now=1_700_000_000, force_sample=True)
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, 1_700_000_000)
        self.assertEqual(assessment.priority.components["turnover_proxy_score"], 0.0)

    def test_risk_separate_from_mops(self):
        self._populate("funpay:account:436", 436, "25000 trophies 65 brawlers")
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, 1_700_003_600)
        original = evaluate_market_observation_priority(assessment.metrics)
        altered_risk = evaluate_account_market_risk([
            {"seller": "x", "title": "x", "seller_reviews": 0,
             "seller_rating": 0, "risk_flags": ["no_email_access", "rental"]}
        ], assessment.metrics)
        self.assertEqual(original.score, assessment.mops)
        self.assertNotEqual(altered_risk.score, assessment.risk_score)

    def test_source_barrier_blocks_claim(self):
        candidate = {"payload": {"source_type": ACCOUNT_OBSERVATION_SOURCE,
                                  "purchase_eligible": False, "market_id": "funpay:account:436"}}
        with self.assertRaises(PermissionError):
            self.db.claim_purchase("x", "funpay:account:436", 100, True, candidate=candidate)


class TestSelectionAndReadOnly(unittest.IsolatedAsyncioTestCase):
    def test_top_two_seed_neutral_and_hysteresis(self):
        cfg = AccountMarketConfig(hysteresis_points=5, hysteresis_samples=2)
        selector = ActiveMarketSelector(cfg)
        def item(market, score):
            return types.SimpleNamespace(market_id=market, mops=score, eligible=True)
        initial = [item("funpay:account:436", 80), item("funpay:account:147", 70), item("funpay:account:248", 60)]
        self.assertEqual(selector.select(initial), ("funpay:account:436", "funpay:account:147"))
        small = [item("funpay:account:436", 80), item("funpay:account:147", 70), item("funpay:account:248", 73)]
        self.assertEqual(selector.select(small), ("funpay:account:436", "funpay:account:147"))
        large = [item("funpay:account:436", 80), item("funpay:account:147", 70), item("funpay:account:248", 76)]
        self.assertEqual(selector.select(large), ("funpay:account:436", "funpay:account:147"))
        self.assertEqual(selector.select(large), ("funpay:account:436", "funpay:account:248"))

    async def test_observer_only_calls_get_facade(self):
        root = tempfile.mkdtemp(prefix="account-readonly-")
        try:
            db = Database(os.path.join(root, "observer.db"))
            client = types.SimpleNamespace(fetch_account_market_lots=AsyncMock(return_value=[]),
                                           checkout_lot=AsyncMock(), send_chat_message=AsyncMock(),
                                           save_offer=AsyncMock())
            observer = AccountMarketObserver(client, db)
            await observer.poll_market("funpay:account:436", now=100)
            client.fetch_account_market_lots.assert_awaited_once_with(436)
            client.checkout_lot.assert_not_called()
            client.send_chat_message.assert_not_called()
            client.save_offer.assert_not_called()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_client_checkout_rejects_account_source_even_in_dry_run(self):
        from auto_flipper.funpay_client import FunPayClient
        with self.assertRaises(PermissionError):
            await FunPayClient().checkout_lot("funpay_123", 100, dry_run=True,
                                             source_type=ACCOUNT_OBSERVATION_SOURCE,
                                             purchase_eligible=False)


if __name__ == "__main__":
    unittest.main()
