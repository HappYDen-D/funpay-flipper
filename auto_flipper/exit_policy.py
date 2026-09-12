"""Admission against a fresh, manually verified exit quote, never a sales forecast.

All money is RUB. ``unit_net_receipt`` is cash after all sale/withdrawal/FX
deductions; ``route_cost`` contains only additional costs, so fees are not counted
twice. A quote is evidence of demand, not an enforceable guarantee: cancellation,
asset defects and buyer/platform failure can still lose the full committed cash.
This pure module verifies the supplied record, not its external authenticity.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
import time


METHODS = {
    'trade_item': {'platform_trade', 'platform_transfer', 'manual'},
    'permanent_key': {'permanent_code', 'manual'},
    'account': {'account_credentials', 'platform_transfer', 'manual'},
}


def _number(value, field, reasons, *, money=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        reasons.append(field + ': expected a number, not a boolean or container')
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result <= 0):
            raise ValueError()
        if money and result != result.quantize(Decimal('.01')):
            raise ValueError()
    except (InvalidOperation, ValueError, OverflowError):
        reasons.append(field + ': invalid nonnegative number' + (' in kopecks' if money else ''))
        return None
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _quote(quote, sku, now, execution, reasons, prefix, buyer_identity):
    if not isinstance(quote, dict):
        reasons.append(prefix + ': explicit verified quote required')
        return None
    before = len(reasons)
    if type(quote.get('schema_version')) is not int or quote['schema_version'] != 1:
        reasons.append(prefix + '.schema_version: expected 1')
    for name in ('quote_id', 'buyer_id', 'source', 'sku'):
        if not _text(quote.get(name)):
            reasons.append(prefix + '.' + name + ': required nonempty text')
    if quote.get('sku') != sku:
        reasons.append(prefix + '.sku: must match the exact product SKU')
    if buyer_identity is not None and str(quote.get('buyer_id', '')).strip().casefold() == str(buyer_identity).strip().casefold():
        reasons.append(prefix + '.buyer_id: cannot be the purchasing operator')
    if type(quote.get('quantity')) is not int or quote['quantity'] < 1:
        reasons.append(prefix + '.quantity: positive integer capacity required')
    for name in ('transfer_verified', 'payout_verified', 'executable'):
        if quote.get(name) is not True:
            reasons.append(prefix + '.' + name + ': must be explicitly true')
    if quote.get('settlement_currency') != 'RUB':
        reasons.append(prefix + '.settlement_currency: actual cash settlement must be RUB')
    observed = _number(quote.get('observed_at'), prefix + '.observed_at', reasons, positive=True)
    expires = _number(quote.get('expires_at'), prefix + '.expires_at', reasons, positive=True)
    hours = _number(quote.get('settlement_hours'), prefix + '.settlement_hours', reasons)
    receipt = _number(quote.get('unit_net_receipt'), prefix + '.unit_net_receipt', reasons, money=True, positive=True)
    if now is not None and observed is not None and (observed > now or now-observed > 60):
        reasons.append(prefix + '.observed_at: quote must be observed within the last 60 seconds')
    if expires is not None and observed is not None and expires <= observed:
        reasons.append(prefix + '.expires_at: must follow observation')
    if expires is not None and now is not None and execution is not None and expires <= now+execution:
        reasons.append(prefix + '.expires_at: insufficient time to complete execution')
    if hours is not None and (hours > 72 or (execution is not None and hours+execution/3600 > 72)):
        reasons.append(prefix + '.settlement_hours: purchase-to-cash horizon exceeds 72 hours')
    if len(reasons) != before:
        return None
    return {'receipt': receipt, 'hours': hours, 'expires': expires}


def evaluate_exit(review, buy_price, *, now=None, min_profit=10, min_roi=.15,
                  buyer_identity=None):
    """Return {admitted, reasons, ...exact monetary strings}; malformed data rejects.

    Required review fields: sku, product, exit_quote, route_cost,
    estimated_execution_seconds. ``product`` requires kind, sku, expires_at=None,
    transfer_method, transfer_ready_at. Only one unit is admitted per invocation;
    the caller must atomically reserve quote capacity and capital, and recheck the
    quote at execution. ``fallback_quote`` is optional and must belong to a
    different buyer; a fallback remains conditional, not a guaranteed loss floor.

    Both profit >= min_profit and profit / purchase_price >= min_roi must hold.
    No budget ceiling or assumed sale probability is embedded in this module.
    """
    result = {'admitted': False, 'reasons': []}
    reasons = result['reasons']
    if not isinstance(review, dict):
        reasons.append('review: expected an object')
        return result
    try:
        with localcontext() as context:
            context.prec = 48
            now_value = _number(time.time() if now is None else now, 'now', reasons, positive=True)
            buy = _number(buy_price, 'buy_price', reasons, money=True, positive=True)
            cost = _number(review.get('route_cost'), 'route_cost', reasons, money=True)
            target = _number(min_profit, 'min_profit', reasons, money=True)
            roi = _number(min_roi, 'min_roi', reasons)
            execution = _number(review.get('estimated_execution_seconds'), 'estimated_execution_seconds', reasons, positive=True)
            sku = review.get('sku')
            if not _text(sku):
                reasons.append('sku: exact SKU required')
            if type(review.get('quantity', 1)) is not int or review.get('quantity', 1) != 1:
                reasons.append('quantity: only a single-unit purchase is supported')
            product = review.get('product')
            if not isinstance(product, dict):
                reasons.append('product: explicit product specification required')
            else:
                kind = product.get('kind')
                if not isinstance(kind, str) or kind not in METHODS:
                    reasons.append('product.kind: only trade_item, permanent_key or account supported')
                elif product.get('transfer_method') not in METHODS[kind]:
                    reasons.append('product.transfer_method: unsupported method for this product kind')
                if product.get('sku') != sku:
                    reasons.append('product.sku: must match the exact review SKU')
                if 'expires_at' not in product or product['expires_at'] is not None or product.get('is_subscription') not in (None, False):
                    reasons.append('product.expires_at: expiring products and subscriptions are excluded')
                ready = _number(product.get('transfer_ready_at'), 'product.transfer_ready_at', reasons)
                if ready is not None and now_value is not None and ready > now_value:
                    reasons.append('product.transfer_ready_at: product is still transfer-locked')
            primary = _quote(review.get('exit_quote'), sku, now_value, execution, reasons, 'exit_quote', buyer_identity)
            adverse = Decimal(0)
            if 'fallback_quote' in review:
                fallback = _quote(review['fallback_quote'], sku, now_value, execution, reasons, 'fallback_quote', buyer_identity)
                if fallback is not None and primary is not None:
                    if review['fallback_quote']['buyer_id'].strip().casefold() == review['exit_quote']['buyer_id'].strip().casefold():
                        reasons.append('fallback_quote.buyer_id: fallback must be an independent buyer')
                    if review['fallback_quote']['quote_id'] == review['exit_quote']['quote_id']:
                        reasons.append('fallback_quote.quote_id: fallback must be a separate quote')
                    adverse = min(primary['receipt'], fallback['receipt'])
            if 'adverse_exit_net' in review:
                supplied = _number(review['adverse_exit_net'], 'adverse_exit_net', reasons, money=True)
                if supplied is not None and supplied != adverse:
                    reasons.append('adverse_exit_net: must match the validated fallback quote or zero')
            if reasons:
                return result
            net = primary['receipt']
            profit = net-buy-cost
            cap = max(Decimal(0), min(net-cost-target, (net-cost)/(1+roi)))
            cap = cap.quantize(Decimal('.01'), rounding=ROUND_FLOOR)
            def money(value):
                return format(value.quantize(Decimal('.01')), 'f')
            result.update({
                'quote_id': review['exit_quote']['quote_id'], 'sku': sku,
                'net_quote': money(net), 'route_cost': money(cost),
                'net_profit': money(profit), 'b_max': money(cap),
                'adverse_exit_net': money(adverse),
                'downside': money(max(Decimal(0), buy+cost-adverse)),
                'full_cash_at_risk': money(buy+cost),
                'roi': str(profit/buy),
                'settlement_hours': str(primary['hours']),
                'cash_cycle_hours': str(primary['hours']+execution/3600),
                'quote_quantity': review['exit_quote']['quantity'],
                'quote_expires_at': str(primary['expires']),
            })
            if profit < target:
                reasons.append('net_profit: below minimum profit')
            if profit < roi*buy:
                reasons.append('roi: below minimum return on purchase price')
            if buy > cap:
                reasons.append('buy_price: above conservative purchase cap')
            result['admitted'] = not reasons
    except (ArithmeticError, ValueError, TypeError) as error:
        # User-supplied huge or malformed numeric values must fail closed too.
        result['admitted'] = False
        reasons.append('invalid arithmetic or malformed input: ' + type(error).__name__)
    return result
