"""Reproduce illustrative screening arithmetic; no market forecast or trading I/O."""

import json
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

D = Decimal


def floor_money(value):
    return str(value.quantize(D("0.01"), rounding=ROUND_FLOOR))


def calculate():
    scenarios = []
    for name, p, q, d in [
        ("illustrative_base", "0.85", "0.03", "0.01"),
        ("illustrative_stress", "0.65", "0.08", "0.03"),
    ]:
        success = D(p) * (1 - D(q)) * (1 - D(d))
        rows = []
        for net in map(D, ["100", "300", "700", "1500"]):
            costs = D("5") + D("0.02") * net
            ceiling = min(success * net - costs - 10,
                          (success * net - costs) / D("1.20"))
            rows.append({"seller_net_rub": str(net), "costs_rub": str(costs),
                         "max_buyer_debit_rub": floor_money(ceiling)})
        scenarios.append({"name": name, "sale_probability": p,
                          "early_defect_conditional": q,
                          "late_refund_conditional": d,
                          "retained_sale_probability": str(success), "rows": rows})
    return {
        "as_of": "2026-09-11",
        "status": "illustrative_assumptions_not_empirical",
        "horizon_days": 14,
        "formula": "a=p*(1-q)*(1-d); Bmax=min(a*N-c-m,(a*N-c)/(1+rho))",
        "assumptions": {"unsold_recovery": "0", "supplier_cash_recovery": "0",
                        "late_refund_amount": "N", "costs": "5 RUB + 0.02*N",
                        "minimum_expected_profit_rub": "10", "minimum_expected_roi": "0.20"},
        "rounding": "purchase ceilings rounded down to kopecks",
        "scenarios": scenarios,
        "capital_example_rub": {"capital": "350.00", "reserve": "52.50",
                                "lot_limit": "52.50", "supplier_limit": "87.50",
                                "category_limit": "140.00"},
        "internal_spread_example": {
            "currency": "EUR", "hypothetical_buyer_debit": "5.74",
            "hypothetical_resale_buyer_gross": "6.22",
            "same_sku_verified": False, "fee_verified": False,
            "markup_percent": str((D("6.22") / D("5.74") - 1) * 100),
            "break_even_deduction_percent": str((1 - D("5.74") / D("6.22")) * 100),
            "profit_at_hypothetical_8_percent_deduction_eur": str(D("6.22") * D("0.92") - D("5.74"))},
        "zero_defect_one_sided_95_percent_upper_bound": {
            "formula": "1 - 0.05**(1/n)",
            "assumes_independent_fully_observed_orders": True,
            "n_30": 1 - 0.05 ** (1 / 30), "n_100": 1 - 0.05 ** (1 / 100)},
    }


if __name__ == "__main__":
    output = Path(__file__).with_name("PRODUCT_SCREENING_CALCULATIONS.json")
    output.write_text(json.dumps(calculate(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output.name)
