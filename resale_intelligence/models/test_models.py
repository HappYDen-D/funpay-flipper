"""Offline accounting and decision regressions; no network, bot or database imports."""
import math
import unittest
from dataclasses import replace
from resale_intelligence.models.economics import cents, seller_receipt, expected_profit
from resale_intelligence.models.capital_allocation import AllocationCandidate, allocate
from resale_intelligence.models.scoring_engine import CandidateLot, MarketContext, ScoringEngine, comparable_market
from resale_intelligence.models.compounding_simulation import CompoundingSimulator, Scenario, Market, Settlement, Stock, scenarios


class EconomicsTests(unittest.TestCase):
    def test_rounding_and_fee_basis(self):
        self.assertEqual(cents(1.005),101)
        self.assertEqual(seller_receipt(34900,'seller_net',.12),34900)
        self.assertEqual(seller_receipt(34900,'buyer_gross',.12),30712)
        with self.assertRaises(ValueError):
            seller_receipt(34900,'buyer_gross')

    def test_ev_refund_and_unsold(self):
        self.assertEqual(expected_profit(10000,15000,1,0),-10000)
        self.assertEqual(expected_profit(10000,15000,0,sale_probability=0),-10000)
        self.assertEqual(expected_profit(10000,15000,.1,.5,1000,.8),200.)

    def test_allocation_reserve_exposure_and_evidence(self):
        rows=[AllocationCandidate('a','micro','seller1',2000,4000,20),
              AllocationCandidate('b','micro2','seller2',2500,4500,20)]
        self.assertEqual(allocate(rows,35000,35000)['spent'],0)
        result=allocate(rows,35000,35000,allow_unverified=True,seller_exposure={'seller1':8000})
        self.assertEqual(result['quantities']['a'],0)
        self.assertLessEqual(result['quantities']['b']*25,87.5)
        self.assertGreaterEqual(result['cash_remaining'],52.5)


class ScoringTests(unittest.TestCase):
    def sample(self):
        lot=CandidateLot('lot1','micro','Valid digital item',20,'s',5,100,
                         sku_key='micro|30d|ru|code',quote_verified=True,resale_status='allowed')
        ctx=MarketContext('micro',60,58,30,.9,5,20,4.5,3,
                          sku_key=lot.sku_key,sample_count=20,seller_count=5,quote_age_minutes=1,
                          price_basis='seller_net',economics_verified=True,
                          max_buy_price=50,spendable_cash=100)
        return lot,ctx

    def test_auto_requires_evidence(self):
        lot,ctx=self.sample()
        self.assertEqual(ScoringEngine.evaluate_lot(lot,ctx).decision,'AUTO_BUY')
        lot.quote_verified=False
        result=ScoringEngine.evaluate_lot(lot,ctx)
        self.assertEqual(result.decision,'MANUAL_REVIEW')
        self.assertFalse(result.is_eligible)

    def test_missing_market_and_invalid_price(self):
        lot,ctx=self.sample()
        ctx.price_basis='unknown'
        self.assertNotEqual(ScoringEngine.evaluate_lot(lot,ctx).decision,'AUTO_BUY')
        for price in (0,-1,math.nan,math.inf):
            lot.price=price
            self.assertEqual(ScoringEngine.evaluate_lot(lot,ctx).decision,'REJECTED')

    def test_min_rating_five_and_bounded_score(self):
        score,_=ScoringEngine.calculate_seller_trust(5,100000,5)
        self.assertEqual(score,100)
        with self.assertRaises(ValueError):
            ScoringEngine.calculate_seller_trust(5.1,2)

    def test_competitor_below_floor_is_not_ignored(self):
        self.assertEqual(ScoringEngine.calculate_undercut_price(100,20,30,.9)[0],0)

    def test_profit_and_roi_both_required(self):
        lot,ctx=self.sample()
        ctx.min_profit=100
        self.assertEqual(ScoringEngine.evaluate_lot(lot,ctx).decision,'REJECTED')

    def test_duplicate_seller_not_market_weight(self):
        obs=[dict(sku_key='a',seller='spam',price=1)]*100
        obs+=[dict(sku_key='a',seller='s2',price=100),dict(sku_key='a',seller='s3',price=110)]
        obs+=[dict(sku_key='other',seller='s4',price=1000)]
        self.assertEqual(comparable_market(obs,'a')['median'],100)


class SimulationTests(unittest.TestCase):
    def test_reproducibility_accounting_and_reserve(self):
        scenario=scenarios()[3]
        a,b=CompoundingSimulator(scenario,seed=6),CompoundingSimulator(scenario,seed=6)
        for _ in range(240):
            before=a.purchases
            snapshot=a.process_hour()
            if a.purchases>before:
                self.assertGreaterEqual(snapshot['cash'],snapshot['reserve'])
            self.assertEqual(a.equity,a.initial+a.pnl)
        b.run_simulation(10)
        self.assertEqual(a.snapshots,b.snapshots)

    def test_global_gate_and_per_credit_differ(self):
        a=CompoundingSimulator(scenarios()[1])
        a.last_sale=10
        a.hour=57
        self.assertFalse(a.purchase_allowed())
        a.hour=58
        self.assertTrue(a.purchase_allowed())
        b=CompoundingSimulator(scenarios()[2])
        b.last_sale=10
        b.hour=11
        self.assertTrue(b.purchase_allowed())

    def test_completion_precedes_hold_and_goal(self):
        market=Market('x',2000,4000,0,0)
        sim=CompoundingSimulator(Scenario('test',(market,)),target_goal=360)
        sim.cash-=2000
        item=Stock('x',0,2000,4000,0)
        sim.settlements=[Settlement(item,2,4000,True)]
        sim.process_hour()
        self.assertEqual(sim.pnl,0)
        sim.process_hour()
        self.assertEqual(sim.pnl,2000)
        self.assertEqual(sim.holds,[(50,4000)])
        self.assertEqual(sim.first_goal['equity'],2)
        self.assertIsNone(sim.first_goal['available_cash'])
        while sim.hour<50:
            sim.process_hour()
        self.assertEqual(sim.cash,37000)
        self.assertEqual(sim.first_goal['available_cash'],50)

    def test_no_demand_expires_inventory(self):
        market=Market('x',2000,3000,20,0,shelf_life_hours=24)
        sim=CompoundingSimulator(Scenario('zero-demand',(market,),hold_mode='none',hold_hours=0),seed=0)
        sim.run_simulation(5)
        self.assertEqual(sim.completed,0)
        self.assertGreater(sim.expired,0)
        self.assertLess(sim.equity,sim.initial)

    def test_final_snapshot_and_no_goal_is_not_success(self):
        sim=CompoundingSimulator()
        self.assertEqual(sim.run_simulation(1)[-1]['hour'],24)
        self.assertIsNone(sim.first_goal['equity'])


if __name__=='__main__':
    unittest.main()
