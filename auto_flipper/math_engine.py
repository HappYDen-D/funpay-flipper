"""
auto_flipper/math_engine.py — Mathematical pricing model, commission calculation, and deal evaluation
"""
from dataclasses import dataclass, field

from typing import Any, Dict, List, Optional, Tuple

from auto_flipper.config import (
    ARBITRAGE_MARKUP_DISCOUNT,
    ARBITRAGE_MAX_BUDGET_DEFAULT,
    ARBITRAGE_MIN_MARGIN_PCT,
    ARBITRAGE_MIN_PROFIT,
    ARBITRAGE_MIN_SELLER_RATING,
    ARBITRAGE_MIN_SELLER_REVIEWS,
    ARBITRAGE_PRICE_FLOOR,
    FUNPAY_DIGITAL_FEE_RATE,
)


@dataclass
class ArbitrageEvaluation:
    """Evaluation result for a candidate deal for auto-flipping."""
    is_eligible: bool
    rejection_reasons: List[str] = field(default_factory=list)
    category_id: str = ""
    buy_price: float = 0.0
    sell_price: float = 0.0
    expected_revenue: float = 0.0
    expected_profit: float = 0.0
    margin_pct: float = 0.0
    roi_pct: float = 0.0
    platform_fee: float = 0.0
    pricing_source: str = ""
    b_max: float = 0.0
    p_min: Optional[float] = None
    ev_rub: float = 0.0
    exit_metrics: Dict[str, Any] = field(default_factory=dict)


class ArbitrageMath:
    """
    Mathematical pricing and profit model for high-frequency resale arbitrage.
    """

    @staticmethod
    def calculate_ev_and_limits(
        buy_price: float,
        net_receipt: float,
        operating_cost: float = 0.0,
        sale_probability: float = 0.80,
        early_defect_rate: float = 0.04,
        early_recovery_rate: float = 0.50,
        salvage_rate: float = 0.60,
        late_defect_rate: float = 0.03,
        late_recovery_rate: float = 0.0,
        late_fine: Optional[float] = None,
        target_margin_rub: float = 50.0,
        target_roi_rate: float = 0.20,
        intake_failure_rate: float = 0.0,
        intake_recovery_rate: float = 0.0,
        intake_failure_cost: float = 0.0,
    ) -> Dict[str, Any]:
        parameters = locals().copy()
        from auto_flipper.economics import calculate
        return calculate(**parameters)

    @staticmethod
    def calculate_sell_price(
        market_median: float,
        lowest_reputable_price: Optional[float] = None,
        markup_discount: float = ARBITRAGE_MARKUP_DISCOUNT,
        price_floor: float = ARBITRAGE_PRICE_FLOOR,
    ) -> float:
        """
        Calculates dynamic resale price:
        P_sell = round(market_median * markup_discount)
        If competitive reputable price is known, undercut by 10 RUB as a price proposal; turnover is not guaranteed.
        Ensures P_sell is never below price_floor.
        """
        target_price = round(market_median * markup_discount)
        if lowest_reputable_price and lowest_reputable_price > price_floor:
            undercut_price = round(lowest_reputable_price - 10.0)
            target_price = min(target_price, undercut_price)

        return max(float(target_price), float(price_floor))

    @staticmethod
    def calculate_net_revenue(sell_price: float, fee_rate: float = FUNPAY_DIGITAL_FEE_RATE) -> float:
        """Net revenue received by seller after FunPay digital fee deduction (12%)."""
        return round(sell_price * (1.0 - fee_rate), 2)

    @staticmethod
    def calculate_profit_and_margin(
        buy_price: float,
        sell_price: float,
        fee_rate: float = FUNPAY_DIGITAL_FEE_RATE,
    ) -> Tuple[float, float, float]:
        """
        Calculates (net_revenue, net_profit, margin_pct).
        R_net = P_sell * (1 - fee_rate)
        Pi = R_net - P_buy
        Margin% = (Pi / P_buy) * 100%
        """
        revenue = round(sell_price * (1.0 - fee_rate), 2)
        profit = round(revenue - buy_price, 2)
        margin_pct = round((profit / buy_price) * 100.0, 2) if buy_price > 0 else 0.0
        return revenue, profit, margin_pct

    @classmethod
    def evaluate_deal(
        cls,
        title: str,
        price: float,
        seller: str,
        seller_rating: float,
        seller_reviews: int,
        market_median: float,
        is_personal: bool = True,
        is_plus: bool = True,
        max_budget: float = ARBITRAGE_MAX_BUDGET_DEFAULT,
        min_profit: float = ARBITRAGE_MIN_PROFIT,
        min_margin_pct: float = ARBITRAGE_MIN_MARGIN_PCT,
        min_seller_rating: float = ARBITRAGE_MIN_SELLER_RATING,
        min_seller_reviews: int = ARBITRAGE_MIN_SELLER_REVIEWS,
        lowest_reputable_price: Optional[float] = None,
        markup_discount: float = ARBITRAGE_MARKUP_DISCOUNT,
        price_floor: float = ARBITRAGE_PRICE_FLOOR,
        fee_rate: float = FUNPAY_DIGITAL_FEE_RATE,
    ) -> ArbitrageEvaluation:
        """
        Evaluates an underpriced candidate lot against safety, budget, and profit criteria.
        Requires BOTH minimum profit in RUB AND minimum margin percentage (AND, not OR).
        """
        rejection_reasons: List[str] = []

        if not is_personal:
            rejection_reasons.append("Лот не является строго личным аккаунтом")
        if not is_plus:
            rejection_reasons.append("Лот не содержит подтвержденную подписку Plus")
        if price > max_budget:
            rejection_reasons.append(f"Цена {price} ₽ превышает бюджет {max_budget} ₽")

        if seller_rating < min_seller_rating:
            rejection_reasons.append(
                f"Рейтинг продавца ({seller_rating:.1f}) ниже минимального {min_seller_rating:.1f}"
            )
        if seller_reviews < min_seller_reviews:
            rejection_reasons.append(
                f"Количество отзывов продавца ({seller_reviews}) ниже минимального {min_seller_reviews}"
            )

        sell_price = cls.calculate_sell_price(
            market_median=market_median,
            lowest_reputable_price=lowest_reputable_price,
            markup_discount=markup_discount,
            price_floor=price_floor,
        )
        net_rev, net_profit, margin = cls.calculate_profit_and_margin(
            buy_price=price,
            sell_price=sell_price,
            fee_rate=fee_rate,
        )
        platform_fee = round(sell_price * fee_rate, 2)

        if net_profit <= 0:
            rejection_reasons.append(f"Убыточная сделка: чистый профит {net_profit} ₽ <= 0")

        # Rigorous requirement: Both profit AND ROI hurdle must be satisfied simultaneously
        profit_condition = (net_profit >= min_profit) and (margin >= min_margin_pct)
        if not profit_condition and net_profit > 0:
            rejection_reasons.append(
                f"Недостаточный профит: {net_profit} ₽ (мин {min_profit} ₽) и/или маржа {margin}% (мин {min_margin_pct}%)"
            )

        # Advanced multi-scenario EV and limits
        ev_metrics = cls.calculate_ev_and_limits(
            buy_price=price,
            net_receipt=net_rev,
            target_margin_rub=min_profit,
            target_roi_rate=min_margin_pct / 100.0,
        )

        if not ev_metrics["meets_targets"]:
            rejection_reasons.append("EV_BELOW_TARGET: риск и ROI не проходят")

        is_eligible = (len(rejection_reasons) == 0)

        return ArbitrageEvaluation(
            is_eligible=is_eligible,
            rejection_reasons=rejection_reasons,
            buy_price=price,
            sell_price=sell_price,
            expected_revenue=net_rev,
            expected_profit=net_profit,
            margin_pct=margin,
            roi_pct=margin,
            platform_fee=platform_fee,
            pricing_source=f"Медиана {int(market_median)} ₽ x {markup_discount} (порог {int(price_floor)} ₽)",
            b_max=ev_metrics["b_max"],
            p_min=ev_metrics["p_min"],
            ev_rub=ev_metrics["ev"],
        )
