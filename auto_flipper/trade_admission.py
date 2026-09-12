"""Shared preflight and transactional cash gate. Values in integer kopecks."""
from decimal import Decimal, ROUND_FLOOR
from auto_flipper.economics import kopecks
from resale_intelligence.models.risk_gate import RiskSnapshot, RiskPolicy, assess_purchase

PILOT_POLICY = RiskPolicy(reserve_ratio=.40)


def check_capital(db, dry_run, review, category_id, price, *, conn=None, available_cash=None, emergency_stopped=None):
    state = db.capital_snapshot(dry_run, supplier_group=review['supplier_group'], category_id=category_id, conn=conn, emergency_stopped=emergency_stopped)
    if available_cash is not None:
        state['available_cash'] = min(state['available_cash'], available_cash)
    cost = kopecks(review['route_cost'])
    # Future route costs use cash too; the full outlay must fit every limit.
    decision = assess_purchase(kopecks(price) + cost, RiskSnapshot(**state), PILOT_POLICY)
    if not decision.allowed:
        raise ValueError(', '.join(decision.reasons))
    status = db.capital_status(dry_run, conn=conn, emergency_stopped=emergency_stopped)
    risk_limit = int((Decimal(status['realized_capital']) * Decimal('.10')).to_integral_value(rounding=ROUND_FLOOR))
    if status['open_risk'] + kopecks(price) + cost > max(0, risk_limit - state['session_loss']):
        raise ValueError('TOTAL_OPEN_LOSS_LIMIT')
    return decision
