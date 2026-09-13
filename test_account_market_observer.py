"""Tests for deterministic read-only account market observation (v0.5)."""
import os
import shutil
import tempfile
import types
import unittest
import asyncio
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
    evaluate_risk_evidence_coverage,
    evaluate_market_observation_priority,
    format_account_market_detail,
    rank_account_markets,
)
from auto_flipper.account_snapshot import (
    AccountMarketSnapshot,
    SnapshotQuality,
    classify_snapshot_quality,
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

    def _record_partial_history(self, count, hours):
        rows = [lot(f"stable-{i}", 436,
                    "25000 trophies 65 brawlers full access email included recovery transfer warranty linked",
                    price=100 + i, seller=f"s{i}") for i in range(4)]
        step = hours * 3600 / max(1, count - 1)
        for index in range(count):
            self.db.record_account_market_observation(
                "funpay:account:436", rows, now=100 + index * step, force_sample=True,
                snapshot_quality="PARTIAL",
                snapshot_diagnostics={"node_id": 436, "advertised_market_count": 10,
                                      "parsed_count": 4, "coverage_ratio": .4,
                                      "fetch_success": True, "parse_success": True})
        return 100 + hours * 3600

    def test_market_isolation_and_event_semantics(self):
        brawl = [lot("same", 436, "25000 trophies 65 brawlers", seller="b1")]
        coc = [lot("same", 147, "TH12 heroes 30/30/20", seller="c1")]
        self.db.record_account_market_observation("funpay:account:436", brawl, now=100, force_sample=True)
        self.db.record_account_market_observation("funpay:account:147", coc, now=100, force_sample=True)
        self.db.record_account_market_observation("funpay:account:436", [], now=400, force_sample=True)
        self.assertEqual(len(self.db.get_account_market_lots("funpay:account:147")), 1)
        self.assertEqual(self.db.get_account_market_events("funpay:account:436")[-1]["event_type"], "DISAPPEARED")
        self.assertNotIn("SOLD", {e["event_type"] for e in self.db.get_account_market_events("funpay:account:436")})

    def test_truncated_snapshot_does_not_generate_disappearance(self):
        first = [lot("a", 436, "25000 trophies 65 brawlers", seller="s1"),
                 lot("b", 436, "25000 trophies 65 brawlers", seller="s2")]
        second = [lot("b", 436, "25000 trophies 65 brawlers", seller="s2"),
                  lot("c", 436, "25000 trophies 65 brawlers", seller="s3")]
        self.db.record_account_market_observation("funpay:account:436", first, now=100,
                                                  force_sample=True, snapshot_quality="PARTIAL")
        result = self.db.record_account_market_observation("funpay:account:436", second, now=400,
                                                           force_sample=True, snapshot_quality="PARTIAL")
        self.assertEqual(result["disappearance_count"], 0)
        self.assertEqual(self.db.get_account_market_events("funpay:account:436"), [])
        all_rows = self.db.get_account_market_lots("funpay:account:436", active_only=False)
        self.assertTrue(next(row for row in all_rows if row["lot_id"] == "a")["is_active"])

    def test_cap_boundary_rotation_does_not_mark_existing_lot_disappeared(self):
        first = [lot("edge-a", 436, "25000 trophies 65 brawlers", seller="s1"),
                 lot("edge-b", 436, "25000 trophies 65 brawlers", seller="s2")]
        shifted = [lot("edge-b", 436, "25000 trophies 65 brawlers", seller="s2"),
                   lot("edge-c", 436, "25000 trophies 65 brawlers", seller="s3")]
        self.db.record_account_market_observation("funpay:account:436", first, now=100,
                                                  force_sample=True, snapshot_quality="PARTIAL")
        self.db.record_account_market_observation("funpay:account:436", shifted, now=400,
                                                  force_sample=True, snapshot_quality="PARTIAL")
        edge = next(row for row in self.db.get_account_market_lots(
            "funpay:account:436", active_only=False) if row["lot_id"] == "edge-a")
        self.assertTrue(edge["is_active"])
        self.assertIsNone(edge["disappeared_at"])

    def test_failed_snapshot_preserves_state_and_turnover(self):
        rows = [lot("a", 436, "25000 trophies 65 brawlers", seller="s1")]
        self.db.record_account_market_observation("funpay:account:436", rows, now=100, force_sample=True)
        samples_before = len(self.db.get_account_market_samples("funpay:account:436"))
        result = self.db.record_account_market_observation(
            "funpay:account:436", [], now=400, force_sample=True, snapshot_quality="FAILED",
            snapshot_diagnostics={"fetch_success": False, "parse_success": False,
                                  "parsed_count": 0, "failure_reason": "HTTP_429_RATE_LIMITED"})
        self.assertEqual(result["disappearance_count"], 0)
        self.assertEqual(len(self.db.get_account_market_lots("funpay:account:436")), 1)
        self.assertEqual(self.db.get_latest_account_market_scan("funpay:account:436")["snapshot_quality"], "FAILED")
        self.assertEqual(len(self.db.get_account_market_samples("funpay:account:436")), samples_before)

    def test_sudden_parse_count_collapse_is_partial(self):
        quality, coverage, reason = classify_snapshot_quality(
            advertised_market_count=None, parsed_count=150, fetch_success=True, parse_success=True,
            previous_complete_count=2000, minimum_previous_ratio=.60)
        self.assertEqual(quality, SnapshotQuality.PARTIAL)
        self.assertIsNone(coverage)
        self.assertEqual(reason, "sudden_parsed_count_collapse")

    def test_partial_snapshot_contributes_zero_turnover(self):
        rows = [lot(f"a{i}", 436, "25000 trophies 65 brawlers", seller=f"s{i}") for i in range(3)]
        self.db.record_account_market_observation("funpay:account:436", rows, now=100,
                                                  force_sample=True, snapshot_quality="PARTIAL")
        self.db.record_account_market_observation("funpay:account:436", rows[1:], now=400,
                                                  force_sample=True, snapshot_quality="PARTIAL")
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, now=400)
        self.assertEqual(assessment.metrics.disappearance_count, 0)
        self.assertEqual(assessment.priority.components["turnover_proxy_score"], 0.0)
        self.assertEqual(assessment.priority.availability["turnover_proxy_score"], "unavailable")

    def test_stable_partial_history_reaches_medium_observation_confidence(self):
        now = self._record_partial_history(12, 1)
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, now=now)
        self.assertEqual(assessment.confidence, "MEDIUM")
        self.assertEqual(assessment.metrics.successful_sample_count, 12)
        self.assertEqual(assessment.turnover_confidence, "NONE")

    def test_six_hour_stable_partial_history_reaches_high_observation_confidence(self):
        now = self._record_partial_history(60, 6)
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, now=now)
        self.assertEqual(assessment.confidence, "HIGH")
        self.assertEqual(assessment.metrics.successful_sample_count, 60)
        self.assertEqual(assessment.turnover_confidence, "NONE")

    def test_partial_same_lot_price_change_is_retained_without_turnover(self):
        self.db.record_account_market_observation(
            "funpay:account:436", [lot("price", 436, "25000 trophies 65 brawlers", price=100)],
            now=100, force_sample=True, snapshot_quality="PARTIAL")
        result = self.db.record_account_market_observation(
            "funpay:account:436", [lot("price", 436, "25000 trophies 65 brawlers", price=125)],
            now=400, force_sample=True, snapshot_quality="PARTIAL")
        assessment = assess_account_market(self.db, "funpay:account:436", self.cfg, now=400)
        self.assertEqual(result["price_change_count"], 1)
        self.assertEqual(self.db.get_account_market_events("funpay:account:436")[-1]["event_type"], "PRICE_CHANGE")
        self.assertEqual(assessment.priority.components["turnover_proxy_score"], 0.0)
        self.assertEqual(assessment.turnover_confidence, "NONE")

    def test_promising_requires_turnover_evidence_even_with_stable_partial_history(self):
        now = self._record_partial_history(12, 1)
        cfg = AccountMarketConfig(
            min_active_lots=1, min_independent_sellers=1, min_cheap_lots=1,
            min_cheap_sellers=1, min_parseable_ratio=0, max_largest_seller_share=1,
            medium_min_hours=1, medium_min_samples=12, promising_min_mops=0,
            promising_max_risk=100, promising_min_cohort_size=1,
            promising_max_dispersion=999, promising_max_reappearance_ratio=1,
            risk_evidence_medium_ratio=0, risk_evidence_high_ratio=0,
        )
        assessment = assess_account_market(self.db, "funpay:account:436", cfg, now=now)
        self.assertEqual(assessment.confidence, "MEDIUM")
        self.assertEqual(assessment.turnover_confidence, "NONE")
        self.assertEqual(assessment.future_flip_eligibility, "WATCH")

    def test_complete_snapshot_can_generate_disappearance(self):
        rows = [lot("a", 436, "25000 trophies 65 brawlers", seller="s1"),
                lot("b", 436, "25000 trophies 65 brawlers", seller="s2")]
        self.db.record_account_market_observation("funpay:account:436", rows, now=100,
                                                  force_sample=True, snapshot_quality="COMPLETE")
        result = self.db.record_account_market_observation("funpay:account:436", rows[1:], now=400,
                                                           force_sample=True, snapshot_quality="COMPLETE")
        self.assertEqual(result["disappearance_count"], 1)
        self.assertEqual(self.db.get_account_market_events("funpay:account:436")[-1]["event_type"], "DISAPPEARED")

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

    def test_missing_risk_evidence_does_not_imply_high_confidence(self):
        evidence = evaluate_risk_evidence_coverage([
            {"risk_flags": [], "title": "25000 trophies 65 brawlers"},
            {"risk_flags": [], "title": "TH12 heroes 30/30"},
        ], self.cfg)
        self.assertEqual(evidence.confidence, "LOW")
        self.assertEqual(evidence.coverage_ratio, 0.0)

    def test_promising_is_blocked_by_insufficient_risk_evidence(self):
        rows = [lot(f"r{i}", 436, "25000 trophies 65 brawlers", seller=f"s{i}") for i in range(6)]
        self.db.record_account_market_observation("funpay:account:436", rows, now=100, force_sample=True)
        self.db.record_account_market_observation("funpay:account:436", rows, now=400, force_sample=True)
        cfg = AccountMarketConfig(
            min_active_lots=1, min_independent_sellers=1, min_cheap_lots=1,
            min_cheap_sellers=1, min_parseable_ratio=.1, max_largest_seller_share=1,
            medium_min_hours=0, medium_min_samples=2, high_min_hours=100,
            promising_min_mops=0, promising_max_risk=100, promising_min_cohort_size=1,
            promising_max_dispersion=999, promising_max_reappearance_ratio=1,
        )
        assessment = assess_account_market(self.db, "funpay:account:436", cfg, now=400)
        self.assertEqual(assessment.confidence, "MEDIUM")
        self.assertEqual(assessment.risk_evidence.confidence, "LOW")
        self.assertNotEqual(assessment.future_flip_eligibility, "PROMISING")

    def test_telegram_detail_includes_integrity_and_risk_diagnostics(self):
        rows = [lot("diag", 436, "25000 trophies 65 brawlers", seller="s1")]
        self.db.record_account_market_observation(
            "funpay:account:436", rows, now=100, force_sample=True,
            snapshot_quality="PARTIAL",
            snapshot_diagnostics={"advertised_market_count": 10, "parsed_count": 1,
                                  "coverage_ratio": .1, "scan_duration": 2.5})
        message = format_account_market_detail(self.db, "funpay:account:436", now=105)
        self.assertIn("Parsed: 1 | Advertised: 10 | Coverage: 10.0%", message)
        self.assertIn("Snapshot: PARTIAL | Scan age: 5s | Duration:", message)
        self.assertIn("Risk evidence: LOW", message)

    def test_advertised_count_parser_accepts_grouped_digits(self):
        from auto_flipper.funpay_client import FunPayClient
        html = ('<a href="https://funpay.com/lots/436/" class="counter-item active">'
                '<div class="counter-value">14 416</div></a>')
        self.assertEqual(FunPayClient._parse_advertised_market_count(html, 436), 14416)

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

    async def test_concurrent_same_market_scan_is_skipped(self):
        root = tempfile.mkdtemp(prefix="account-lock-")
        started, release = asyncio.Event(), asyncio.Event()
        class Client:
            calls = 0
            async def fetch_account_market_snapshot(self, node_id, **kwargs):
                self.calls += 1
                started.set()
                await release.wait()
                return AccountMarketSnapshot("funpay:account:436", 436, [], 0, 0, 1.0,
                    True, True, SnapshotQuality.COMPLETE, .1, 200)
        try:
            db = Database(os.path.join(root, "observer.db"))
            client = Client()
            observer = AccountMarketObserver(client, db)
            first = asyncio.create_task(observer.poll_market("funpay:account:436", now=100))
            await started.wait()
            second = await observer.poll_market("funpay:account:436", now=101)
            release.set()
            await first
            self.assertTrue(second["skipped"])
            self.assertEqual(client.calls, 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_429_preserves_history_and_schedules_backoff(self):
        root = tempfile.mkdtemp(prefix="account-429-")
        class Client:
            async def fetch_account_market_snapshot(self, node_id, **kwargs):
                return AccountMarketSnapshot(
                    "funpay:account:436", node_id, [], None, 0, None,
                    False, False, SnapshotQuality.FAILED, .1, 429,
                    "HTTP_429_RATE_LIMITED")
        try:
            db = Database(os.path.join(root, "observer.db"))
            rows = [lot("kept", 436, "25000 trophies 65 brawlers")]
            db.record_account_market_observation(
                "funpay:account:436", rows, now=100, force_sample=True)
            observer = AccountMarketObserver(Client(), db)
            observer._next_poll["funpay:account:147"] = float("inf")
            observer._next_poll["funpay:account:248"] = float("inf")
            result = await observer.poll_due_once(now=400)
            self.assertEqual(result[0]["snapshot_quality"], "FAILED")
            self.assertEqual(observer._next_poll["funpay:account:436"], 700)
            self.assertEqual(len(db.get_account_market_lots("funpay:account:436")), 1)
            self.assertEqual(len(db.get_account_market_samples("funpay:account:436")), 1)
            self.assertEqual(db.get_account_market_events("funpay:account:436")[-1]["event_type"], "NEW")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_one_broken_market_does_not_stop_others(self):
        root = tempfile.mkdtemp(prefix="account-isolation-")
        class Client:
            async def fetch_account_market_snapshot(self, node_id, **kwargs):
                market = {436: "funpay:account:436", 147: "funpay:account:147", 248: "funpay:account:248"}[node_id]
                if node_id == 436:
                    return AccountMarketSnapshot(market, node_id, [], None, 0, None,
                        False, False, SnapshotQuality.FAILED, .1, 429, "HTTP_429_RATE_LIMITED")
                title = "TH12 heroes 30/30" if node_id == 147 else "PC 20 skins full access"
                rows = [lot(str(node_id), node_id, title)]
                return AccountMarketSnapshot(market, node_id, rows, 1, 1, 1.0,
                    True, True, SnapshotQuality.COMPLETE, .1, 200)
        try:
            db = Database(os.path.join(root, "observer.db"))
            observer = AccountMarketObserver(Client(), db)
            results = await observer.poll_due_once(now=100)
            self.assertEqual(len(results), 3)
            self.assertEqual(db.get_latest_account_market_scan("funpay:account:436")["snapshot_quality"], "FAILED")
            self.assertEqual(len(db.get_account_market_lots("funpay:account:147")), 1)
            self.assertEqual(len(db.get_account_market_lots("funpay:account:248")), 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
