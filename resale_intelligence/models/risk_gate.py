"""Offline pre-purchase gate. No I/O; not an atomic reservation or execution system.

Amounts are integer kopecks. Snapshot must be supplied by a reconciled ledger.
Liabilities already deducted from cash MUST NOT be included a second time.
"""
from dataclasses import dataclass
from math import isfinite

try:
    from .economics import fraction, reserve_required
except ImportError:
    from economics import fraction, reserve_required


@dataclass(frozen=True)
class RiskPolicy:
    minimum_reserve: int = 5000
    reserve_ratio: float = .15
    lot_cap: float = .15
    supplier_cap: float = .25
    category_cap: float = .40
    loss_stop_ratio: float = .10  # measured from fixed session opening equity
    max_snapshot_age_seconds: int = 60


@dataclass(frozen=True)
class RiskSnapshot:
    available_cash: int
    conservative_equity: int  # equity after markdowns AND liabilities
    session_opening_equity: int
    session_loss: int  # max(0, opening equity - current equity), deposit adjusted
    pending_purchase_debits: int = 0
    refund_liabilities: int = 0  # not yet debited from available_cash
    operating_buffer: int = 0
    supplier_exposure: int = 0  # includes pending intentions and open recovery risk
    category_exposure: int = 0
    snapshot_age_seconds: float = float('inf')
    balance_verified: bool = False
    purchasing_available: bool = False
    reconciliation_ok: bool = False
    unresolved_purchase: bool = False
    emergency_stopped: bool = False
    supplier_quarantined: bool = False


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    reserve: int
    purchase_budget: int
    cash_after_commitments: int


def assess_purchase(buy: int, state: RiskSnapshot,
                    policy: RiskPolicy = RiskPolicy()) -> RiskDecision:
    """Fail closed. Caller also needs economic/SKU approval, then atomic recheck."""
    money = (buy, state.available_cash, state.conservative_equity,
             state.session_opening_equity, state.session_loss,
             state.pending_purchase_debits, state.refund_liabilities,
             state.operating_buffer, state.supplier_exposure,
             state.category_exposure, policy.minimum_reserve)
    if any(type(v) is not int or v < 0 for v in money) or buy == 0 or state.session_opening_equity == 0:
        return RiskDecision(False, ('INVALID_AMOUNT',), 0, 0, 0)
    rates = (policy.reserve_ratio, policy.lot_cap, policy.supplier_cap,
             policy.category_cap, policy.loss_stop_ratio)
    if (any(not isfinite(v) or not 0 < v <= 1 for v in rates)
            or not isfinite(policy.max_snapshot_age_seconds) or policy.max_snapshot_age_seconds <= 0):
        return RiskDecision(False, ('INVALID_POLICY',), 0, 0, 0)
    reserve = reserve_required(state.conservative_equity, policy.minimum_reserve, policy.reserve_ratio)
    cash = (state.available_cash - state.pending_purchase_debits
            - state.refund_liabilities - state.operating_buffer)
    budget = max(0, min(cash-reserve,
                        fraction(state.conservative_equity, policy.lot_cap),
                        fraction(state.conservative_equity, policy.supplier_cap)-state.supplier_exposure,
                        fraction(state.conservative_equity, policy.category_cap)-state.category_exposure))
    reasons = []
    checks = (
        (state.emergency_stopped, 'EMERGENCY_STOP'),
        (not state.balance_verified, 'BALANCE_UNKNOWN'),
        (not state.purchasing_available, 'BALANCE_PURCHASE_BLOCKED'),
        (not state.reconciliation_ok, 'RECONCILIATION_REQUIRED'),
        (not isfinite(state.snapshot_age_seconds) or not 0 <= state.snapshot_age_seconds <= policy.max_snapshot_age_seconds,
         'STALE_SNAPSHOT'),
        (state.unresolved_purchase, 'PURCHASE_OUTCOME_UNKNOWN'),
        (state.supplier_quarantined, 'SUPPLIER_QUARANTINED'),
        (state.session_loss >= fraction(state.session_opening_equity, policy.loss_stop_ratio), 'LOSS_LIMIT'),
        (buy > budget, 'EXPOSURE_OR_LIQUIDITY_LIMIT'),
    )
    reasons.extend(code for condition, code in checks if condition)
    return RiskDecision(not reasons, tuple(reasons), reserve, budget, cash)


def delayed_refund_effect(receipt: int, buyer_refund_debit: int, supplier_recovery: int = 0) -> dict:
    """Additional PnL after an already-recognized sale; no second write-off of buy.

    Use actual marketplace debits/net fee adjustments, NOT automatically gross buyer price.
    Recovery is realized only when received. This function merely does arithmetic.
    """
    if any(type(v) is not int or v < 0 for v in (receipt, buyer_refund_debit, supplier_recovery)):
        raise ValueError('Expected nonnegative integer kopecks')
    return {'pnl_adjustment': supplier_recovery-buyer_refund_debit,
            'remaining_receipt': receipt-buyer_refund_debit+supplier_recovery}
