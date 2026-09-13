"""Snapshot integrity primitives for read-only account market scans."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, List, Optional


class SnapshotQuality(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AccountMarketSnapshot:
    market_id: str
    node_id: int
    lots: List[Dict[str, Any]]
    advertised_market_count: Optional[int]
    parsed_count: int
    coverage_ratio: Optional[float]
    fetch_success: bool
    parse_success: bool
    quality: SnapshotQuality
    scan_duration: float
    http_status: Optional[int] = None
    failure_reason: str = ""

    def with_quality(self, quality: SnapshotQuality, reason: str = "") -> "AccountMarketSnapshot":
        return replace(self, quality=quality, failure_reason=reason or self.failure_reason)

    def as_diagnostic(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "node_id": self.node_id,
            "advertised_market_count": self.advertised_market_count,
            "parsed_count": self.parsed_count,
            "coverage_ratio": self.coverage_ratio,
            "fetch_success": self.fetch_success,
            "parse_success": self.parse_success,
            "quality": self.quality.value,
            "scan_duration": self.scan_duration,
            "http_status": self.http_status,
            "failure_reason": self.failure_reason,
        }


def classify_snapshot_quality(
    *, advertised_market_count: Optional[int], parsed_count: int,
    fetch_success: bool, parse_success: bool,
    previous_complete_count: Optional[int] = None,
    minimum_coverage_ratio: float = 0.98,
    minimum_previous_ratio: float = 0.60,
) -> tuple[SnapshotQuality, Optional[float], str]:
    """Fail closed on transport/parser failures, known truncation, or collapse."""
    if not fetch_success:
        return SnapshotQuality.FAILED, None, "fetch_failed"
    if not parse_success:
        return SnapshotQuality.FAILED, None, "parse_failed"
    coverage = None
    if advertised_market_count is not None:
        coverage = 1.0 if advertised_market_count == 0 else parsed_count / max(1, advertised_market_count)
        if coverage < minimum_coverage_ratio:
            return SnapshotQuality.PARTIAL, coverage, "advertised_count_not_covered"
    if previous_complete_count and parsed_count < previous_complete_count * minimum_previous_ratio:
        return SnapshotQuality.PARTIAL, coverage, "sudden_parsed_count_collapse"
    return SnapshotQuality.COMPLETE, coverage, ""
