"""
test_live_node1808_smoke.py — Optional read-only live market smoke test for FunPay node 1808.

Performs strictly read-only GET requests:
- Zero POST requests.
- No purchases or checkout interactions.
- Extracts live FunPay TF2 listings, classifies exact benchmark SKUs,
  and evaluates live liquidity metrics and reference prices.
"""
import asyncio
import sys
import tempfile
import time

from auto_flipper.database import Database
from auto_flipper.funpay_client import FunPayClient
from auto_flipper.liquidity import evaluate_sku_liquidity
from auto_flipper.sku_matcher import (
    BENCHMARK_SKUS,
    BENCHMARK_SKU_NAMES,
    SKU_TF2_KEY,
    SKU_TF2_TICKET,
    SKU_UNKNOWN,
    match_sku,
)


async def _fetch_lots():
    client = FunPayClient()
    return await client.fetch_market_lots([1808])


def run_live_smoke():
    print("=" * 60)
    print("LIVE SMOKE: Read-Only GET FunPay Node 1808 (TF2 Items)")
    print("=" * 60)

    t0 = time.time()
    try:
        lots = asyncio.run(_fetch_lots())
        elapsed = time.time() - t0
        print(f"GET https://funpay.com/lots/1808/ fetched {len(lots)} active lots in {elapsed:.2f}s")
    except Exception as e:
        print(f"SKIPPED: Could not fetch live page (network/proxy error): {e}")
        return 0
    print(f"Parsed total listings on node 1808: {len(lots)}")

    classified = {SKU_TF2_KEY: [], SKU_TF2_TICKET: [], SKU_UNKNOWN: []}
    for lot in lots:
        sku = match_sku(lot.get("node_id", 1808), lot.get("title", ""))
        classified.setdefault(sku, []).append(lot)

    print(f" - Mann Co. Keys (tf2:5021;6): {len(classified[SKU_TF2_KEY])} lots")
    print(f" - Tour of Duty Tickets (tf2:725;6): {len(classified[SKU_TF2_TICKET])} lots")
    print(f" - Other / Excluded / UNKNOWN: {len(classified[SKU_UNKNOWN])} lots")

    # Ingest into a fresh temporary database to test real end-to-end evaluation
    with tempfile.TemporaryDirectory(prefix="live-smoke-mkt-") as tmp_dir:
        db = Database(f"{tmp_dir}/live_smoke.db")
        summary = db.record_market_observation(lots, force_sample=True)
        print(f"\nObservation recorded to market history: {summary}")

        print("\n" + "=" * 60)
        print("EVALUATED LIVE MARKET BENCHMARKS:")
        print("=" * 60)

        for sku in BENCHMARK_SKUS:
            name = BENCHMARK_SKU_NAMES.get(sku, sku)
            assessment = evaluate_sku_liquidity(db, sku)
            if not assessment:
                print(f"\n[{name}] No active market observed.")
                continue

            m = assessment.metrics
            ref = db.get_sku_reference_price(sku)

            print(f"\n[BENCHMARK] {name} ({sku})")
            print(f"   Score: {assessment.score:.1f}/100 | Confidence: {assessment.confidence}")
            print(f"   Active listings: {m.active_listings} | Unique sellers: {m.unique_sellers} | Observed stock: {m.observed_stock}")
            print(f"   Prices: Min={m.min_price:.2f} RUB | P25={m.p25_price:.2f} RUB | Median={m.p50_price:.2f} RUB | P90={m.p90_price:.2f} RUB | Max={m.max_price:.2f} RUB")
            print(f"   Price dispersion: {m.price_dispersion:.2%} | MAD: {ref['mad']:.2f} RUB")
            print(f"   Turnover proxy: {m.turnover_proxy:.1f}/h | Cleanliness: {assessment.sub_scores['reappearance']:.1f}/100")
            print(f"   Sub-scores: {assessment.sub_scores}")

    print("\n[OK] Live smoke test completed with ZERO POST requests.")
    return 0


if __name__ == "__main__":
    sys.exit(run_live_smoke())
