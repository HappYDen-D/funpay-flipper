"""Exact small-basket allocation of supplied assumptions, not a market recommendation."""
from dataclasses import dataclass
from itertools import product
import json
try:
    from .economics import cents,rub,fraction,expected_profit,reserve_required
except ImportError:
    from economics import cents,rub,fraction,expected_profit,reserve_required

@dataclass(frozen=True)
class AllocationCandidate:
    sku: str
    category: str
    seller: str
    buy: int
    receipt: int
    max_units: int
    defect: float = .05
    recovery: float = 0
    sale_probability: float = .8
    operating_cost: int = 0
    verified: bool = False

def allocate(candidates,cash,equity,category_cap=.4,seller_cap=.25,single_cap=.15,
             allow_unverified=False,category_exposure=None,seller_exposure=None):
    """Money is kopecks. Up to 1m combinations. Existing exposure included."""
    if cash<0 or equity<0:
        raise ValueError("Negative cash/equity")
    if len({c.sku for c in candidates})!=len(candidates):
        raise ValueError("Aggregate duplicate SKU first")
    ec,es=dict(category_exposure or {}),dict(seller_exposure or {})
    if any(v<0 for v in (*ec.values(),*es.values())):
        raise ValueError("Negative exposure")
    cl,sl,ll=[fraction(equity,v) for v in (category_cap,seller_cap,single_cap)]
    reserve=reserve_required(equity)
    spendable=max(0,cash-reserve)
    ranges,values,combinations=[],[],1
    for c in candidates:
        if c.buy<=0 or not isinstance(c.max_units,int) or c.max_units<0:
            raise ValueError("Invalid candidate")
        ev=expected_profit(c.buy,c.receipt,c.defect,c.recovery,c.operating_cost,c.sale_probability)
        cap=min(c.max_units,spendable//c.buy)
        if c.buy>ll or ev<=0 or not(c.verified or allow_unverified):
            cap=0
        combinations*=cap+1
        if combinations>1_000_000:
            raise ValueError("Pilot allocator supports at most 1m combinations")
        ranges.append(range(cap+1))
        values.append(ev)
    best_score,best_spend,best_qty=0.,0,tuple(0 for _ in candidates)
    for quantities in product(*ranges):
        spend=sum(q*c.buy for q,c in zip(quantities,candidates))
        if spend>spendable:
            continue
        cats,sellers=dict(ec),dict(es)
        for q,c in zip(quantities,candidates):
            cats[c.category]=cats.get(c.category,0)+q*c.buy
            sellers[c.seller]=sellers.get(c.seller,0)+q*c.buy
        if any(v>cl for v in cats.values()) or any(v>sl for v in sellers.values()):
            continue
        score=sum(q*v for q,v in zip(quantities,values))
        if score>best_score or (score==best_score and spend<best_spend):
            best_score,best_spend,best_qty=score,spend,quantities
    return dict(quantities=dict(zip((c.sku for c in candidates),best_qty)),spent=rub(best_spend),
                cash_remaining=rub(cash-best_spend),reserve_required=rub(reserve),
                expected_profit=round(best_score/100,2),assumption_only=allow_unverified)

def legacy_comparison():
    buy=300
    receipt=4*78.32+4*131.12+3*148.72
    return dict(buy=buy,receipt=round(receipt,2),profit=round(receipt-buy,2),
                roi_pct=round((receipt-buy)/buy*100,2),reserve_at_350=rub(reserve_required(cents(350))),
                old_basket_cash=50,reserve_shortfall=2.5,legacy_single_profit=256.12,
                warning="Original hypothetical prices and 12% assumption, not executable quotes")

if __name__=="__main__":
    print(json.dumps(legacy_comparison(),indent=2))
