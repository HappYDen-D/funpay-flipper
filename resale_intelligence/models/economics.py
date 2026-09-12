"""Offline economics: integer kopecks, explicit price basis, no assumed marketplace fee."""
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite

def cents(value):
    if not isfinite(value):
        raise ValueError("Nonfinite amount")
    return int((Decimal(str(value))*100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def rub(value):
    return value/100

def fraction(amount, rate):
    if not isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("Rate must be in [0,1]")
    return int((Decimal(amount)*Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def seller_receipt(price, basis, fee_rate=None):
    if price < 0:
        raise ValueError("Negative price")
    if basis == "seller_net":
        return price
    if basis == "buyer_gross" and fee_rate is not None:
        return price-fraction(price,fee_rate)
    raise ValueError("Explicit seller_net or buyer_gross with calibrated fee required")

def expected_profit(buy, receipt, defect, recovery=0, operating_cost=0, sale_probability=1, salvage=0):
    """Horizon EV in kopecks. Recovery/salvage are fractions of buy."""
    for rate in (defect,recovery,sale_probability,salvage):
        fraction(0,rate)
    if buy<=0 or receipt<0 or operating_cost<0:
        raise ValueError("Invalid economics")
    return (sale_probability*((1-defect)*receipt+defect*recovery*buy)
            +(1-sale_probability)*salvage*buy-buy-operating_cost)

def reserve_required(equity, minimum=5000, ratio=.15):
    return max(minimum,fraction(max(0,equity),ratio))
