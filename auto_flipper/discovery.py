"""
auto_flipper/discovery.py — Autonomous discovery, normalization, and blind liquidity ranking for unlisted market items.

Observation-only mechanism for FunPay node 1808 (TF2):
- Normalizes listing titles deterministically (strips emojis, quantity tags, promo clutter, decorative brackets).
- Groups listings into candidate items without LLMs or heuristic guesses.
- Never merges distinct items ("better to split one item into multiple groups than mistakenly merge different ones").
- Filters out non-items (services, "на выбор", account packs).
- Applies DiscoveryConfig thresholds to qualify candidates for liquidity ranking.
- Purely read-only: discovery candidates are never trusted SKUs and can NEVER be purchased.
"""
from dataclasses import asdict, dataclass, field
import html
import logging
import re
import statistics
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

from auto_flipper.liquidity import (
    LiquidityConfig,
    LiquidityMetrics,
    LiquidityAssessment,
    compute_liquidity_metrics,
    evaluate_liquidity,
)
from auto_flipper.sku_matcher import (
    BENCHMARK_SKUS,
    BENCHMARK_SKU_NAMES,
    SKU_TF2_KEY,
    SKU_TF2_TICKET,
    SKU_UNKNOWN,
    is_benchmark_sku,
    match_sku,
)

logger = logging.getLogger("DiscoveryEngine")

DISCOVERY_SKU_PREFIX = "tf2_disc:"

# Explicit non-item patterns: services, choice packs, account sales, random keys
_SERVICE_OR_PACK_PATTERNS = [
    r"\b(на\s*выбор|на\s*ваш\s*выбор)\b",
    r"\b(все\s*оружия|все\s*вещи\s*на|вещи\s*на\s*класс|оружия\s*на\s*все|любой\s*предмет)\b",
    r"\b(аккаунт\w*|account\w*|acc)\b",
    r"\b(подписк\w*|subscription\w*|аренд\w*|rent\w*)\b",
    r"\b(рандом\w*|random\w*)\b",
    r"\b(сет\s*на\s*выбор|набор\s*на\s*выбор|вещи\s*на\s*выбор|рескин\s*на\s*выбор)\b",
]

# Quantity indicators stripped from listing titles
_QUANTITY_PATTERNS = [
    r"\[\s*(?:от\s*)?\d+\s*шт\.?\s*\]",
    r"\(\s*(?:от\s*)?\d+\s*шт\.?\s*\)",
    r"\bот\s*\d+\s*шт\.?\b",
    r"\b\d+\s*шт\.?\b",
    r"\b\d+\s*pcs?\b",
    r"\bx\d+\b",
    r"\b\d+\s*lvl\b",
    r"\b\d+\s*уровн\w*\b",
]

# Marketing and promotional clutter stripped from listing titles
_PROMO_PATTERNS = [
    r"\+?\s*подарок\b",
    r"\+?\s*подарка\b",
    r"\+?\s*подарков\b",
    r"бонус\s*за\s*отзыв",
    r"за\s*отзыв(?:\s*\d+)?\s*подарк\w*",
    r"быстрая\s*выдача",
    r"мгновенн\w*",
    r"по\s*трейду",
    r"скрин\w*\s*в\s*описании",
    r"делаю\s*под\s*заказ",
    r"смотри\s*описание",
    r"выгодная\s*цена",
    r"низкая\s*цена",
    r"по\s*низкой\s*цене",
    r"акция",
    r"гарантия",
    r"автовыдача",
    r"скидк\w*",
    r"фото\s*ниже",
    r"быстро",
    r"дешево",
    r"дёшево",
    r"сразу\s*после\s*оплаты",
    r"без\s*бана",
    r"достану\s*любые\s*вещи",
    r"мгновенная\s*доставка",
    r"моментальн\w*",
    r"надежно",
    r"лучшая\s*цена",
    r"топ\s*цена",
    r"дешевле\s*всех",
    r"\b(tf\s*2|тф\s*2|тим\s*фортресс\s*2|team\s*fortress\s*2)\b",
]

# Decorative symbols, borders, and emojis regex
_DECORATIVE_SYMBOLS = (
    r"[\U00010000-\U0010ffff]|[\u2000-\u3300]|"
    r"[●▬►◄★☆⭐✨⚡🔥💎👑🎁✅☑️✔❌❗❓❤🖤💖🌸🔪🦆💉🟨🪙🔘🔵🟦🟩🟥🟧🪖💌📸👉👈👥🏃💪🎩⛩️🏷️🎫🔑📦🎒🏹💣⚾🥪🦅🔧]"
)

# Bracket and delimiter punctuation
_DELIMITERS = r"[【】『』〔〕［］\[\]\(\)\{\}\<\>\|\\_\-\—\–+=*&^%$#@!~`\"\'«».,:;?]"


def is_service_or_pack(title: str) -> bool:
    """Detects multi-item choice menus, accounts, or services that are not distinct single items."""
    if not isinstance(title, str) or not title.strip():
        return True
    t_lower = title.lower()
    for pat in _SERVICE_OR_PACK_PATTERNS:
        if re.search(pat, t_lower):
            return True
    return False


def normalize_title(raw_title: str) -> str:
    """
    Deterministically normalizes a FunPay listing title:
    1. Unescapes HTML entities and normalizes unicode (NFKC).
    2. Strips quantity tags ([От 1 шт.], x1, 10 шт).
    3. Strips advertising and promotional junk phrases.
    4. Strips emojis and decorative symbols.
    5. Strips framing brackets and punctuation.
    6. Normalizes whitespace to single spaces and lowercases.
    """
    if not isinstance(raw_title, str) or not raw_title.strip():
        return ""

    t = html.unescape(raw_title)
    t = unicodedata.normalize("NFKC", t)

    # 1. Strip quantities
    for pat in _QUANTITY_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)

    # 2. Strip promo phrases
    for pat in _PROMO_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)

    # 3. Strip emojis and decorative symbols
    t = re.sub(_DECORATIVE_SYMBOLS, " ", t)

    # 4. Strip punctuation and brackets
    t = re.sub(_DELIMITERS, " ", t)

    # 5. Normalize whitespace
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def extract_discovery_sku(node_id: int, title: str) -> Optional[str]:
    """
    Extracts deterministic discovery SKU for unclassified lots on TF2 node 1808.
    Returns None if:
    - node_id is not 1808
    - lot matches known benchmark SKU (Mann Co Key, Tour of Duty Ticket)
    - title is a service, account, or multi-item pack
    - normalized title is empty or too short (< 2 chars)
    Format: 'tf2_disc:<slug>'
    """
    if node_id != 1808:
        return None

    if match_sku(node_id, title) != SKU_UNKNOWN:
        return None

    if is_service_or_pack(title):
        return None

    norm = normalize_title(title)
    if not norm or len(norm) < 2:
        return None

    slug = re.sub(r"[^a-z0-9а-яё]+", "_", norm).strip("_")
    if not slug:
        return None

    return f"{DISCOVERY_SKU_PREFIX}{slug}"


def is_discovery_sku(sku: Optional[str]) -> bool:
    """Returns True if the SKU belongs to the autonomous discovery tier."""
    return bool(sku and sku.startswith(DISCOVERY_SKU_PREFIX))


@dataclass(frozen=True)
class DiscoveryConfig:
    """Configurable thresholds for qualifying discovery candidates for liquidity evaluation."""

    min_active_lots: int = 3
    min_unique_sellers: int = 3
    min_market_samples: int = 2
    max_price_ratio: float = 5.0              # max_price / min_price <= 5.0
    min_title_token_consistency: float = 0.55 # Fraction of core tokens present across raw titles
    min_price: float = 0.10                   # Minimum sanity price (RUB)
    max_price: float = 50000.0                # Maximum sanity price (RUB)


@dataclass
class DiscoveryCandidate:
    """Represents an autonomously discovered item group with metrics and qualification status."""

    canonical_sku: str
    normalized_name: str
    display_name: str
    is_benchmark: bool
    is_qualified: bool
    qualification_reasons: List[str]
    raw_titles: List[str]
    active_lots: int
    unique_sellers: int
    min_price: float
    p25_price: float
    p50_price: float
    p90_price: float
    max_price: float
    turnover_proxy: float
    reappearance_rate: float
    price_volatility: float
    competition_depth_bottom: int
    observation_duration_hours: float
    sample_count: int
    liquidity_score: float
    confidence: str
    assessment: Optional[LiquidityAssessment] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_sku": self.canonical_sku,
            "normalized_name": self.normalized_name,
            "display_name": self.display_name,
            "is_benchmark": self.is_benchmark,
            "is_qualified": self.is_qualified,
            "qualification_reasons": self.qualification_reasons,
            "raw_titles": self.raw_titles,
            "active_lots": self.active_lots,
            "unique_sellers": self.unique_sellers,
            "min_price": self.min_price,
            "p25_price": self.p25_price,
            "p50_price": self.p50_price,
            "p90_price": self.p90_price,
            "max_price": self.max_price,
            "turnover_proxy": self.turnover_proxy,
            "reappearance_rate": self.reappearance_rate,
            "price_volatility": self.price_volatility,
            "competition_depth_bottom": self.competition_depth_bottom,
            "observation_duration_hours": self.observation_duration_hours,
            "sample_count": self.sample_count,
            "liquidity_score": self.liquidity_score,
            "confidence": self.confidence,
            "assessment": self.assessment.to_dict() if self.assessment else None,
        }


def check_candidate_qualification(
    active_lots: List[Dict[str, Any]],
    samples: List[Dict[str, Any]],
    normalized_name: str,
    config: Optional[DiscoveryConfig] = None,
) -> Tuple[bool, List[str]]:
    """
    Evaluates whether an observed group satisfies minimal candidate filters
    to qualify for official Liquidity Score ranking.
    """
    cfg = config or DiscoveryConfig()
    reasons: List[str] = []

    lot_count = len(active_lots)
    if lot_count < cfg.min_active_lots:
        reasons.append(f"Insufficient active lots ({lot_count} < {cfg.min_active_lots})")

    sellers = {l.get("seller") for l in active_lots if l.get("seller")}
    if len(sellers) < cfg.min_unique_sellers:
        reasons.append(f"Insufficient unique sellers ({len(sellers)} < {cfg.min_unique_sellers})")

    sample_count = len(samples)
    if sample_count < cfg.min_market_samples:
        reasons.append(f"Insufficient market samples ({sample_count} < {cfg.min_market_samples})")

    if active_lots:
        prices = [float(l.get("price", 0.0)) for l in active_lots]
        min_p = min(prices)
        max_p = max(prices)
        if min_p < cfg.min_price:
            reasons.append(f"Min price too low ({min_p:.2f} < {cfg.min_price:.2f} RUB)")
        if max_p > cfg.max_price:
            reasons.append(f"Max price exceeds ceiling ({max_p:.2f} > {cfg.max_price:.2f} RUB)")
        if min_p > 0 and (max_p / min_p) > cfg.max_price_ratio:
            reasons.append(
                f"Price ratio too wide ({max_p / min_p:.1f}x > {cfg.max_price_ratio:.1f}x max/min)"
            )

        # Title consistency check across active lots
        norm_tokens = set(normalized_name.split())
        if norm_tokens:
            overlaps = []
            for l in active_lots:
                title = l.get("title", "")
                cleaned = normalize_title(title)
                cleaned_tokens = set(cleaned.split())
                if cleaned_tokens:
                    overlap = len(norm_tokens & cleaned_tokens) / len(norm_tokens)
                    overlaps.append(overlap)
            if overlaps:
                avg_overlap = statistics.mean(overlaps)
                if avg_overlap < cfg.min_title_token_consistency:
                    reasons.append(
                        f"Inconsistent raw titles across lots (token consistency: {avg_overlap:.2f} < {cfg.min_title_token_consistency:.2f})"
                    )

    is_qualified = len(reasons) == 0
    return is_qualified, reasons


def pick_best_display_name(raw_titles: List[str], normalized_name: str) -> str:
    """Chooses the cleanest, most legible human-friendly display name from observed raw titles."""
    if not raw_titles:
        return normalized_name.title()

    # Prefer shortest titles that contain letters and no heavy decorative punctuation
    def score_title(t: str) -> int:
        penalty = 0
        if re.search(r"[\U00010000-\U0010ffff]", t):
            penalty += 50
        if any(c in t for c in "★☆⭐✨⚡🔥💎👑🎁✅☑️✔❌❗❓●▬"):
            penalty += 30
        if len(t) > 60:
            penalty += len(t) - 60
        return penalty + len(t)

    sorted_titles = sorted(raw_titles, key=score_title)
    best = sorted_titles[0].strip()
    # Clean up outer brackets or whitespace if any
    best = re.sub(r"^[\[\(\<【『\s]+|[\]\)\>】』\s]+$", "", best).strip()
    return best if best else normalized_name.title()


def rank_market_liquidity(
    db,
    discovery_config: Optional[DiscoveryConfig] = None,
    liquidity_config: Optional[LiquidityConfig] = None,
    include_benchmarks: bool = True,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Performs blind ranking of all discovered and benchmark items in the MarketHistoryStore.
    Filters discovery candidates by DiscoveryConfig.
    Evaluates LiquidityAssessment for every qualified item with ZERO benchmark bonuses.
    Returns:
    - 'top_candidates': sorted descending by Liquidity Score
    - 'bottom_candidates': sorted ascending by Liquidity Score
    - 'all_ranked': complete list of qualified ranked items
    - 'unqualified_groups': items that did not pass minimal filters
    - 'total_discovery_groups_found': int
    - 'qualified_groups_count': int
    - 'benchmark_positions': mapping of SKU -> 1-based rank in combined ranking
    """
    disc_cfg = discovery_config or DiscoveryConfig()
    liq_cfg = liquidity_config or LiquidityConfig()

    # 1. Fetch all distinct canonical SKUs present in market_history_lots
    with db._get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT canonical_sku FROM market_history_lots"
        ).fetchall()
        all_skus = [r[0] for r in rows]

    total_discovery_groups_found = sum(1 for s in all_skus if is_discovery_sku(s))

    ranked_candidates: List[DiscoveryCandidate] = []
    unqualified_groups: List[Dict[str, Any]] = []
    benchmark_positions: Dict[str, int] = {}

    for sku in all_skus:
        is_bm = is_benchmark_sku(sku)
        if is_bm and not include_benchmarks:
            continue

        active_lots = db.get_market_lots_for_sku(sku, active_only=True)
        all_lots = db.get_market_lots_for_sku(sku, active_only=False)
        samples = db.get_market_samples_for_sku(sku, limit=100)

        raw_titles = [l.get("title", "") for l in all_lots if l.get("title")]

        if is_bm:
            norm_name = BENCHMARK_SKU_NAMES.get(sku, sku)
            display_name = norm_name
            is_qualified = len(active_lots) > 0 or len(samples) > 0
            qual_reasons = [] if is_qualified else ["No active market observed"]
        else:
            slug = sku[len(DISCOVERY_SKU_PREFIX) :]
            norm_name = slug.replace("_", " ")
            display_name = pick_best_display_name(raw_titles, norm_name)
            is_qualified, qual_reasons = check_candidate_qualification(
                active_lots, samples, norm_name, config=disc_cfg
            )

        metrics = compute_liquidity_metrics(db, sku, now=now)
        if not metrics:
            continue

        assessment = evaluate_liquidity(metrics, config=liq_cfg)

        cand = DiscoveryCandidate(
            canonical_sku=sku,
            normalized_name=norm_name,
            display_name=display_name,
            is_benchmark=is_bm,
            is_qualified=is_qualified,
            qualification_reasons=qual_reasons,
            raw_titles=raw_titles,
            active_lots=metrics.active_listings,
            unique_sellers=metrics.unique_sellers,
            min_price=metrics.min_price,
            p25_price=metrics.p25_price,
            p50_price=metrics.p50_price,
            p90_price=metrics.p90_price,
            max_price=metrics.max_price,
            turnover_proxy=metrics.turnover_proxy,
            reappearance_rate=metrics.reappearance_rate,
            price_volatility=metrics.price_volatility,
            competition_depth_bottom=metrics.competition_depth_bottom,
            observation_duration_hours=metrics.observation_duration_hours,
            sample_count=metrics.sample_count,
            liquidity_score=assessment.score,
            confidence=assessment.confidence,
            assessment=assessment,
        )

        if is_qualified:
            ranked_candidates.append(cand)
        else:
            unqualified_groups.append(
                {
                    "canonical_sku": sku,
                    "normalized_name": norm_name,
                    "active_lots": metrics.active_listings,
                    "unique_sellers": metrics.unique_sellers,
                    "reasons": qual_reasons,
                }
            )

    # Deterministic sorting:
    # 1. Score descending
    # 2. Turnover proxy descending
    # 3. Active listings descending
    # 4. Canonical SKU ascending (tie-breaker for strictly deterministic ranking)
    ranked_candidates.sort(
        key=lambda c: (
            -c.liquidity_score,
            -c.turnover_proxy,
            -c.active_lots,
            c.canonical_sku,
        )
    )

    for idx, cand in enumerate(ranked_candidates, start=1):
        if cand.is_benchmark:
            benchmark_positions[cand.canonical_sku] = idx

    top_candidates = ranked_candidates[:10]
    bottom_candidates = list(reversed(ranked_candidates[-10:])) if ranked_candidates else []

    qualified_discovery_count = sum(1 for c in ranked_candidates if not c.is_benchmark)

    return {
        "top_candidates": top_candidates,
        "bottom_candidates": bottom_candidates,
        "all_ranked": ranked_candidates,
        "unqualified_groups": unqualified_groups,
        "total_discovery_groups_found": total_discovery_groups_found,
        "qualified_groups_count": qualified_discovery_count,
        "benchmark_positions": benchmark_positions,
    }
