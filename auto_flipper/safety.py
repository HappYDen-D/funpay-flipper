"""Explicit evidence and action policy; defaults do not authorize live spending."""
from auto_flipper.economics import calculate, kopecks
from auto_flipper.exit_policy import evaluate_exit

EXCLUDED_CATEGORIES = frozenset({'chatgpt', 'discord', 'cursor', 'exitlag', 'tg_premium'})

MODEL_FIELDS = {'operating_cost', 'sale_probability', 'early_defect_rate',
                'early_recovery_rate', 'salvage_rate', 'late_defect_rate',
                'late_recovery_rate', 'late_fine', 'intake_failure_rate',
                'intake_recovery_rate', 'intake_failure_cost'}
REQUIRED_MODEL_FIELDS = MODEL_FIELDS - {'late_fine'}


def is_account_observation_candidate(value):
    """Return True for any payload/category originating in account observation."""
    if not isinstance(value, dict):
        return str(value).startswith("funpay:account:") or str(value) == "ACCOUNT_OBSERVATION"
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
    return (
        str(payload.get("source_type", "")).upper() == "ACCOUNT_OBSERVATION"
        or str(payload.get("market_id", "")).startswith("funpay:account:")
        or str(payload.get("canonical_sku", "")).startswith("account:")
        or str(payload.get("category_id", "")).startswith("funpay:account:")
        or payload.get("purchase_eligible") is False
    )


def reject_account_observation_purchase(value):
    if is_account_observation_candidate(value):
        raise PermissionError(
            "ACCOUNT_OBSERVATION_NOT_PURCHASABLE: account-market observations are permanently read-only"
        )


def validate_review(review):
    if not isinstance(review, dict):
        raise ValueError('Review must be an object')
    for field in ('sku', 'source', 'supplier_group'):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError('Missing '+field)
    # Validate evidence independently of a particular purchase price/profit hurdle.
    result = evaluate_exit(review, '.01', min_profit=0, min_roi=0)
    structural = [r for r in result['reasons'] if not r.startswith(('net_profit:', 'roi:', 'buy_price:'))]
    if structural:
        raise ValueError('NEEDS_VERIFIED_DEMAND: ' + '; '.join(structural))
    if review.get('purchase_currency') != 'RUB':
        raise ValueError('Purchase debit must be verified in RUB')
    # The listing price is NOT the all-fees net quote. Never infer one from the other.
    if 'listing_price' in review and kopecks(review['listing_price']) <= 0:
        raise ValueError('Invalid listing price')


def action_allowed(mode, stopped, action):
    if action == 'deactivate':
        return stopped or mode == 'PAUSED'
    if stopped or mode not in ('ASSIST', 'LIMITED_AUTO'):
        return False
    return action in ('checkout', 'publish', 'deliver', 'boost')
