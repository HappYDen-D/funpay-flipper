"""One-shot public listing inspection for research; no login, trades or bot imports.

Usage: python -B fast_resale_public_scan.py CATEGORY_ID [DESCRIPTION_REGEX]
Output is an asking-price sample, never a sales or liquidity estimate.
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone


def clean(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def inspect(category, pattern, max_examples=8):
    url = f"https://funpay.com/en/lots/{int(category)}/"
    with urllib.request.urlopen(url, timeout=25) as response:
        page = response.read().decode("utf-8")
    rows = []
    for attrs, body in re.findall(r'<a\s+([^>]*class="tc-item[^>]*)>(.*?)</a>', page, re.S):
        desc = re.search(r'class="tc-desc-text"[^>]*>(.*?)</div>', body, re.S)
        price = re.search(r'class="tc-price"[^>]*data-s="([^"]+)"', body)
        href = re.search(r'href="([^"]+)"', attrs)
        if not (desc and price and href):
            continue
        desc = clean(desc.group(1))
        if pattern and not re.search(pattern, desc, re.I):
            continue
        unit = re.search(r'class="unit"[^>]*>(.*?)</span>', body, re.S)
        seller = re.search(r'class="media-user-name"[^>]*>(.*?)</div>', body, re.S)
        rows.append({"url": html.unescape(href.group(1)), "description": desc,
                     "display_price": float(price.group(1)),
                     "currency": clean(unit.group(1)) if unit else "UNKNOWN",
                     "seller": clean(seller.group(1)) if seller else "UNKNOWN",
                     "filters": dict(re.findall(r'(data-f-[\w-]+)="([^"]*)"', attrs))})
    rows.sort(key=lambda row: row["display_price"])
    return {"source": url, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "pattern": pattern, "matching_listings_in_returned_html": len(rows),
            "warning": "Asks only. Text match is NOT verified SKU equivalence. Not a complete market census.",
            "cheapest_matching_examples": rows[:max_examples]}


if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""), ensure_ascii=False, indent=2))
