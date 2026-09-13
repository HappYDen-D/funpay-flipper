"""Deterministic, fail-closed cohort normalization for FunPay account markets.

This module intentionally contains no network, LLM, purchase, or messaging code.
False splits are preferred to false merges: contradictory or incomplete key
features produce an unclassified result which is excluded from cohort pricing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


ACCOUNT_OBSERVATION_SOURCE = "ACCOUNT_OBSERVATION"
UNCLASSIFIED = "UNCLASSIFIED"
UNCLASSIFIED_SPECIALTY = "UNCLASSIFIED_SPECIALTY"

BRAWL_TROPHY_BOUNDS: Tuple[int, ...] = (10_000, 20_000, 30_000, 40_000, 50_000, 60_000)
BRAWL_BRAWLER_BOUNDS: Tuple[int, ...] = (20, 40, 60, 80, 100)
FORTNITE_SKIN_BOUNDS: Tuple[int, ...] = (5, 10, 25, 50, 100, 200)
COC_HERO_BOUNDS: Tuple[int, ...] = (49, 99, 139, 179, 219, 279)

SPECIALTY_COSMETICS = (
    "renegade raider", "ренегат рейдер", "aerial assault trooper",
    "black knight", "чёрный рыцарь", "черный рыцарь", "purple skull",
    "pink ghoul", "galaxy skin", "иконик", "ikonik", "double helix",
    "wonder skin", "world warrior", "rarest skin", "редчайший скин",
)


@dataclass(frozen=True)
class AccountCohortResult:
    market_id: str
    cohort_id: str
    confidence: float
    features: Dict[str, Any] = field(default_factory=dict)
    risk_flags: Tuple[str, ...] = ()
    classified: bool = False
    reason: str = ""
    source_type: str = ACCOUNT_OBSERVATION_SOURCE
    purchase_eligible: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "cohort_id": self.cohort_id,
            "confidence": self.confidence,
            "features": dict(self.features),
            "risk_flags": list(self.risk_flags),
            "classified": self.classified,
            "reason": self.reason,
            "source_type": self.source_type,
            "purchase_eligible": False,
        }


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _structured(lot: Mapping[str, Any]) -> Dict[str, Any]:
    raw = lot.get("structured_fields") or lot.get("features") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _text(lot: Mapping[str, Any]) -> str:
    fields = _structured(lot)
    values = [lot.get("title", ""), lot.get("description", "")]
    values.extend(f"{k} {v}" for k, v in sorted(fields.items()))
    return _clean(" ".join(str(v) for v in values if v is not None))


def _field_int(fields: Mapping[str, Any], keys: Iterable[str]) -> Optional[int]:
    for key in keys:
        if key in fields:
            match = re.search(r"\d[\d\s,.]*", str(fields[key]))
            if match:
                try:
                    return int(float(match.group(0).replace(" ", "").replace(",", ".")))
                except ValueError:
                    pass
    return None


def _unique_int(patterns: Iterable[str], text: str) -> Optional[int]:
    found = set()
    for pattern in patterns:
        for value in re.findall(pattern, text, flags=re.IGNORECASE):
            try:
                found.add(int(str(value).replace(" ", "")))
            except ValueError:
                continue
    return next(iter(found)) if len(found) == 1 else None


def _bucket(value: int, bounds: Tuple[int, ...], labels: Tuple[str, ...]) -> str:
    for index, upper in enumerate(bounds):
        if value <= upper:
            return labels[index]
    return labels[-1]


def _transaction_type(text: str) -> Optional[str]:
    rent = bool(re.search(r"\b(аренд\w*|rent(?:al)?|прокат|на\s+\d+\s*(?:час|дн))\b", text))
    sale = bool(re.search(r"\b(продаж\w*|sale|навсегда|полный\s+доступ|full\s+access)\b", text))
    if rent and sale:
        return None
    return "rent" if rent else "sale"  # Account nodes are sale markets unless explicitly rental.


def _risk_flags(text: str, lot: Mapping[str, Any]) -> Tuple[str, ...]:
    flags = set()
    rules = {
        "full_access_claim": r"\b(полный доступ|full access)\b",
        "email_included_claim": r"\b(с почтой|почта в комплекте|email included)\b",
        "native_email_claim": r"\b(родная|первая|original|native|first)\s+(?:почта|mail|email)\b",
        "email_change_claim": r"\b(смена почты|можно сменить почту|email change|change email)\b",
        "no_email_access": r"\b(без доступа к почте|без почты|no email access|mail not included)\b",
        "rental": r"\b(аренд\w*|rent(?:al)?|прокат)\b",
        "warranty_claim": r"\b(гарант\w*|warranty)\b",
        "ambiguous_access": r"\b(частичный доступ|semi access|без смены|вход по коду)\b",
        "platform_linking": r"\b(привяз\w*|linked|unlink|отвяз\w*|cross.?platform)\b",
        "recovery_claim": r"\b(восстанов\w*|recovery|recoverable|невосстанов\w*)\b",
        "transfer_claim": r"\b(передач\w*|transfer|перепривяз\w*)\b",
    }
    for name, pattern in rules.items():
        if re.search(pattern, text):
            flags.add(name)
    if int(lot.get("seller_reviews") or 0) < 3:
        flags.add("extremely_new_seller")
    return tuple(sorted(flags))


def _unclassified(market_id: str, reason: str, features: Optional[Dict[str, Any]] = None,
                  flags: Tuple[str, ...] = (), specialty: bool = False) -> AccountCohortResult:
    return AccountCohortResult(
        market_id=market_id,
        cohort_id=UNCLASSIFIED_SPECIALTY if specialty else UNCLASSIFIED,
        confidence=0.0,
        features=features or {},
        risk_flags=flags,
        classified=False,
        reason=reason,
    )


def _normalize_brawl(market_id: str, lot: Mapping[str, Any], text: str,
                     flags: Tuple[str, ...]) -> AccountCohortResult:
    fields = _structured(lot)
    trophies = _field_int(fields, ("trophies", "кубки", "трофеи"))
    parsed_trophies = _unique_int((r"(\d[\d ]{2,5})\s*(?:кубк\w*|трофе\w*|troph(?:y|ies))",), text)
    if trophies is not None and parsed_trophies is not None and trophies != parsed_trophies:
        return _unclassified(market_id, "structured/text trophy conflict", flags=flags)
    trophies = trophies if trophies is not None else parsed_trophies

    brawlers = _field_int(fields, ("brawlers_count", "brawlers", "бойцы", "бравлеры"))
    parsed_brawlers = _unique_int((r"(\d{1,3})\s*(?:бойц\w*|бравлер\w*|brawlers?)",), text)
    if brawlers is not None and parsed_brawlers is not None and brawlers != parsed_brawlers:
        return _unclassified(market_id, "structured/text brawler conflict", flags=flags)
    brawlers = brawlers if brawlers is not None else parsed_brawlers
    if trophies is None or brawlers is None or trophies < 0 or brawlers <= 0 or brawlers > 200:
        return _unclassified(market_id, "missing or invalid trophies/brawlers", flags=flags)

    trophy_band = _bucket(trophies, BRAWL_TROPHY_BOUNDS,
                          ("0_10k", "10_20k", "20_30k", "30_40k", "40_50k", "50_60k", "60k_plus"))
    brawler_band = _bucket(brawlers, BRAWL_BRAWLER_BOUNDS,
                           ("1_20", "21_40", "41_60", "61_80", "81_100", "101_plus"))
    features = {"trophies": trophies, "trophy_band": trophy_band,
                "brawlers_count": brawlers, "brawler_band": brawler_band}
    return AccountCohortResult(market_id, f"brawl:trophies_{trophy_band}:brawlers_{brawler_band}",
                               0.92, features, flags, True, "")


def _normalize_coc(market_id: str, lot: Mapping[str, Any], text: str,
                   flags: Tuple[str, ...]) -> AccountCohortResult:
    fields = _structured(lot)
    th = _field_int(fields, ("town_hall", "townhall", "th", "ратуша"))
    parsed_th = _unique_int((r"\bth\s*[-:]?\s*(\d{1,2})\b", r"\bтх\s*[-:]?\s*(\d{1,2})\b",
                             r"\bратуш\w*\s*(?:уров(?:ень|ня)?\s*)?(\d{1,2})\b"), text)
    if th is not None and parsed_th is not None and th != parsed_th:
        return _unclassified(market_id, "structured/text Town Hall conflict", flags=flags)
    th = th if th is not None else parsed_th
    if th is None or not 1 <= th <= 20:
        return _unclassified(market_id, "Town Hall not confidently parsed", flags=flags)

    hero_sum = _field_int(fields, ("hero_levels_total", "heroes_total", "сумма героев"))
    if hero_sum is None:
        hero_block = re.search(r"(?:геро\w*|heroes?)\s*[:\-]?\s*((?:\d{1,3}\s*[/+,]\s*){1,5}\d{1,3})", text)
        if hero_block:
            nums = [int(n) for n in re.findall(r"\d{1,3}", hero_block.group(1))]
            hero_sum = sum(nums) if nums else None
    maxed = bool(re.search(r"\b(фулл|full|max(?:ed)?|макс\w*)\b", text))
    if hero_sum is None:
        hero_band = "maxed_claim" if maxed else "unknown"
        confidence = 0.68 if maxed else 0.58
    else:
        hero_band = _bucket(hero_sum, COC_HERO_BOUNDS,
                            ("0_49", "50_99", "100_139", "140_179", "180_219", "220_279", "280_plus"))
        confidence = 0.94
    features = {"town_hall": th, "hero_levels_total": hero_sum,
                "hero_band": hero_band, "maxed_claim": maxed}
    return AccountCohortResult(market_id, f"coc:th{th}:heroes_{hero_band}", confidence,
                               features, flags, True, "")


def _normalize_fortnite(market_id: str, lot: Mapping[str, Any], text: str,
                        flags: Tuple[str, ...]) -> AccountCohortResult:
    specialty_hits = tuple(sorted(term for term in SPECIALTY_COSMETICS if term in text))
    if specialty_hits:
        return _unclassified(market_id, "specialty cosmetics require individual comparison",
                             {"specialty_terms": specialty_hits}, flags, specialty=True)

    transaction = _transaction_type(text)
    if transaction is None:
        return _unclassified(market_id, "conflicting sale/rent signals", flags=flags)
    fields = _structured(lot)
    skins = _field_int(fields, ("skins_count", "skins", "скины", "скинов"))
    parsed_skins = _unique_int((r"(\d{1,4})\s*(?:скин\w*|skins?)",), text)
    if skins is not None and parsed_skins is not None and skins != parsed_skins:
        return _unclassified(market_id, "structured/text skins conflict", flags=flags)
    skins = skins if skins is not None else parsed_skins
    if skins is None or skins <= 0 or skins > 10_000:
        return _unclassified(market_id, "skins count not confidently parsed", flags=flags)

    platforms = []
    platform_rules = {
        "pc": r"\b(pc|пк|epic)\b", "playstation": r"\b(ps[45]?|playstation)\b",
        "xbox": r"\bxbox\b", "switch": r"\b(?:nintendo\s+)?switch\b",
        "mobile": r"\b(android|ios|mobile|телефон)\b",
    }
    for platform, pattern in platform_rules.items():
        if re.search(pattern, text):
            platforms.append(platform)
    explicit_platform = _clean(fields.get("platform"))
    if explicit_platform:
        mapped = next((p for p, pattern in platform_rules.items() if re.search(pattern, explicit_platform)), None)
        if mapped and platforms and mapped not in platforms:
            return _unclassified(market_id, "structured/text platform conflict", flags=flags)
        if mapped:
            platforms.append(mapped)
    platforms = sorted(set(platforms))
    if len(platforms) != 1:
        return _unclassified(market_id, "platform missing or ambiguous", flags=flags)

    skin_band = _bucket(skins, FORTNITE_SKIN_BOUNDS,
                        ("1_5", "6_10", "11_25", "26_50", "51_100", "101_200", "200_plus"))
    access = "full" if "full_access_claim" in flags else "limited_or_unknown"
    email = "native" if "native_email_claim" in flags else (
        "changeable" if "email_change_claim" in flags else "unknown")
    features = {"platform": platforms[0], "transaction_type": transaction,
                "skins_count": skins, "skins_band": skin_band,
                "access": access, "email": email}
    cohort = f"fortnite:{transaction}:{platforms[0]}:skins_{skin_band}:access_{access}:email_{email}"
    confidence = 0.92 if access == "full" and email != "unknown" else 0.76
    return AccountCohortResult(market_id, cohort, confidence, features, flags, True, "")


def normalize_account_lot(market_id: str, lot: Mapping[str, Any]) -> AccountCohortResult:
    """Normalize one listing. Unknown markets and ambiguous key features fail closed."""
    text = _text(lot)
    flags = _risk_flags(text, lot)
    market = str(market_id).lower().strip()
    if market in ("funpay:account:436", "436", "brawl", "bs"):
        return _normalize_brawl("funpay:account:436", lot, text, flags)
    if market in ("funpay:account:147", "147", "coc"):
        return _normalize_coc("funpay:account:147", lot, text, flags)
    if market in ("funpay:account:248", "248", "fortnite", "fn"):
        return _normalize_fortnite("funpay:account:248", lot, text, flags)
    return _unclassified(market, "unsupported account market", flags=flags)


# Explicit, discoverable alias for callers which prefer noun-first naming.
normalize_account_cohort = normalize_account_lot
