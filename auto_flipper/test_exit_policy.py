"""Offline boundaries for verified demand admission and exact purchase ceilings."""
from copy import deepcopy
from decimal import Decimal
import unittest

from auto_flipper.exit_policy import evaluate_exit


NOW = 1_800_000_000


def fixture():
    return {
        'sku': 'tf2-key-tradable', 'route_cost': '5.00',
        'estimated_execution_seconds': 30,
        'product': {'kind': 'trade_item', 'sku': 'tf2-key-tradable',
                    'expires_at': None, 'transfer_method': 'platform_trade',
                    'transfer_ready_at': NOW-1},
        'exit_quote': {'schema_version': 1, 'quote_id': 'bid-123',
                       'sku': 'tf2-key-tradable', 'buyer_id': 'buyer-A',
                       'source': 'https://example.test/bids/123',
                       'observed_at': NOW-20, 'expires_at': NOW+120,
                       'quantity': 1, 'unit_net_receipt': '120.00',
                       'settlement_currency': 'RUB', 'settlement_hours': 1,
                       'transfer_verified': True, 'payout_verified': True,
                       'executable': True},
    }


class ExitPolicyTests(unittest.TestCase):
    def evaluate(self, review=None, price='100.00', **kwargs):
        return evaluate_exit(fixture() if review is None else review, price, now=NOW, **kwargs)

    def test_exact_hurdles_and_caps(self):
        result = self.evaluate()
        self.assertTrue(result['admitted'], result)
        self.assertEqual(result['net_profit'], '15.00')
        self.assertEqual(result['b_max'], '100.00')
        self.assertEqual(result['roi'], '0.15')
        self.assertEqual(result['downside'], '105.00')
        self.assertEqual(result['adverse_exit_net'], '0.00')
        self.assertFalse(self.evaluate(price='100.01')['admitted'])

    def test_absolute_profit_and_roi_are_both_required(self):
        self.assertFalse(self.evaluate(min_profit=16, min_roi=0)['admitted'])
        self.assertFalse(self.evaluate(min_profit=0, min_roi='.151')['admitted'])
        self.assertTrue(self.evaluate(min_profit=15, min_roi='.15')['admitted'])

    def test_cap_rounds_down_to_kopeck(self):
        review = fixture()
        review['exit_quote']['unit_net_receipt'] = '120.01'
        result = self.evaluate(review)
        self.assertEqual(result['b_max'], '100.00')
        self.assertFalse(self.evaluate(review, price='100.01')['admitted'])

    def test_no_hard_capital_ceiling(self):
        review = fixture()
        review['exit_quote']['unit_net_receipt'] = '1200000.00'
        self.assertTrue(self.evaluate(review, price='1000000')['admitted'])

    def test_quote_freshness_and_execution_window(self):
        for observed, expires, expected in [(NOW-60, NOW+31, True),
                                            (NOW-61, NOW+120, False),
                                            (NOW+1, NOW+120, False),
                                            (NOW-20, NOW+30, False),
                                            (NOW-20, NOW-1, False)]:
            with self.subTest(observed=observed, expires=expires):
                review = fixture()
                review['exit_quote'].update(observed_at=observed, expires_at=expires)
                self.assertEqual(self.evaluate(review)['admitted'], expected)

    def test_capacity_sku_currency_and_required_evidence(self):
        cases = [('quantity', 0), ('quantity', True), ('quantity', 1.0),
                 ('sku', 'different'), ('settlement_currency', 'USD'),
                 ('schema_version', True), ('schema_version', 2),
                 ('transfer_verified', 1), ('payout_verified', 'true'),
                 ('executable', False), ('source', ''), ('buyer_id', None)]
        for name, value in cases:
            with self.subTest(field=name, value=value):
                review = fixture()
                review['exit_quote'][name] = value
                self.assertFalse(self.evaluate(review)['admitted'])

    def test_missing_fields_reject(self):
        for section in (None, 'product', 'exit_quote'):
            template = fixture()
            fields = template if section is None else template[section]
            for name in fields:
                with self.subTest(section=section, field=name):
                    review = fixture()
                    del (review if section is None else review[section])[name]
                    self.assertFalse(self.evaluate(review)['admitted'])

    def test_subscription_expiry_hold_and_wrong_transfer_reject(self):
        cases = [('kind', 'subscription'), ('expires_at', NOW+86400),
                 ('is_subscription', True), ('transfer_ready_at', NOW+1),
                 ('transfer_method', 'permanent_code'), ('sku', 'another')]
        for name, value in cases:
            with self.subTest(field=name):
                review = fixture()
                review['product'][name] = value
                self.assertFalse(self.evaluate(review)['admitted'])

    def test_supported_product_methods(self):
        for kind, method in [('permanent_key', 'permanent_code'),
                             ('account', 'account_credentials'),
                             ('trade_item', 'manual')]:
            review = fixture()
            review['product'].update(kind=kind, transfer_method=method)
            self.assertTrue(self.evaluate(review)['admitted'])

    def test_full_cash_cycle_not_only_listing_or_sale_time(self):
        review = fixture()
        review['exit_quote']['settlement_hours'] = 72
        self.assertFalse(self.evaluate(review)['admitted'])
        review['exit_quote']['settlement_hours'] = 71
        self.assertTrue(self.evaluate(review)['admitted'])

    def test_self_buyer_rejects(self):
        self.assertFalse(self.evaluate(buyer_identity=' BUYER-a ')['admitted'])
        self.assertTrue(self.evaluate(buyer_identity='seller-B')['admitted'])

    def test_invalid_numbers_fail_closed(self):
        for value in [True, False, None, {}, [], 'NaN', 'Infinity', '-1', '1.001', '1e999999']:
            with self.subTest(value=value):
                self.assertFalse(self.evaluate(price=value)['admitted'])
        for field in ['route_cost', 'estimated_execution_seconds']:
            for value in [True, None, 'NaN', '-1']:
                review = fixture()
                review[field] = value
                self.assertFalse(self.evaluate(review)['admitted'])
        self.assertFalse(self.evaluate(price=0)['admitted'])

    def test_nonpositive_execution_and_multiple_unit_buy_reject(self):
        review = fixture()
        review['estimated_execution_seconds'] = 0
        self.assertFalse(self.evaluate(review)['admitted'])
        for value in [0, 2, True, '1']:
            review = fixture()
            review['quantity'] = value
            self.assertFalse(self.evaluate(review)['admitted'])

    def test_unsubstantiated_salvage_rejects(self):
        review = fixture()
        review['adverse_exit_net'] = '80.00'
        self.assertFalse(self.evaluate(review)['admitted'])
        review['adverse_exit_net'] = '0'
        self.assertTrue(self.evaluate(review)['admitted'])

    def test_verified_fallback_is_still_conditional_and_independent(self):
        review = fixture()
        review['fallback_quote'] = deepcopy(review['exit_quote'])
        review['fallback_quote'].update(quote_id='bid-456', buyer_id='buyer-B', unit_net_receipt='80.00')
        result = self.evaluate(review)
        self.assertTrue(result['admitted'])
        self.assertEqual(result['adverse_exit_net'], '80.00')
        self.assertEqual(result['downside'], '25.00')
        self.assertEqual(result['full_cash_at_risk'], '105.00')
        review['fallback_quote']['buyer_id'] = 'buyer-A'
        self.assertFalse(self.evaluate(review)['admitted'])

    def test_low_exit_value_never_authorizes(self):
        review = fixture()
        review['exit_quote']['unit_net_receipt'] = '1.00'
        result = self.evaluate(review)
        self.assertFalse(result['admitted'])
        self.assertEqual(result['b_max'], '0.00')

    def test_inputs_not_mutated_and_nonobject_rejected(self):
        review = fixture()
        original = deepcopy(review)
        self.evaluate(review)
        self.assertEqual(review, original)
        self.assertFalse(evaluate_exit(None, 100, now=NOW)['admitted'])


if __name__ == '__main__':
    unittest.main()
