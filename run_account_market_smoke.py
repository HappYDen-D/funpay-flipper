"""One-shot GET-only smoke test for the three allowlisted account markets."""
import asyncio
import json
import os
import tempfile
import time

from auto_flipper.account_market_observer import AccountMarketObserver, top_cohorts
from auto_flipper.config import ACCOUNT_MARKET_SEEDS
from auto_flipper.database import Database
from auto_flipper.funpay_client import FunPayClient


async def main():
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="funpay-account-smoke-") as root:
        db = Database(os.path.join(root, "smoke.db"))
        client = FunPayClient()
        observer = AccountMarketObserver(client, db)
        reports = []
        for market_id, seed in ACCOUNT_MARKET_SEEDS.items():
            result = await observer.poll_market(market_id)
            assessment = result["assessment"]
            metrics = assessment.metrics
            reports.append({
                "market_id": market_id,
                "name": seed["name"],
                "node_id": seed["node_id"],
                "advertised_market_count": metrics.advertised_market_count,
                "lots_parsed": metrics.active_lots,
                "coverage_ratio": metrics.coverage_ratio,
                "snapshot_quality": metrics.snapshot_quality,
                "scan_duration": metrics.scan_duration,
                "sellers": metrics.independent_sellers,
                "cheap_lots_rub": metrics.cheap_lots,
                "classified": result["classified"],
                "unclassified": result["lots_observed"] - result["classified"],
                "top_cohorts": top_cohorts(db, market_id),
                "mops": assessment.mops,
                "observed_risk": assessment.risk_score,
                "risk_evidence_coverage": assessment.risk_evidence.coverage_ratio,
                "risk_evidence_confidence": assessment.risk_evidence.confidence,
                "confidence": assessment.confidence,
                "eligible": assessment.eligible,
            })
        print(json.dumps({
            "started_at_unix": started,
            "finished_at_unix": time.time(),
            "requests": [f"GET https://funpay.com/lots/{seed['node_id']}/" for seed in ACCOUNT_MARKET_SEEDS.values()],
            "post_count": 0,
            "markets": reports,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
