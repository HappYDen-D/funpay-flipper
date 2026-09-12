"""Decimal outcome model. Monetary inputs are rubles; execution stores kopecks.

q is conditional on a sale after successful intake, u is intake failure.
No probability in this module is inferred from seller ratings or listing prices.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP


def decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('Invalid decimal input') from error
    if not result.is_finite():
        raise ValueError("Non-finite economic input")
    return result


def kopecks(value):
    amount = decimal(value)
    if amount < 0 or amount != amount.quantize(Decimal('.01')):
        raise ValueError("Expected nonnegative rubles with at most two decimals")
    return int(amount * 100)


def stored_kopecks(value):
    """Read a legacy SQLite REAL price; tolerate only sub-kopeck float noise.

    External quotes, cash proofs and user commands must still use kopecks().
    The 1e-8 RUB tolerance is a millionth of a kopeck, not ordinary price rounding.
    Strings/Decimals with extra fractional digits remain invalid.
    """
    if type(value) is not float:
        return kopecks(value)
    amount = decimal(value)
    if amount < 0:
        raise ValueError('Negative stored money')
    rounded = amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if abs(amount-rounded) > Decimal('0.00000001'):
        raise ValueError('Stored price has a material fractional kopeck')
    return kopecks(rounded)


def calculate(*, buy_price, net_receipt, operating_cost=0, sale_probability=.8,
              early_defect_rate=.04, early_recovery_rate=.5, salvage_rate=.6,
              late_defect_rate=.03, late_recovery_rate=0, late_fine=None,
              target_margin_rub=50, target_roi_rate=.2, intake_failure_rate=0,
              intake_recovery_rate=0, intake_failure_cost=0):
    B, N, c, m, rho, cu = map(decimal, (buy_price, net_receipt, operating_cost,
                                      target_margin_rub, target_roi_rate, intake_failure_cost))
    if B <= 0 or min(N, c, m, rho, cu) < 0:
        raise ValueError("Invalid amount or target")
    p, q, r, s, d, rl, u, ru = map(decimal, (
        sale_probability, early_defect_rate, early_recovery_rate, salvage_rate,
        late_defect_rate, late_recovery_rate, intake_failure_rate, intake_recovery_rate))
    if any(not 0 <= rate <= 1 for rate in (p, q, r, s, d, rl, u, ru)):
        raise ValueError("Probabilities/recovery rates must be within [0, 1]")
    F = N if late_fine is None else decimal(late_fine)
    if F < 0:
        raise ValueError("Negative refund debit")
    # EV = A - K*B - C, including intake as its own exclusive branch.
    A = (1-u)*p*(1-q)*(N-d*F)
    K = u*(1-ru)+(1-u)*(1-p*q*r-(1-p)*s-p*(1-q)*d*rl)
    C = u*cu+(1-u)*c
    ev = A-K*B-C
    bounds = []
    feasible = True
    for coefficient, rhs in ((K, A-C-m), (K+rho, A-C)):
        if coefficient > 0:
            bounds.append(rhs/coefficient)
        elif coefficient == 0 and rhs >= 0:
            continue
        else:
            feasible = False
    b_max = max(Decimal(0), min(bounds)) if feasible and bounds else Decimal(0)
    if not bounds:
        feasible = False  # Unbounded/degenerate proposals require manual model review.
    base = u*(ru*B-B-cu)+(1-u)*(s*B-B-c)
    slope = (1-u)*((1-q)*(N+d*(rl*B-F))+q*r*B-s*B)
    threshold = max(m, rho*B)
    if base >= threshold:
        p_min = Decimal(0)
    elif slope > 0:
        p_min = (threshold-base)/slope
    else:
        p_min = None
    meets = feasible and ev >= m and ev >= rho*B and B <= b_max
    return {
        'ev': float(ev.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
        'ev_rub': float(ev.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
        'ev_exact': str(ev),
        'b_max': float(b_max.quantize(Decimal('.01'), rounding=ROUND_FLOOR)),
        'p_min': float(p_min) if p_min is not None else None,
        'feasible': feasible and p_min is not None and p_min <= 1,
        'meets_targets': bool(meets),
        'a_coeff': float((1-u)*p*(1-q)*(1-d)),
        'k_coeff': float(K),
    }
