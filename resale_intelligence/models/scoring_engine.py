"""Offline decision support. A trust index is NOT a probability of successful delivery."""
import math
from dataclasses import dataclass, field
from statistics import median
try:
    from .economics import cents, rub, seller_receipt, expected_profit
except ImportError:
    from economics import cents, rub, seller_receipt, expected_profit

@dataclass
class CandidateLot:
    lot_id: str
    category_id: str
    title: str
    price: float  # actual buyer debit, RUB
    seller: str
    seller_rating: float
    seller_reviews: int
    seller_online: bool = True
    is_personal: bool = True
    is_valid_type: bool = True
    sku_key: str = ''
    quote_verified: bool = False
    resale_status: str = 'unknown'  # allowed / unknown / restricted

@dataclass
class MarketContext:
    category_id: str
    median_price: float  # same currency AND basis as rival prices
    lowest_reputable_price: float | None
    price_floor: float
    markup_discount: float
    min_profit: float
    min_margin_pct: float
    min_seller_rating: float
    min_seller_reviews: int
    min_anomaly_ratio: float = .15
    blacklisted_keywords: list[str] = field(default_factory=list)
    sku_key: str = ''
    sample_count: int = 0
    seller_count: int = 0
    quote_age_minutes: float = math.inf
    price_basis: str = 'unknown'
    fee_rate: float | None = None
    economics_verified: bool = False
    defect_upper_bound: float = .10
    sale_probability: float = .70
    recovery_fraction: float = 0
    cost_per_trade: float = 0
    max_buy_price: float = 0
    spendable_cash: float = 0

@dataclass
class EvaluationReport:
    lot_id: str
    category_id: str
    is_eligible: bool  # True ONLY for AUTO_BUY
    decision: str
    seller_trust_score: float
    anomaly_status: str
    buy_price: float
    suggested_sell_price: float
    net_revenue: float
    expected_profit: float
    margin_pct: float
    rejection_reasons: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)

def comparable_market(observations, sku_key):
    """One price per seller. Caller pre-normalizes basis, currency and freshness."""
    sellers = {}
    for row in observations:
        price, seller = row.get('price'), row.get('seller')
        if (row.get('sku_key') == sku_key and seller and isinstance(price,(int,float))
                and math.isfinite(price) and price > 0):
            sellers.setdefault(seller,[]).append(price)
    prices = [median(v) for v in sellers.values()]
    return dict(median=median(prices) if prices else None,
                sample_count=sum(len(v) for v in sellers.values()),seller_count=len(prices))

class ScoringEngine:
    @staticmethod
    def calculate_seller_trust(rating, reviews, min_rating=4.5, min_reviews=3):
        if (not math.isfinite(rating) or not 0<=rating<=5 or not 0<min_rating<=5
                or not isinstance(reviews,int) or reviews<0):
            raise ValueError('Invalid seller data')
        if rating<min_rating:
            rs=rating/min_rating*20
        elif min_rating==5:
            rs=50.
        else:
            rs=20+(rating-min_rating)/(5-min_rating)*30
        vs=min(50.,math.log1p(reviews)/math.log(51)*50)
        score=round(min(100.,max(0.,rs+vs)),1)
        return score,dict(rating_score=rs,review_score=vs,minimum_reviews_met=reviews>=min_reviews,
                          interpretation='heuristic index, not probability')

    @staticmethod
    def detect_anomaly(buy_price,median_price,min_ratio=.15):
        if not all(math.isfinite(x) and x>0 for x in (buy_price,median_price)):
            return 'UNKNOWN','Invalid or missing prices'
        ratio=buy_price/median_price
        if ratio<min_ratio:
            return 'SUSPICIOUS_LOW','Check SKU and provenance; low price does not prove fraud'
        return ('DISCOUNT' if ratio<=.70 else 'NEAR_MARKET'),'Economics evaluated separately'

    @staticmethod
    def calculate_undercut_price(median_price,lowest_reputable,price_floor,markup_discount):
        if (not math.isfinite(median_price) or median_price<=0 or not 0<markup_discount<=1
                or not math.isfinite(price_floor) or price_floor<0):
            raise ValueError('Invalid pricing inputs')
        target=cents(median_price*markup_discount)
        if lowest_reputable is not None:
            if not math.isfinite(lowest_reputable) or lowest_reputable<=0:
                raise ValueError('Invalid competitor quote')
            target=min(target,cents(lowest_reputable-(10 if lowest_reputable>200 else 2)))
        if target<=0 or target<cents(price_floor):
            return 0.,'NO_COMPETITIVE_PRICE_ABOVE_FLOOR'
        return rub(target),'Comparable quote undercut; sale not guaranteed'

    @classmethod
    def evaluate_lot(cls,lot,ctx):
        reject,review,score,sell,receipt,ev,roi=[],[],0.,0.,0.,0.,0.
        anomaly,note='UNKNOWN',''
        try:
            if lot.category_id!=ctx.category_id or not lot.is_valid_type:
                reject.append('SKU/category mismatch')
            if not lot.is_personal or lot.resale_status=='restricted':
                reject.append('Product transfer conditions not met')
            if any(kw.casefold() in lot.title.casefold() for kw in ctx.blacklisted_keywords):
                review.append('Title flag: review context and negation')
            if not math.isfinite(lot.price) or lot.price<=0:
                raise ValueError('Nonpositive or nonfinite purchase price')
            for value in (ctx.min_profit,ctx.min_margin_pct,ctx.cost_per_trade,
                          ctx.max_buy_price,ctx.spendable_cash):
                if not math.isfinite(value) or value<0:
                    raise ValueError('Invalid economic threshold')
            if not 0<ctx.min_anomaly_ratio<1 or ctx.min_seller_reviews<0:
                raise ValueError('Invalid seller/anomaly threshold')
            score,_=cls.calculate_seller_trust(lot.seller_rating,lot.seller_reviews,
                                              ctx.min_seller_rating,ctx.min_seller_reviews)
            if lot.seller_rating<ctx.min_seller_rating or lot.seller_reviews<ctx.min_seller_reviews:
                reject.append('Seller minimum not met')
            if score<70:
                review.append('Insufficient trust index for automatic decision')
            if not lot.sku_key or lot.sku_key!=ctx.sku_key:
                review.append('Exact SKU comparison missing')
            if not lot.quote_verified or not ctx.economics_verified or lot.resale_status!='allowed':
                review.append('Executable quote, fee or transfer evidence missing')
            if ctx.sample_count<20 or ctx.seller_count<5 or not 0<=ctx.quote_age_minutes<=30:
                review.append('Insufficient or stale comparable sample')
            if lot.price>ctx.max_buy_price or lot.price>ctx.spendable_cash:
                reject.append('Purchase/exposure/cash budget exceeded')
            if ctx.price_basis=='buyer_gross':
                anomaly,note=cls.detect_anomaly(lot.price,ctx.median_price,ctx.min_anomaly_ratio)
                if anomaly=='SUSPICIOUS_LOW':
                    review.append(note)
            else:
                note='Ratio omitted: buyer debit and seller-net quote have different bases'
            sell,reason=cls.calculate_undercut_price(ctx.median_price,ctx.lowest_reputable_price,
                                                    ctx.price_floor,ctx.markup_discount)
            if sell<=0:
                reject.append(reason)
            if ctx.price_basis=='unknown':
                review.append('Price basis unknown; commission not assumed')
            else:
                receipt=rub(seller_receipt(cents(sell),ctx.price_basis,ctx.fee_rate))
                ev=expected_profit(cents(lot.price),cents(receipt),ctx.defect_upper_bound,
                                   ctx.recovery_fraction,cents(ctx.cost_per_trade),
                                   ctx.sale_probability)/100
                roi=ev/lot.price*100
                if ev<=0 or ev<ctx.min_profit or roi<ctx.min_margin_pct:
                    reject.append('Risk-adjusted profit AND ROI requirements not met')
        except (ValueError,TypeError,OverflowError) as exc:
            reject.append(str(exc))
        decision='REJECTED' if reject else ('MANUAL_REVIEW' if review else 'AUTO_BUY')
        return EvaluationReport(lot.lot_id,lot.category_id,decision=='AUTO_BUY',decision,score,
                                anomaly,lot.price,sell,receipt,round(ev,2),round(roi,2),reject,
                                dict(review_reasons=review,anomaly_note=note,price_basis=ctx.price_basis))
