"""
auto_flipper/liquidity.py — Quantitative Liquidity Evaluator and Confidence model.

Evaluates observed market behavior across 5 core signal pillars:
1. turnover / churn proxy (high weight)
2. price stability / volatility (high weight)
3. price dispersion / tightness (medium weight)
4. reappearance cleanliness (medium weight)
5. competition depth near market bottom (medium weight)

Outputs:
- Liquidity Score: 0..100
- Confidence: LOW / MEDIUM / HIGH (strictly decoupled from Score)
"""
from dataclasses import asdict, dataclass, field
import logging
import statistics
from typing import Any, Dict, List, Optional, Tuple

from auto_flipper.sku_matcher import BENCHMARK_SKUS, get_sku_display_name

logger = logging.getLogger("LiquidityEvaluator")


@dataclass(frozen=True)
class LiquidityConfig:
    """Configurable weights and thresholds for Liquidity Scoring and Confidence."""

    # Weights of the 5 scoring pillars (must sum to 1.0)
    weight_turnover: float = 0.30
    weight_stability: float = 0.25
    weight_dispersion: float = 0.15
    weight_reappearance: float = 0.15
    weight_competition: float = 0.15

    # Turnover proxy targets
    target_turnover_rate_per_hour: float = 2.0  # >= 2 net disappearances/h gives full turnover score

    # Volatility and dispersion thresholds
    max_acceptable_volatility: float = 0.15  # stddev(p50)/mean(p50) above 15% yields zero stability score
    target_dispersion_tight: float = 0.15   # (p90 - p10)/p50 <= 15% yields full dispersion score
    max_acceptable_dispersion: float = 0.80 # (p90 - p10)/p50 >= 80% yields zero dispersion score

    # Competition depth
    bottom_price_band_pct: float = 0.05    # listings within 5% of min price
    target_sellers_count: int = 5          # >= 5 unique sellers gives full seller diversity score
    target_bottom_listings: int = 3        # >= 3 active listings near bottom gives full depth score

    # Confidence duration & sample thresholds
    min_duration_hours_medium: float = 1.0
    min_samples_medium: int = 12
    min_duration_hours_high: float = 4.0
    min_samples_high: int = 36
    min_active_lots_high: int = 3


@dataclass
class LiquidityMetrics:
    """Extracted market dynamics and statistics for a canonical SKU."""

    canonical_sku: str
    active_listings: int
    unique_sellers: int
    observed_stock: int
    min_price: float
    p10_price: float
    p25_price: float
    p50_price: float
    p90_price: float
    max_price: float
    price_dispersion: float
    p25_change_pct: float
    p50_change_pct: float
    price_volatility: float
    observation_duration_hours: float
    sample_count: int
    new_listings_count: int
    new_listings_rate: float
    disappearance_count: int
    disappearance_rate: float
    reappearance_count: int
    reappearance_rate: float
    reappearance_ratio: float
    turnover_proxy: float
    median_listing_age_hours: float
    competition_depth_bottom: int
    competition_depth_sellers: int


@dataclass
class LiquidityAssessment:
    """Final calibrated liquidity score and independent confidence rating."""

    canonical_sku: str
    sku_name: str
    score: float                      # 0..100
    confidence: str                   # 'LOW' | 'MEDIUM' | 'HIGH'
    confidence_reasons: List[str]
    sub_scores: Dict[str, float]      # Breakdown of sub-component scores 0..100
    metrics: LiquidityMetrics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_sku": self.canonical_sku,
            "sku_name": self.sku_name,
            "score": self.score,
            "confidence": self.confidence,
            "confidence_reasons": self.confidence_reasons,
            "sub_scores": self.sub_scores,
            "metrics": asdict(self.metrics),
        }


def compute_liquidity_metrics(
    db, canonical_sku: str, now: Optional[float] = None
) -> Optional[LiquidityMetrics]:
    """
    Queries `db` (MarketHistoryStore) to compute comprehensive liquidity metrics
    for the specified canonical SKU.
    """
    import time
    ts = time.time() if now is None else float(now)

    active_lots = db.get_market_lots_for_sku(canonical_sku, active_only=True)
    all_lots = db.get_market_lots_for_sku(canonical_sku, active_only=False)
    events = db.get_market_events_for_sku(canonical_sku)
    samples = db.get_market_samples_for_sku(canonical_sku, limit=500)

    if not all_lots and not samples:
        return None

    active_count = len(active_lots)
    unique_sellers = len({l["seller"] for l in active_lots})
    observed_stock = sum(int(l.get("stock", 1)) for l in active_lots)

    # Observation duration
    if samples:
        min_sample_time = min(s["sampled_at"] for s in samples)
        max_sample_time = max(s["sampled_at"] for s in samples)
        sample_duration = max(0.0, max_sample_time - min_sample_time)
    else:
        sample_duration = 0.0

    if all_lots:
        min_lot_time = min(l["first_seen_at"] for l in all_lots)
        max_lot_time = max(l["last_seen_at"] for l in all_lots)
        lot_duration = max(0.0, max_lot_time - min_lot_time)
    else:
        lot_duration = 0.0

    duration_seconds = max(sample_duration, lot_duration)
    duration_hours = duration_seconds / 3600.0
    effective_hours = max(1.0, duration_hours)

    # Prices and quantiles
    if active_lots:
        prices = sorted(float(l["price"]) for l in active_lots)
        min_p = prices[0]
        max_p = prices[-1]
        from auto_flipper.market_store import compute_quantile
        p10_p = compute_quantile(prices, 0.10)
        p25_p = compute_quantile(prices, 0.25)
        p50_p = compute_quantile(prices, 0.50)
        p90_p = compute_quantile(prices, 0.90)
        dispersion = round((p90_p - p10_p) / max(0.01, p50_p), 4)

        # Listing age
        ages = [max(0.0, ts - l["first_seen_at"]) for l in active_lots]
        median_age_hours = round(statistics.median(ages) / 3600.0, 2)

        # Competition depth near bottom
        bottom_threshold = min_p * 1.05
        bottom_lots = [l for l in active_lots if float(l["price"]) <= bottom_threshold]
        competition_depth_bottom = len(bottom_lots)
        competition_depth_sellers = len({l["seller"] for l in bottom_lots})
    elif samples:
        latest_s = samples[0]
        min_p = latest_s["min_price"]
        max_p = latest_s["max_price"]
        p10_p = latest_s["p10_price"]
        p25_p = latest_s["p25_price"]
        p50_p = latest_s["p50_price"]
        p90_p = latest_s["p90_price"]
        dispersion = latest_s["price_dispersion"]
        median_age_hours = 0.0
        competition_depth_bottom = 0
        competition_depth_sellers = 0
    else:
        min_p = p10_p = p25_p = p50_p = p90_p = max_p = dispersion = 0.0
        median_age_hours = 0.0
        competition_depth_bottom = 0
        competition_depth_sellers = 0

    # Events breakdown
    disappeared_count = sum(1 for e in events if e.get("event_type") == "DISAPPEARED")
    reappeared_count = sum(1 for e in events if e.get("event_type") == "REAPPEARED")
    new_count = sum(1 for e in events if e.get("event_type") == "NEW")

    disappearance_rate = round(disappeared_count / effective_hours, 2)
    reappearance_rate = round(reappeared_count / effective_hours, 2)
    new_listings_rate = round(new_count / effective_hours, 2)
    reappearance_ratio = (
        round(reappeared_count / max(1, disappeared_count), 4) if disappeared_count > 0 else 0.0
    )

    # Net turnover proxy: penalizes disappearances that merely reappeared later
    net_disappearances = max(0, disappeared_count - reappeared_count)
    turnover_proxy = round(net_disappearances / effective_hours, 2)

    # Historical price changes & volatility across samples
    if len(samples) >= 2:
        oldest_sample = samples[-1]
        newest_sample = samples[0]
        if oldest_sample["p25_price"] > 0:
            p25_change_pct = round(
                ((newest_sample["p25_price"] - oldest_sample["p25_price"]) / oldest_sample["p25_price"]) * 100.0, 2
            )
        else:
            p25_change_pct = 0.0

        if oldest_sample["p50_price"] > 0:
            p50_change_pct = round(
                ((newest_sample["p50_price"] - oldest_sample["p50_price"]) / oldest_sample["p50_price"]) * 100.0, 2
            )
        else:
            p50_change_pct = 0.0

        p50_series = [s["p50_price"] for s in samples]
        mean_p50 = statistics.mean(p50_series)
        stdev_p50 = statistics.stdev(p50_series) if len(p50_series) > 1 else 0.0
        price_volatility = round((stdev_p50 / mean_p50) if mean_p50 > 0 else 0.0, 4)
    else:
        p25_change_pct = 0.0
        p50_change_pct = 0.0
        price_volatility = 0.0

    return LiquidityMetrics(
        canonical_sku=canonical_sku,
        active_listings=active_count,
        unique_sellers=unique_sellers,
        observed_stock=observed_stock,
        min_price=min_p,
        p10_price=p10_p,
        p25_price=p25_p,
        p50_price=p50_p,
        p90_price=p90_p,
        max_price=max_p,
        price_dispersion=dispersion,
        p25_change_pct=p25_change_pct,
        p50_change_pct=p50_change_pct,
        price_volatility=price_volatility,
        observation_duration_hours=round(duration_hours, 2),
        sample_count=len(samples),
        new_listings_count=new_count,
        new_listings_rate=new_listings_rate,
        disappearance_count=disappeared_count,
        disappearance_rate=disappearance_rate,
        reappearance_count=reappeared_count,
        reappearance_rate=reappearance_rate,
        reappearance_ratio=reappearance_ratio,
        turnover_proxy=turnover_proxy,
        median_listing_age_hours=median_age_hours,
        competition_depth_bottom=competition_depth_bottom,
        competition_depth_sellers=competition_depth_sellers,
    )


def evaluate_liquidity(
    metrics: LiquidityMetrics, config: Optional[LiquidityConfig] = None
) -> LiquidityAssessment:
    """
    Evaluates Liquidity Score (0..100) and Confidence (LOW / MEDIUM / HIGH)
    from extracted LiquidityMetrics according to calibrated rules.
    NOTE: Benchmark SKU identity is purely informational and NEVER artificially boosts score.
    """
    cfg = config or LiquidityConfig()

    # 1. Turnover Pillar (Weight 0.30)
    # Target: 2.0 net turnovers / hour gives 100%
    if metrics.turnover_proxy >= cfg.target_turnover_rate_per_hour:
        raw_turnover = 100.0
    else:
        raw_turnover = (metrics.turnover_proxy / max(0.01, cfg.target_turnover_rate_per_hour)) * 100.0
    # Additional penalty if reappearance ratio is high (unreliable turnover)
    turnover_score = max(0.0, min(100.0, raw_turnover * (1.0 - 0.5 * metrics.reappearance_ratio)))

    # 2. Price Stability Pillar (Weight 0.25)
    # Zero volatility & zero median drift gives 100%
    # Volatility scaling: 0.0 -> 100, cfg.max_acceptable_volatility (15%) -> 0
    vol_ratio = min(1.0, metrics.price_volatility / max(0.01, cfg.max_acceptable_volatility))
    vol_score = 100.0 * (1.0 - vol_ratio)
    drift_ratio = min(1.0, abs(metrics.p50_change_pct) / 20.0)
    drift_score = 100.0 * (1.0 - drift_ratio)
    stability_score = max(0.0, min(100.0, 0.7 * vol_score + 0.3 * drift_score))

    # 3. Price Dispersion Pillar (Weight 0.15)
    # Tight dispersion (e.g. <= 15%) gives 100%
    # Above max_acceptable_dispersion (80%) gives 0%
    if metrics.price_dispersion <= cfg.target_dispersion_tight:
        dispersion_score = 100.0
    elif metrics.price_dispersion >= cfg.max_acceptable_dispersion:
        dispersion_score = 0.0
    else:
        span = cfg.max_acceptable_dispersion - cfg.target_dispersion_tight
        fraction = (metrics.price_dispersion - cfg.target_dispersion_tight) / span
        dispersion_score = max(0.0, min(100.0, 100.0 * (1.0 - fraction)))

    # 4. Reappearance Cleanliness Pillar (Weight 0.15)
    # If 0 reappearances, 100% clean. If all disappearances reappear, 0%
    reappearance_score = max(0.0, min(100.0, 100.0 * (1.0 - metrics.reappearance_ratio)))

    # 5. Competition Depth Pillar (Weight 0.15)
    # Unique sellers component (50 pts) + bottom listings depth (50 pts)
    sellers_frac = min(1.0, metrics.unique_sellers / max(1, cfg.target_sellers_count))
    bottom_frac = min(1.0, metrics.competition_depth_bottom / max(1, cfg.target_bottom_listings))
    competition_score = max(0.0, min(100.0, sellers_frac * 50.0 + bottom_frac * 50.0))

    # Composite Score
    total_score = (
        cfg.weight_turnover * turnover_score
        + cfg.weight_stability * stability_score
        + cfg.weight_dispersion * dispersion_score
        + cfg.weight_reappearance * reappearance_score
        + cfg.weight_competition * competition_score
    )
    score_clamped = round(max(0.0, min(100.0, total_score)), 1)

    sub_scores = {
        "turnover": round(turnover_score, 1),
        "stability": round(stability_score, 1),
        "dispersion": round(dispersion_score, 1),
        "reappearance": round(reappearance_score, 1),
        "competition": round(competition_score, 1),
    }

    # Confidence Evaluation (Independent of Score)
    confidence_reasons: List[str] = []
    if metrics.active_listings == 0:
        confidence = "LOW"
        confidence_reasons.append("No active listings currently observed")
    elif (
        metrics.observation_duration_hours < cfg.min_duration_hours_medium
        or metrics.sample_count < cfg.min_samples_medium
    ):
        confidence = "LOW"
        confidence_reasons.append(
            f"Insufficient history: {metrics.observation_duration_hours:.1f}h observed (need >={cfg.min_duration_hours_medium:.1f}h), "
            f"{metrics.sample_count} samples (need >={cfg.min_samples_medium})"
        )
    elif (
        metrics.observation_duration_hours < cfg.min_duration_hours_high
        or metrics.sample_count < cfg.min_samples_high
    ):
        confidence = "MEDIUM"
        confidence_reasons.append(
            f"Moderate history: {metrics.observation_duration_hours:.1f}h observed, {metrics.sample_count} samples"
        )
    else:
        if metrics.active_listings >= cfg.min_active_lots_high:
            confidence = "HIGH"
            confidence_reasons.append(
                f"Sufficient history: {metrics.observation_duration_hours:.1f}h observed, "
                f"{metrics.sample_count} samples, {metrics.active_listings} active listings"
            )
        else:
            confidence = "MEDIUM"
            confidence_reasons.append(
                f"Sparse active listings ({metrics.active_listings}) despite {metrics.observation_duration_hours:.1f}h history"
            )

    sku_name = get_sku_display_name(metrics.canonical_sku)

    return LiquidityAssessment(
        canonical_sku=metrics.canonical_sku,
        sku_name=sku_name,
        score=score_clamped,
        confidence=confidence,
        confidence_reasons=confidence_reasons,
        sub_scores=sub_scores,
        metrics=metrics,
    )


def evaluate_sku_liquidity(
    db, canonical_sku: str, config: Optional[LiquidityConfig] = None, now: Optional[float] = None
) -> Optional[LiquidityAssessment]:
    """Convenience helper: computes metrics and evaluates liquidity assessment in one call."""
    metrics = compute_liquidity_metrics(db, canonical_sku, now=now)
    if not metrics:
        return None
    return evaluate_liquidity(metrics, config=config)
