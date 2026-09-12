import unittest
from dataclasses import replace
from resale_intelligence.models.risk_gate import RiskSnapshot, RiskPolicy, assess_purchase, delayed_refund_effect


class RiskGateTests(unittest.TestCase):
    def setUp(self):
        self.state = RiskSnapshot(35000,35000,35000,0,snapshot_age_seconds=1,
                                  balance_verified=True,purchasing_available=True,reconciliation_ok=True)

    def test_default_unknown_state_blocks(self):
        self.assertFalse(assess_purchase(2000,RiskSnapshot(35000,35000,35000,0)).allowed)

    def test_pilot_lot_limit(self):
        self.assertTrue(assess_purchase(5250,self.state).allowed)
        self.assertFalse(assess_purchase(5251,self.state).allowed)

    def test_refunds_and_pending_debits_cannot_be_reinvested(self):
        s=replace(self.state,pending_purchase_debits=20000,refund_liabilities=8000,operating_buffer=500)
        r=assess_purchase(2000,s)
        self.assertEqual(r.cash_after_commitments,6500)
        self.assertEqual(r.purchase_budget,1250)
        self.assertFalse(r.allowed)

    def test_supplier_exposure_counts_pending(self):
        self.assertFalse(assess_purchase(1000,replace(self.state,supplier_exposure=8000)).allowed)
        self.assertTrue(assess_purchase(750,replace(self.state,supplier_exposure=8000)).allowed)

    def test_stop_conditions_are_independent(self):
        cases=[dict(snapshot_age_seconds=61),dict(snapshot_age_seconds=float('nan')),
               dict(emergency_stopped=True),dict(unresolved_purchase=True),
               dict(reconciliation_ok=False),dict(purchasing_available=False),dict(supplier_quarantined=True)]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(assess_purchase(2000,replace(self.state,**change)).allowed)

    def test_loss_boundary(self):
        self.assertTrue(assess_purchase(2000,replace(self.state,session_loss=3499)).allowed)
        self.assertIn('LOSS_LIMIT',assess_purchase(2000,replace(self.state,session_loss=3500)).reasons)

    def test_invalid_amounts_and_policy(self):
        for amount in (-1,0,float('nan'),True,20.5):
            self.assertFalse(assess_purchase(amount,self.state).allowed)
        self.assertFalse(assess_purchase(1000,self.state,RiskPolicy(lot_cap=float('nan'))).allowed)

    def test_delayed_refund_does_not_double_charge_purchase(self):
        # B=25, N=40 -> initial profit 15. Refund 40, supplier pays back 10 -> loss 15.
        event=delayed_refund_effect(4000,4000,1000)
        self.assertEqual(1500+event['pnl_adjustment'],-1500)
        self.assertEqual(event['remaining_receipt'],1000)


if __name__=='__main__':
    unittest.main()
