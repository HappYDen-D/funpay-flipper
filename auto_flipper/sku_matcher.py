"""
auto_flipper/sku_matcher.py — Strict, fail-closed exact SKU matcher for FunPay node 1808.

Supported benchmark SKUs:
1. TF2 Mann Co. Supply Crate Key -> canonical SKU: 'tf2:5021;6'
2. TF2 Tour of Duty Ticket       -> canonical SKU: 'tf2:725;6'

Rule: Fail-closed on any ambiguity, modification, bundle, or missing info -> 'UNKNOWN'.
No LLM guessing or fuzzy approximations.
"""
import html
import re
import unicodedata
from typing import Optional, Set

SKU_TF2_KEY = "tf2:5021;6"
SKU_TF2_TICKET = "tf2:725;6"
SKU_UNKNOWN = "UNKNOWN"

BENCHMARK_SKUS = (SKU_TF2_KEY, SKU_TF2_TICKET)

SKU_NODE_MAP = {
    SKU_TF2_KEY: 1808,
    SKU_TF2_TICKET: 1808,
}

BENCHMARK_SKU_NAMES = {
    SKU_TF2_KEY: "Mann Co. Supply Crate Key",
    SKU_TF2_TICKET: "Tour of Duty Ticket",
}

# General blacklist: exclusions that immediately invalidate any exact benchmark match
_GENERAL_EXCLUSIONS = [
    # Accounts / services / subscriptions
    r"\b(аккаунт|аккаунты|account|acc)\b",
    r"\b(подписка|subscription|аренда|rent)\b",
    # Store Package (NOT a key)
    r"mann\s*co\.?\s*store\s*package|пакет\s*магазина|пакет\s*манн|пакет\s*манко",
    # Specific weapons / non-key items that contain 'box' or 'ящик'
    r"\b(black\s*box|черный\s*ящик|ч[её]рный\s*ящик)\b",
    # Wrenches / Tools
    r"\b(wrench|гаечный\s*ключ|острозуб|jag)\b",
    # Kits, Fabricators, Killstreaks
    r"\b(kit|набор|fabricator|серийного|профессионального|killstreak|специализированного)\b",
    # Item qualities / modifications
    r"\b(strange|странного\s*типа|странный)\b",
    r"\b(unusual|необычного\s*типа|необычный)\b",
    r"\b(vintage|старинного\s*типа|старинный)\b",
    r"\b(genuine|подлинного\s*типа|подлинный)\b",
    r"\b(australium|австралий)\b",
    r"\b(haunted|проклятый)\b",
    r"\b(collector'?s|коллекционный)\b",
    r"\b(war\s*paint|боевая\s*краска)\b",
    r"\b(скин|скины|skin|skins)\b",
    r"\b(badge|значок|pin|медаль)\b",
    r"\b(taunt|насмешка)\b",
    # Other TF2 items
    r"\b(расширитель|expander|рюкзак|backpack)\b",
    r"\b(ярлык|name\s*tag|description\s*tag)\b",
    r"\b(duck\s*journal|утиный\s*журнал)\b",
    r"\b(voucher|ваучер|купон|талон)\b",  # Squad surplus voucher is sku 727;6, not ticket 725;6
    # Ticket Boy cosmetic
    r"\b(билетер|билет[её]р|ticket\s*boy)\b",
    # Random / unverified game keys
    r"\b(рандом|random)\b",
]

# Mann Co. Supply Crate Key positive matching patterns
_KEY_PATTERNS = [
    r"mann\s*co\.?\s*(supply\s*crate\s*)?keys?",
    r"mann\s*box\s*keys?",
    r"mann\s*ko\s*keys?",
    r"ключ[иа]?\s*(от\s*ящика\s*)?манн(\s*ко|\.ко)?",
    r"ключ[иа]?\s*ящика\s*манн(\s*ко|\.ко)?",
    r"ключ[иа]?\s*(от\s*ящика\s*)?mann\s*(co|\.co)?",
    r"mann\s*(co|\.co)?\s*ключ[иа]?",
    r"ключ[иа]?\s*mann\b",
    r"\bmann\s*ключ[иа]?",
    r"ключ[иа]?\s*(для\s*ящиков|от\s*ящиков)\s*(тф2?|tf2?)",
    r"\b(тф2?|tf2?)\s*ключ[иа]?\b",
    r"\bключ[иа]?\s*(тф2?|tf2?)\b",
    r"\b(тф2?|tf2?)\s*keys?\b",
    r"\bkeys?\s*(тф2?|tf2?)\b",
    r"манн\s*ко\s*ключ[иа]?",
    r"манко\s*ключ[иа]?",
    r"ключ[иа]?\s*манн",
    r"ключ[иа]?\s*манко",
    r"\bmonco\s*keys?\b",
    r"\bключ[иа]?\s*monco\b",
]

# Tour of Duty Ticket positive matching patterns
_TICKET_PATTERNS = [
    r"tour\s*of\s*duty(\s*tickets?)?",
    r"командировочн(ый|ые|ого)\s*билет[а-я]*",
    r"билет[а-я]*\s*(на\s*)?командировк[а-я]*",
    r"билет[а-я]*\s*(на\s*)?служб[а-я]*",
    r"билет[а-я]*\s*(мвм|mvm)",
    r"билет[а-я]*\s*tour\s*of\s*duty",
]


def is_benchmark_sku(sku: Optional[str]) -> bool:
    """Returns True if the given SKU string is one of the designated benchmark SKUs."""
    return sku in BENCHMARK_SKUS


def get_sku_display_name(sku: str) -> str:
    """Returns friendly English display name for known benchmark SKUs, formatted discovery name, or the raw SKU."""
    if sku in BENCHMARK_SKU_NAMES:
        return BENCHMARK_SKU_NAMES[sku]
    if isinstance(sku, str) and sku.startswith("tf2_disc:"):
        return sku[len("tf2_disc:") :].replace("_", " ").title()
    return sku


def match_sku(node_id: int, title: str) -> str:
    """
    Strictly matches a FunPay listing title to a canonical SKU.
    Only node_id 1808 (TF2 Items) is currently evaluated for TF2 benchmarks.
    Fails closed: returns SKU_UNKNOWN ('UNKNOWN') on ambiguity or unverified item.
    """
    if node_id != 1808 or not isinstance(title, str) or not title.strip():
        return SKU_UNKNOWN

    # Unescape HTML entities (e.g. &quot; -> ", &#039; -> ') and normalize unicode
    unescaped = html.unescape(title)
    t = unicodedata.normalize("NFC", unescaped.lower())

    # Check general fail-closed exclusions on raw normalized title
    for exc in _GENERAL_EXCLUSIONS:
        if re.search(exc, t, re.IGNORECASE):
            return SKU_UNKNOWN

    # Normalized text with non-alphanumerics (emojis, borders) replaced by spaces
    t_clean = re.sub(r"[^\w\sа-яёА-ЯЁa-zA-Z0-9]", " ", t)
    t_clean = re.sub(r"\s+", " ", t_clean).strip()

    # Re-check exclusions on clean title to catch separated keywords
    for exc in _GENERAL_EXCLUSIONS:
        if re.search(exc, t_clean, re.IGNORECASE):
            return SKU_UNKNOWN

    # Exclude cases/crates/boxes that do not explicitly mention being a key
    if re.search(r"\b(ящик|сундук|crate|case|box)\b", t, re.IGNORECASE) or re.search(
        r"\b(ящик|сундук|crate|case|box)\b", t_clean, re.IGNORECASE
    ):
        if not (
            re.search(r"\b(ключ|ключи|key|keys)\b", t, re.IGNORECASE)
            or re.search(r"\b(ключ|ключи|key|keys)\b", t_clean, re.IGNORECASE)
        ):
            return SKU_UNKNOWN

    # Detect presence of key vs ticket terms
    has_key_word = bool(
        re.search(r"\b(ключ|ключи|key|keys)\b", t, re.IGNORECASE)
        or re.search(r"\b(ключ|ключи|key|keys)\b", t_clean, re.IGNORECASE)
    )
    has_ticket_word = bool(
        re.search(r"билет|ticket|duty", t, re.IGNORECASE)
        or re.search(r"билет|ticket|duty", t_clean, re.IGNORECASE)
    )

    # Mixed bundle / ambiguous mention of both key and ticket -> UNKNOWN
    if has_key_word and has_ticket_word:
        return SKU_UNKNOWN

    # Match Key
    is_key = False
    if has_key_word:
        for pat in _KEY_PATTERNS:
            if re.search(pat, t, re.IGNORECASE) or re.search(pat, t_clean, re.IGNORECASE):
                is_key = True
                break

    # Match Ticket
    is_ticket = False
    if has_ticket_word:
        for pat in _TICKET_PATTERNS:
            if re.search(pat, t, re.IGNORECASE) or re.search(pat, t_clean, re.IGNORECASE):
                is_ticket = True
                break

    # Fail closed on ambiguity
    if is_key and is_ticket:
        return SKU_UNKNOWN
    if is_key:
        return SKU_TF2_KEY
    if is_ticket:
        return SKU_TF2_TICKET

    return SKU_UNKNOWN
