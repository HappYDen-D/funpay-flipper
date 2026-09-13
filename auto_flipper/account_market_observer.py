"""Read-only account market assessment, selection, and polling.

MOPS measures observation usefulness. AccountMarketRiskScore is deliberately
computed and returned separately and never feeds the MOPS formula.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from auto_flipper.account_cohort_normalizer import (
    ACCOUNT_OBSERVATION_SOURCE,
    UNCLASSIFIED,
    UNCLASSIFIED_SPECIALTY,
)
from auto_flipper.config import (
    ACCOUNT_ACTIVE_POLL_MIN_SECONDS,
    ACCOUNT_ACTIVE_POLL_MAX_SECONDS,
    ACCOUNT_BACKGROUND_POLL_MIN_SECONDS,
    ACCOUNT_BACKGROUND_POLL_MAX_SECONDS,
    ACCOUNT_FAILURE_BACKOFF_INITIAL_SECONDS,
    ACCOUNT_FAILURE_BACKOFF_MAX_SECONDS,
    ACCOUNT_CHEAP_RUB_THRESHOLD,
    ACCOUNT_MARKET_SEEDS,
    ACCOUNT_MAX_LARGEST_SELLER_SHARE,
    ACCOUNT_MIN_ACTIVE_LOTS,
    ACCOUNT_MIN_CHEAP_LOTS,
    ACCOUNT_MIN_CHEAP_SELLERS,
    ACCOUNT_MIN_INDEPENDENT_SELLERS,
    ACCOUNT_MIN_PARSEABLE_RATIO,
    ACCOUNT_RISK_EVIDENCE_HIGH_RATIO,
    ACCOUNT_RISK_EVIDENCE_MEDIUM_RATIO,
    ACCOUNT_SAMPLE_INTERVAL_SECONDS,
    ACCOUNT_SELECTION_CONFIRM_SAMPLES,
    ACCOUNT_SELECTION_HYSTERESIS_POINTS,
    ACCOUNT_SNAPSHOT_MIN_PREVIOUS_RATIO,
    MAX_ACTIVE_ACCOUNT_MARKETS,
)
from auto_flipper.account_snapshot import AccountMarketSnapshot, SnapshotQuality
from auto_flipper.market_store import compute_mad, compute_quantile

logger = logging.getLogger("AccountMarketObserver")


@dataclass(frozen=True)
class AccountMarketConfig:
    min_active_lots: int = ACCOUNT_MIN_ACTIVE_LOTS
    min_independent_sellers: int = ACCOUNT_MIN_INDEPENDENT_SELLERS
    min_cheap_lots: int = ACCOUNT_MIN_CHEAP_LOTS
    min_cheap_sellers: int = ACCOUNT_MIN_CHEAP_SELLERS
    min_parseable_ratio: float = ACCOUNT_MIN_PARSEABLE_RATIO
    max_largest_seller_share: float = ACCOUNT_MAX_LARGEST_SELLER_SHARE
    cheap_rub_threshold: float = ACCOUNT_CHEAP_RUB_THRESHOLD
    max_active_markets: int = MAX_ACTIVE_ACCOUNT_MARKETS
    hysteresis_points: float = ACCOUNT_SELECTION_HYSTERESIS_POINTS
    hysteresis_samples: int = ACCOUNT_SELECTION_CONFIRM_SAMPLES
    active_poll_seconds: float = ACCOUNT_ACTIVE_POLL_MIN_SECONDS
    background_poll_seconds: float = ACCOUNT_BACKGROUND_POLL_MIN_SECONDS
    active_poll_max_seconds: float = ACCOUNT_ACTIVE_POLL_MAX_SECONDS
    background_poll_max_seconds: float = ACCOUNT_BACKGROUND_POLL_MAX_SECONDS
    failure_backoff_initial_seconds: float = ACCOUNT_FAILURE_BACKOFF_INITIAL_SECONDS
    failure_backoff_max_seconds: float = ACCOUNT_FAILURE_BACKOFF_MAX_SECONDS
    risk_evidence_medium_ratio: float = ACCOUNT_RISK_EVIDENCE_MEDIUM_RATIO
    risk_evidence_high_ratio: float = ACCOUNT_RISK_EVIDENCE_HIGH_RATIO
    sample_interval_seconds: float = ACCOUNT_SAMPLE_INTERVAL_SECONDS
    stable_min_previous_ratio: float = ACCOUNT_SNAPSHOT_MIN_PREVIOUS_RATIO
    medium_min_hours: float = 1.0
    medium_min_samples: int = 12
    high_min_hours: float = 6.0
    high_min_samples: int = 60
    promising_min_mops: float = 60.0
    promising_max_risk: float = 70.0
    promising_min_cohort_size: int = 5
    promising_max_dispersion: float = 1.5
    promising_max_reappearance_ratio: float = 0.35


@dataclass(frozen=True)
class AccountMarketMetrics:
    market_id: str
    active_lots: int
    independent_sellers: int
    cheap_lots: int
    cheap_independent_sellers: int
    parseable_ratio: float
    largest_seller_share: float
    top3_seller_share: float
    hhi: float
    cohort_count: int
    median_cohort_size: float
    priced_classified_lots: int
    observation_duration_hours: float
    sample_count: int
    new_count: int
    disappearance_count: int
    reappearance_count: int
    price_change_count: int
    disappearance_rate: float
    reappearance_rate: float
    reappearance_ratio: float
    listing_churn_rate: float
    average_lifetime_hours: float
    median_cohort_dispersion: float
    well_priced_cohort_coverage: float
    advertised_market_count: Optional[int] = None
    parsed_count: int = 0
    coverage_ratio: Optional[float] = None
    snapshot_quality: str = "FAILED"
    scan_age_seconds: Optional[float] = None
    scan_duration: float = 0.0
    complete_sample_count: int = 0
    successful_sample_count: int = 0
    complete_observation_duration_hours: float = 0.0
    turnover_confidence: str = "NONE"


@dataclass(frozen=True)
class MarketObservationPriorityScore:
    score: float
    components: Dict[str, float]
    availability: Dict[str, str]


@dataclass(frozen=True)
class AccountMarketRiskScore:
    score: float
    components: Dict[str, float]
    signals: Tuple[str, ...]


@dataclass(frozen=True)
class RiskEvidenceCoverage:
    coverage_ratio: float
    confidence: str
    observed_dimensions: Tuple[str, ...]
    missing_dimensions: Tuple[str, ...]


@dataclass(frozen=True)
class AccountMarketAssessment:
    market_id: str
    name: str
    node_id: int
    metrics: AccountMarketMetrics
    priority: MarketObservationPriorityScore
    risk: AccountMarketRiskScore
    confidence: str
    turnover_confidence: str
    eligible: bool
    eligibility_failures: Tuple[str, ...]
    future_flip_eligibility: str
    explanations: Tuple[str, ...]
    risk_evidence: RiskEvidenceCoverage

    @property
    def mops(self) -> float:
        return self.priority.score

    @property
    def risk_score(self) -> float:
        return self.risk.score


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, float(value))), 2)


def _sat_log(value: float, target: float) -> float:
    if value <= 0:
        return 0.0
    return _clamp(100.0 * math.log1p(value) / math.log1p(target))


def _confidence(metrics: AccountMarketMetrics, cfg: AccountMarketConfig) -> str:
    if metrics.observation_duration_hours >= cfg.high_min_hours and metrics.successful_sample_count >= cfg.high_min_samples:
        return "HIGH"
    if metrics.observation_duration_hours >= cfg.medium_min_hours and metrics.successful_sample_count >= cfg.medium_min_samples:
        return "MEDIUM"
    return "LOW"


def _turnover_confidence(metrics: AccountMarketMetrics, cfg: AccountMarketConfig) -> str:
    """Only complete snapshots can establish absence-based turnover evidence."""
    if metrics.complete_sample_count == 0:
        return "NONE"
    if (metrics.complete_observation_duration_hours >= cfg.high_min_hours
            and metrics.complete_sample_count >= cfg.high_min_samples):
        return "HIGH"
    if (metrics.complete_observation_duration_hours >= cfg.medium_min_hours
            and metrics.complete_sample_count >= cfg.medium_min_samples):
        return "MEDIUM"
    return "LOW"


def _stable_successful_samples(samples, market_id: str, node_id: int,
                               cfg: AccountMarketConfig):
    """Return successful samples whose visible window is stable enough to compare."""
    stable = []
    parsed_baseline = []
    coverage_baseline = []
    for sample in reversed(samples):
        try:
            metadata = json.loads(sample.get("metrics_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        quality = metadata.get("snapshot_quality", SnapshotQuality.COMPLETE.value)
        if quality == SnapshotQuality.FAILED.value:
            continue
        if not metadata.get("fetch_success", True) or not metadata.get("parse_success", True):
            continue
        if str(sample.get("market_id")) != market_id or int(metadata.get("node_id", node_id)) != node_id:
            continue
        parsed_count = int(metadata.get("parsed_count", sample.get("active_lots") or 0))
        coverage = metadata.get("coverage_ratio")
        if parsed_count < cfg.min_active_lots:
            continue
        if parsed_baseline:
            usual_parsed = statistics.median(parsed_baseline[-20:])
            if parsed_count < usual_parsed * cfg.stable_min_previous_ratio:
                continue
        if coverage is not None and coverage_baseline:
            usual_coverage = statistics.median(coverage_baseline[-20:])
            if float(coverage) < usual_coverage * cfg.stable_min_previous_ratio:
                continue
        stable.append(sample)
        parsed_baseline.append(parsed_count)
        if coverage is not None:
            coverage_baseline.append(float(coverage))
    return stable


def compute_account_market_metrics(db, market_id: str, now: Optional[float] = None,
                                   cheap_rub_threshold: float = ACCOUNT_CHEAP_RUB_THRESHOLD,
                                   config: Optional[AccountMarketConfig] = None) -> AccountMarketMetrics:
    cfg = config or AccountMarketConfig(cheap_rub_threshold=cheap_rub_threshold)
    now_ts = time.time() if now is None else float(now)
    scans = db.get_account_market_scans(market_id, limit=500)
    latest_scan = scans[0] if scans else None
    latest_data_scan = next((scan for scan in scans if scan["snapshot_quality"] != "FAILED"), None)
    if latest_data_scan and latest_data_scan["snapshot_quality"] == "PARTIAL":
        lots = [lot for lot in db.get_account_market_lots(market_id, active_only=False)
                if float(lot["observed_at"]) == float(latest_data_scan["started_at"])]
    else:
        lots = db.get_account_market_lots(market_id, active_only=True)
    all_lots = db.get_account_market_lots(market_id, active_only=False)
    events = db.get_account_market_events(market_id)
    samples = db.get_account_market_samples(market_id, limit=500)
    complete_samples = []
    for sample in samples:
        try:
            sample_meta = json.loads(sample.get("metrics_json") or "{}")
        except (TypeError, ValueError):
            sample_meta = {}
        if sample_meta.get("snapshot_quality", "COMPLETE") == "COMPLETE":
            complete_samples.append(sample)
    node_id = int(ACCOUNT_MARKET_SEEDS[market_id]["node_id"])
    stable_samples = _stable_successful_samples(samples, market_id, node_id, cfg)
    seller_counts = Counter(str(lot.get("seller") or "Unknown") for lot in lots)
    active_count = len(lots)
    shares = sorted((count / active_count for count in seller_counts.values()), reverse=True) if active_count else []
    classified = [lot for lot in lots if bool(lot.get("classified")) and lot.get("cohort_id") not in (UNCLASSIFIED, UNCLASSIFIED_SPECIALTY)]
    rub = [lot for lot in lots if lot.get("currency") == "RUB" and lot.get("price") is not None]
    cheap = [lot for lot in rub if float(lot["price"]) <= cheap_rub_threshold]
    cohorts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for lot in classified:
        cohorts[lot["cohort_id"]].append(lot)
    cohort_sizes = [len(rows) for rows in cohorts.values()]
    dispersions = []
    well_priced_lots = 0
    priced_classified_lots = 0
    for rows in cohorts.values():
        prices = sorted(float(row["price"]) for row in rows if row.get("currency") == "RUB" and row.get("price") is not None)
        priced_classified_lots += len(prices)
        if len(prices) >= 3:
            p10, p50, p90 = compute_quantile(prices, .10), compute_quantile(prices, .50), compute_quantile(prices, .90)
            dispersions.append((p90 - p10) / max(.01, p50))
            well_priced_lots += len(prices)
    stable_times = [float(sample["timestamp"]) for sample in stable_samples]
    duration_hours = ((max(stable_times) - min(stable_times)) / 3600.0
                      if len(stable_times) >= 2 else 0.0)
    complete_times = [float(sample["timestamp"]) for sample in complete_samples]
    complete_duration = ((max(complete_times) - min(complete_times)) / 3600.0
                         if len(complete_times) >= 2 else 0.0)
    event_counts = Counter(event["event_type"] for event in events)
    oldest_sample_active = int(samples[-1]["active_lots"]) if samples else 0
    replacement_new_count = max(0, event_counts["NEW"] - oldest_sample_active)
    rate_denominator = max(duration_hours, 1 / 60)
    disappearance = event_counts["DISAPPEARED"]
    reappearance = event_counts["REAPPEARED"]
    completed_lifetimes = [
        max(0.0, (float(lot["disappeared_at"]) - float(lot["first_seen_at"])) / 3600.0)
        for lot in all_lots if lot.get("disappeared_at") is not None
    ]
    average_lifetime = statistics.mean(completed_lifetimes) if completed_lifetimes else duration_hours
    return AccountMarketMetrics(
        market_id=market_id, active_lots=active_count, independent_sellers=len(seller_counts),
        cheap_lots=len(cheap), cheap_independent_sellers=len({lot["seller"] for lot in cheap}),
        parseable_ratio=len(classified) / max(1, active_count),
        largest_seller_share=shares[0] if shares else 1.0,
        top3_seller_share=sum(shares[:3]) if shares else 1.0,
        hhi=sum(share * share for share in shares) if shares else 1.0,
        cohort_count=len(cohorts), median_cohort_size=float(statistics.median(cohort_sizes)) if cohort_sizes else 0.0,
        priced_classified_lots=priced_classified_lots,
        observation_duration_hours=round(duration_hours, 4), sample_count=len(samples),
        new_count=event_counts["NEW"], disappearance_count=disappearance,
        reappearance_count=reappearance, price_change_count=event_counts["PRICE_CHANGE"],
        disappearance_rate=disappearance / rate_denominator, reappearance_rate=reappearance / rate_denominator,
        reappearance_ratio=reappearance / max(1, disappearance),
        # Price/stock updates are safe positive evidence, not absence-based
        # turnover. NEW/DISAPPEARED are emitted only by COMPLETE scans.
        listing_churn_rate=(replacement_new_count + disappearance) / rate_denominator,
        average_lifetime_hours=round(average_lifetime, 4),
        median_cohort_dispersion=float(statistics.median(dispersions)) if dispersions else 0.0,
        well_priced_cohort_coverage=well_priced_lots / max(1, priced_classified_lots),
        advertised_market_count=latest_scan["advertised_market_count"] if latest_scan else None,
        parsed_count=int(latest_scan["parsed_count"]) if latest_scan else 0,
        coverage_ratio=latest_scan["coverage_ratio"] if latest_scan else None,
        snapshot_quality=latest_scan["snapshot_quality"] if latest_scan else "FAILED",
        scan_age_seconds=max(0.0, now_ts - float(latest_scan["started_at"])) if latest_scan else None,
        scan_duration=float(latest_scan["scan_duration"]) if latest_scan else 0.0,
        complete_sample_count=len(complete_samples),
        successful_sample_count=len(stable_samples),
        complete_observation_duration_hours=round(complete_duration, 4),
    )


def evaluate_market_observation_priority(metrics: AccountMarketMetrics) -> MarketObservationPriorityScore:
    depth = _sat_log(metrics.active_lots, 10_000)
    seller_count = _sat_log(metrics.independent_sellers, 500)
    concentration_quality = 100.0 * (1.0 - min(1.0, .50 * metrics.largest_seller_share +
                                                   .30 * metrics.top3_seller_share + .20 * metrics.hhi))
    seller_diversity = _clamp(.55 * seller_count + .45 * concentration_quality)
    cheap_count = _sat_log(metrics.cheap_lots, 1_000)
    cheap_sellers = _sat_log(metrics.cheap_independent_sellers, 150)
    cheap_share = _clamp(100.0 * min(1.0, (metrics.cheap_lots / max(1, metrics.active_lots)) / .25))
    budget = _clamp(.45 * cheap_count + .35 * cheap_sellers + .20 * cheap_share)
    cohort_count_quality = _clamp(100.0 * min(1.0, metrics.cohort_count / 20.0))
    cohort_size_quality = _clamp(100.0 * min(1.0, metrics.median_cohort_size / 20.0))
    cohortability = _clamp(.60 * metrics.parseable_ratio * 100 + .20 * cohort_count_quality + .20 * cohort_size_quality)
    if metrics.turnover_confidence == "NONE" or metrics.sample_count < 2:
        turnover = 0.0
    else:
        disappear_signal = _clamp(100.0 * (1.0 - math.exp(-metrics.disappearance_rate / 10.0)))
        churn_signal = _clamp(100.0 * (1.0 - math.exp(-metrics.listing_churn_rate / 20.0)))
        reappearance_quality = (_clamp(100.0 * (1.0 - min(1.0, metrics.reappearance_ratio)))
                                if metrics.disappearance_count else 0.0)
        turnover = _clamp(.45 * disappear_signal + .25 * churn_signal + .30 * reappearance_quality)
    sample_quality = _clamp(100.0 * min(1.0, metrics.priced_classified_lots / 100.0))
    dispersion_quality = _clamp(100.0 * (1.0 - min(1.0, metrics.median_cohort_dispersion / 2.0)))
    price_structure = _clamp(.45 * metrics.well_priced_cohort_coverage * 100 +
                             .30 * dispersion_quality + .25 * sample_quality)
    components = {
        "depth_score": depth, "seller_diversity_score": seller_diversity,
        "budget_accessibility_score": budget, "cohortability_score": cohortability,
        "turnover_proxy_score": turnover, "price_structure_score": price_structure,
    }
    score = _clamp(.25 * depth + .20 * seller_diversity + .20 * budget +
                   .15 * cohortability + .10 * turnover + .10 * price_structure)
    availability = {"turnover_proxy_score": ("unavailable" if metrics.turnover_confidence == "NONE"
                                               else metrics.turnover_confidence.lower())}
    return MarketObservationPriorityScore(score, components, availability)


def evaluate_account_market_risk(lots: Sequence[Mapping[str, Any]], metrics: AccountMarketMetrics) -> AccountMarketRiskScore:
    """Observed risk proxy, independent of MOPS and never a safety guarantee."""
    total = max(1, len(lots))
    flag_counts = Counter(flag for lot in lots for flag in (lot.get("risk_flags") or ()))
    signal_weights = {
        "no_email_access": 95, "ambiguous_access": 85, "rental": 70,
        "platform_linking": 55, "extremely_new_seller": 65, "warranty_claim": 20,
        "email_change_claim": 25, "native_email_claim": 30, "email_included_claim": 20,
        "full_access_claim": 15, "recovery_claim": 45, "transfer_claim": 35,
    }
    claim_risk = sum(signal_weights.get(flag, 35) * count / total for flag, count in flag_counts.items())
    claim_risk = _clamp(claim_risk)
    reputation = _clamp(100.0 * sum(1 for lot in lots if int(lot.get("seller_reviews") or 0) < 3 or
                                     float(lot.get("seller_rating") or 0) < 4.5) / total)
    reappearance = _clamp(metrics.reappearance_ratio * 100.0)
    concentration = _clamp(100.0 * (.65 * metrics.largest_seller_share + .35 * metrics.hhi))
    # Repeated normalized titles from one seller are a mass-identical-account proxy.
    duplicates = Counter((str(lot.get("seller")), str(lot.get("title", "")).lower().strip()) for lot in lots)
    mass_identical = _clamp(100.0 * sum(count for count in duplicates.values() if count >= 3) / total)
    components = {"access_recovery_proxy": claim_risk, "seller_reputation_proxy": reputation,
                  "reappearance_proxy": reappearance, "seller_concentration_proxy": concentration,
                  "mass_identical_proxy": mass_identical}
    score = _clamp(.35 * claim_risk + .20 * reputation + .20 * reappearance +
                   .15 * concentration + .10 * mass_identical)
    signals = tuple(sorted(flag for flag, count in flag_counts.items() if count))
    return AccountMarketRiskScore(score, components, signals)


def evaluate_risk_evidence_coverage(lots: Sequence[Mapping[str, Any]],
                                    config: Optional[AccountMarketConfig] = None) -> RiskEvidenceCoverage:
    """Measure how much risk-relevant evidence exists; missing means unknown."""
    cfg = config or AccountMarketConfig()
    dimensions = {
        "access": {"full_access_claim", "ambiguous_access", "no_email_access"},
        "email": {"email_included_claim", "native_email_claim", "email_change_claim", "no_email_access"},
        "recovery": {"recovery_claim", "native_email_claim", "no_email_access"},
        "transfer": {"transfer_claim", "email_change_claim", "ambiguous_access"},
        "warranty": {"warranty_claim"},
        "platform_linking": {"platform_linking"},
    }
    if not lots:
        coverage = 0.0
        per_dimension = {name: 0.0 for name in dimensions}
    else:
        per_dimension = {}
        for name, flags in dimensions.items():
            per_dimension[name] = sum(
                1 for lot in lots if flags.intersection(set(lot.get("risk_flags") or ()))
            ) / len(lots)
        coverage = sum(per_dimension.values()) / len(per_dimension)
    if coverage >= cfg.risk_evidence_high_ratio:
        confidence = "HIGH"
    elif coverage >= cfg.risk_evidence_medium_ratio:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    observed = tuple(sorted(name for name, ratio in per_dimension.items() if ratio > 0))
    missing = tuple(sorted(name for name, ratio in per_dimension.items() if ratio == 0))
    return RiskEvidenceCoverage(round(coverage, 4), confidence, observed, missing)


def _eligibility(metrics: AccountMarketMetrics, cfg: AccountMarketConfig) -> Tuple[bool, Tuple[str, ...]]:
    failures = []
    checks = (
        (metrics.active_lots >= cfg.min_active_lots, f"active_lots<{cfg.min_active_lots}"),
        (metrics.independent_sellers >= cfg.min_independent_sellers, f"independent_sellers<{cfg.min_independent_sellers}"),
        (metrics.cheap_lots >= cfg.min_cheap_lots, f"cheap_lots<{cfg.min_cheap_lots}"),
        (metrics.cheap_independent_sellers >= cfg.min_cheap_sellers, f"cheap_sellers<{cfg.min_cheap_sellers}"),
        (metrics.parseable_ratio >= cfg.min_parseable_ratio, f"parseable_ratio<{cfg.min_parseable_ratio}"),
        (metrics.largest_seller_share <= cfg.max_largest_seller_share,
         f"largest_seller_share>{cfg.max_largest_seller_share}"),
    )
    failures.extend(reason for passed, reason in checks if not passed)
    return not failures, tuple(failures)


def assess_account_market(db, market_id: str, config: Optional[AccountMarketConfig] = None,
                          now: Optional[float] = None) -> AccountMarketAssessment:
    cfg = config or AccountMarketConfig()
    if market_id not in ACCOUNT_MARKET_SEEDS:
        raise ValueError("unsupported account market")
    metrics = compute_account_market_metrics(db, market_id, now=now,
                                             cheap_rub_threshold=cfg.cheap_rub_threshold,
                                             config=cfg)
    turnover_confidence = _turnover_confidence(metrics, cfg)
    metrics = AccountMarketMetrics(**{**metrics.__dict__, "turnover_confidence": turnover_confidence})
    priority = evaluate_market_observation_priority(metrics)
    scans = db.get_account_market_scans(market_id, limit=100)
    latest_data_scan = next((scan for scan in scans if scan["snapshot_quality"] != "FAILED"), None)
    if latest_data_scan and latest_data_scan["snapshot_quality"] == "PARTIAL":
        lots = [lot for lot in db.get_account_market_lots(market_id, active_only=False)
                if float(lot["observed_at"]) == float(latest_data_scan["started_at"])]
    else:
        lots = db.get_account_market_lots(market_id, active_only=True)
    risk = evaluate_account_market_risk(lots, metrics)
    risk_evidence = evaluate_risk_evidence_coverage(lots, cfg)
    eligible, failures = _eligibility(metrics, cfg)
    confidence = _confidence(metrics, cfg)
    cohort_sample_sufficient = metrics.median_cohort_size >= cfg.promising_min_cohort_size
    quality_ok = metrics.median_cohort_dispersion <= cfg.promising_max_dispersion and metrics.reappearance_ratio <= cfg.promising_max_reappearance_ratio
    if (confidence != "LOW" and turnover_confidence != "NONE"
            and risk_evidence.confidence != "LOW"
            and priority.score >= cfg.promising_min_mops and risk.score <= cfg.promising_max_risk
            and cohort_sample_sufficient and quality_ok):
        future = "PROMISING"
    elif metrics.sample_count > 0:
        future = "WATCH"
    else:
        future = "NOT_READY"
    explanations = []
    c = priority.components
    for key, label in (("depth_score", "market depth"), ("seller_diversity_score", "independent sellers"),
                       ("budget_accessibility_score", "budget accessibility"), ("cohortability_score", "cohort coverage"),
                       ("turnover_proxy_score", "listing churn signal"), ("price_structure_score", "within-cohort pricing")):
        if key == "turnover_proxy_score" and turnover_confidence == "NONE":
            explanations.append("- listing churn signal (unavailable)")
        else:
            explanations.append(("+ " if c[key] >= 60 else "- ") + label + f" ({c[key]:.0f})")
    explanations.append(f"- observed account risk ({risk.score:.0f})" if risk.score >= 50 else f"+ lower observed risk ({risk.score:.0f})")
    seed = ACCOUNT_MARKET_SEEDS[market_id]
    return AccountMarketAssessment(market_id, str(seed["name"]), int(seed["node_id"]), metrics,
                                   priority, risk, confidence, turnover_confidence, eligible, failures, future,
                                   tuple(explanations), risk_evidence)


def rank_account_markets(db, config: Optional[AccountMarketConfig] = None,
                         now: Optional[float] = None) -> List[AccountMarketAssessment]:
    assessments = [assess_account_market(db, market_id, config, now) for market_id in ACCOUNT_MARKET_SEEDS]
    # Names and seed order never affect score. market_id only makes ties deterministic.
    return sorted(assessments, key=lambda item: (-int(item.eligible), -item.mops, item.market_id))


class ActiveMarketSelector:
    """Top-N selection with score hysteresis held for consecutive samples."""
    def __init__(self, config: Optional[AccountMarketConfig] = None):
        self.config = config or AccountMarketConfig()
        self.active: Tuple[str, ...] = ()
        self._pending: Optional[Tuple[Tuple[str, ...], int]] = None

    def select(self, assessments: Sequence[AccountMarketAssessment]) -> Tuple[str, ...]:
        ranked = sorted((a for a in assessments if a.eligible), key=lambda a: (-a.mops, a.market_id))
        desired = tuple(a.market_id for a in ranked[:self.config.max_active_markets])
        score_map = {a.market_id: a.mops for a in assessments}
        if not self.active:
            self.active = desired
            return self.active
        eligible_ids = {a.market_id for a in ranked}
        retained = tuple(m for m in self.active if m in eligible_ids)
        if len(retained) < min(self.config.max_active_markets, len(desired)):
            self.active = desired
            self._pending = None
            return self.active
        entrants = [m for m in desired if m not in self.active]
        incumbents = sorted((m for m in self.active if m not in desired), key=lambda m: (score_map.get(m, -1), m))
        if not entrants or not incumbents:
            self._pending = None
            return self.active
        if min(score_map[e] for e in entrants) < max(score_map[i] for i in incumbents) + self.config.hysteresis_points:
            self._pending = None
            return self.active
        if self._pending and self._pending[0] == desired:
            self._pending = (desired, self._pending[1] + 1)
        else:
            self._pending = (desired, 1)
        if self._pending[1] >= self.config.hysteresis_samples:
            self.active = desired
            self._pending = None
        return self.active


# Backwards-friendly explicit name from the specification.
AccountMarketSelector = ActiveMarketSelector


class AccountMarketObserver:
    """Scheduler facade which only performs public GET observations."""
    source_type = ACCOUNT_OBSERVATION_SOURCE
    purchase_eligible = False

    def __init__(self, client, db, config: Optional[AccountMarketConfig] = None, rng=None):
        self.client, self.db = client, db
        self.config = config or AccountMarketConfig()
        self.selector = ActiveMarketSelector(self.config)
        self._rng = rng or random.Random()
        self._next_poll = {market_id: 0.0 for market_id in ACCOUNT_MARKET_SEEDS}
        self._market_locks = {market_id: asyncio.Lock() for market_id in ACCOUNT_MARKET_SEEDS}
        self._failure_counts = {market_id: 0 for market_id in ACCOUNT_MARKET_SEEDS}
        self._running = False

    def assessments(self, now: Optional[float] = None) -> List[AccountMarketAssessment]:
        return rank_account_markets(self.db, self.config, now)

    def active_markets(self, now: Optional[float] = None) -> Tuple[str, ...]:
        return self.selector.select(self.assessments(now))

    async def poll_market(self, market_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        if market_id not in ACCOUNT_MARKET_SEEDS:
            raise ValueError("unsupported account market")
        lock = self._market_locks[market_id]
        if lock.locked():
            return {"market_id": market_id, "skipped": True, "reason": "scan_already_running"}
        node_id = int(ACCOUNT_MARKET_SEEDS[market_id]["node_id"])
        ts = time.time() if now is None else float(now)
        async with lock:
            previous_complete_count = self.db.get_last_complete_account_market_count(market_id)
            try:
                if hasattr(self.client, "fetch_account_market_snapshot"):
                    snapshot = await self.client.fetch_account_market_snapshot(
                        node_id, previous_complete_count=previous_complete_count)
                else:
                    lots = await self.client.fetch_account_market_lots(node_id)
                    quality = SnapshotQuality.COMPLETE if lots else SnapshotQuality.FAILED
                    snapshot = AccountMarketSnapshot(
                        market_id, node_id, lots, len(lots) if lots else None, len(lots),
                        1.0 if lots else None, bool(lots), bool(lots), quality, 0.0,
                        failure_reason="legacy_fetch_empty" if not lots else "",
                    )
            except Exception as error:
                snapshot = AccountMarketSnapshot(
                    market_id, node_id, [], None, 0, None, False, False,
                    SnapshotQuality.FAILED, 0.0,
                    failure_reason="fetch_exception:" + type(error).__name__,
                )
            result = self.db.record_account_market_observation(
                market_id, snapshot.lots, now=ts,
                sample_interval_seconds=self.config.sample_interval_seconds,
                snapshot_quality=snapshot.quality.value,
                snapshot_diagnostics=snapshot.as_diagnostic(),
            )
            result["assessment"] = assess_account_market(self.db, market_id, self.config, ts)
            return result

    async def poll_due_once(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        results = []
        active = set(self.active_markets(ts))
        for market_id in sorted(ACCOUNT_MARKET_SEEDS):
            if ts < self._next_poll[market_id]:
                continue
            try:
                result = await self.poll_market(market_id, ts)
                results.append(result)
            except Exception as error:
                logger.warning("Account market GET observation failed for %s: %s", market_id, error)
                result = {"snapshot_quality": SnapshotQuality.FAILED.value}
            if result.get("snapshot_quality") == SnapshotQuality.FAILED.value:
                self._failure_counts[market_id] += 1
                delay = min(self.config.failure_backoff_max_seconds,
                            self.config.failure_backoff_initial_seconds * (2 ** (self._failure_counts[market_id] - 1)))
            elif result.get("skipped"):
                delay = 5.0
            elif market_id in active:
                self._failure_counts[market_id] = 0
                delay = self._rng.uniform(self.config.active_poll_seconds, self.config.active_poll_max_seconds)
            else:
                self._failure_counts[market_id] = 0
                delay = self._rng.uniform(self.config.background_poll_seconds, self.config.background_poll_max_seconds)
            self._next_poll[market_id] = ts + delay
        self.selector.select(self.assessments(ts))
        return results

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_due_once()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Account observer loop failed")
                await asyncio.sleep(10.0)

    def stop(self) -> None:
        self._running = False


def market_alias_to_id(value: str) -> Optional[str]:
    clean = str(value or "").lower().strip()
    for market_id, seed in ACCOUNT_MARKET_SEEDS.items():
        if clean in set(seed["aliases"]) | {market_id, str(seed["node_id"])}:
            return market_id
    return None


def top_cohorts(db, market_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in db.get_account_cohort_samples(market_id, limit=1000):
        latest.setdefault(row["cohort_id"], row)
    return sorted(latest.values(), key=lambda row: (-row["active_lots"], row["cohort_id"]))[:limit]


def format_account_markets(db, selector: Optional[ActiveMarketSelector] = None,
                           now: Optional[float] = None) -> str:
    ranked = rank_account_markets(db, now=now)
    active = set((selector or ActiveMarketSelector()).select(ranked))
    lines = ["<b>Account Markets</b>", "<i>OBSERVATION ONLY</i>", ""]
    for index, assessment in enumerate(ranked, 1):
        m = assessment.metrics
        mode = "ACTIVE WATCH" if assessment.market_id in active else "BACKGROUND"
        lines.extend([f"{index}. <b>{assessment.name}</b>",
                      f"MOPS: {assessment.mops:.0f} | Risk: {assessment.risk_score:.0f}",
                      f"Observation confidence: {assessment.confidence} | Turnover confidence: {assessment.turnover_confidence}",
                      f"Active lots: {m.active_lots:,} | Sellers: {m.independent_sellers:,}",
                      f"≤{ACCOUNT_CHEAP_RUB_THRESHOLD:.0f} RUB: {m.cheap_lots:,} | Cohort coverage: {m.parseable_ratio:.0%}",
                      f"Mode: {mode}", ""])
    return "\n".join(lines).strip()


def format_account_market_detail(db, market_id: str, now: Optional[float] = None) -> str:
    a = assess_account_market(db, market_id, now=now)
    m = a.metrics
    advertised = f"{m.advertised_market_count:,}" if m.advertised_market_count is not None else "unknown"
    coverage = f"{m.coverage_ratio:.1%}" if m.coverage_ratio is not None else "unknown"
    scan_age = f"{m.scan_age_seconds:.0f}s" if m.scan_age_seconds is not None else "unknown"
    lines = [f"<b>{a.name} — Account Market</b>", "<b>OBSERVATION ONLY</b>",
             f"MOPS: {a.mops:.1f} | Observed risk: {a.risk_score:.1f}",
             f"Observation confidence: {a.confidence} | Turnover confidence: {a.turnover_confidence}",
             f"Risk evidence: {a.risk_evidence.confidence} ({a.risk_evidence.coverage_ratio:.0%})",
             f"Parsed: {m.parsed_count:,} | Advertised: {advertised} | Coverage: {coverage}",
             f"Snapshot: {m.snapshot_quality} | Scan age: {scan_age} | Duration: {m.scan_duration:.2f}s",
             f"Eligible: {'YES' if a.eligible else 'NO'} | Future: {a.future_flip_eligibility}",
             f"Lots/sellers: {m.active_lots:,}/{m.independent_sellers:,}",
             f"Cheap segment: {m.cheap_lots:,} lots, {m.cheap_independent_sellers:,} sellers",
             f"Cohort coverage: {m.parseable_ratio:.1%}; cohorts: {m.cohort_count}; median size: {m.median_cohort_size:.1f}",
             f"Largest/top-3 seller share: {m.largest_seller_share:.1%}/{m.top3_seller_share:.1%}; HHI: {m.hhi:.3f}",
             ("Turnover: unavailable (no safe absence reconciliation)" if a.turnover_confidence == "NONE"
              else f"Disappearances/h: {m.disappearance_rate:.2f}; reappearance ratio: {m.reappearance_ratio:.1%}"),
             f"MOPS components: " + ", ".join(f"{k.replace('_score','')}={v:.0f}" for k, v in a.priority.components.items()),
             f"Risk signals: {', '.join(a.risk.signals) or 'none observed'}", "", "<b>Top cohorts</b>"]
    cohorts = top_cohorts(db, market_id)
    if not cohorts:
        lines.append("No priced cohort samples yet.")
    for row in cohorts:
        lines.append(f"{row['cohort_id']} — lots {row['active_lots']}, sellers {row['independent_sellers']}, "
                     f"P25/P50 {row['p25']}/{row['p50']} RUB, disappearances {row['disappearance_count']}, "
                     f"reappearances {row['reappearance_count']}")
    if a.eligibility_failures:
        lines.extend(["", "Gates: " + ", ".join(a.eligibility_failures)])
    return "\n".join(lines)


def format_account_market_summary(db, now: Optional[float] = None) -> str:
    ranked = rank_account_markets(db, now=now)
    eligible = [a for a in ranked if a.eligible]
    if not any(a.metrics.sample_count for a in ranked):
        return "<b>Account Markets — Night Summary</b>\nOBSERVATION ONLY\nNo real observation samples recorded yet."
    by_component = lambda key: max(ranked, key=lambda a: (a.priority.components[key], a.market_id))
    highest_risk = max(ranked, key=lambda a: (a.risk_score, a.market_id))
    lowest_risk = min(ranked, key=lambda a: (a.risk_score, a.market_id))
    duration = max(a.metrics.observation_duration_hours for a in ranked)
    sample_count = sum(a.metrics.sample_count for a in ranked)
    all_cohorts = []
    for a in ranked:
        for row in top_cohorts(db, a.market_id, limit=100):
            all_cohorts.append((a.name, row))
    activity = sorted(all_cohorts, key=lambda pair: (-pair[1]["disappearance_count"] - pair[1]["new_count"], pair[1]["cohort_id"]))[:5]
    cheap = sorted(all_cohorts, key=lambda pair: (-pair[1]["cheap_lot_count"], pair[1]["cohort_id"]))[:5]
    lines = ["<b>Account Markets — Night Summary</b>", "<b>OBSERVATION ONLY</b>",
             f"Observation duration: {duration:.2f}h | Samples: {sample_count} | Eligible: {len(eligible)}",
             "Observation confidence: " + ", ".join(f"{a.name}={a.confidence}" for a in ranked),
             "Turnover confidence: " + ", ".join(f"{a.name}={a.turnover_confidence}" for a in ranked),
             f"Best observation market: {ranked[0].name} ({ranked[0].mops:.0f})",
             f"Most cohortable: {by_component('cohortability_score').name}",
             f"Best budget accessibility: {by_component('budget_accessibility_score').name}",
             (f"Highest turnover proxy: {by_component('turnover_proxy_score').name}"
              if any(a.turnover_confidence != "NONE" for a in ranked) else "Turnover proxy: unavailable"),
             f"Highest observed risk: {highest_risk.name} ({highest_risk.risk_score:.0f})",
             f"Lowest observed risk: {lowest_risk.name} ({lowest_risk.risk_score:.0f})", "", "Top cohorts by activity:"]
    lines.extend(f"- {name}: {row['cohort_id']} ({row['new_count'] + row['disappearance_count']} changes)" for name, row in activity)
    lines.append("Top cohorts ≤500 RUB:")
    lines.extend(f"- {name}: {row['cohort_id']} ({row['cheap_lot_count']} lots)" for name, row in cheap)
    return "\n".join(lines)
