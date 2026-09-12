"""Offline hourly Monte Carlo model. All default markets are SYNTHETIC, not forecasts."""
import argparse
import json
import math
import random
from dataclasses import dataclass, asdict, replace
from pathlib import Path
try:
    from .economics import cents,rub,fraction,reserve_required
except ImportError:
    from economics import cents,rub,fraction,reserve_required

@dataclass(frozen=True)
class Market:
    id: str
    buy: int
    receipt: int  # seller-net receipt; never deduct a second fee
    supply_daily: float
    demand_daily: float  # capacity of THIS shop at assumed price, not marketplace volume
    min_sale_hours: int = 2
    confirmation_hours: int = 12
    defect_rate: float = .05
    recovery_fraction: float = .25
    recovery_delay_hours: int = 72
    shelf_life_hours: int = 168
    max_stock: int = 4

@dataclass(frozen=True)
class Scenario:
    name: str
    markets: tuple[Market,...]
    hold_mode: str = 'global_48h'  # conservative account gate + per-credit holds
    hold_hours: int = 48
    daily_cost: int = 0
    supplier_shock_daily: float = 0
    reserve_ratio: float = .15
    reserve_minimum: int = 5000
    category_cap: float = .4
    supplier_cap: float = .25
    lot_cap: float = .15

@dataclass
class Stock:
    market: str
    supplier: int
    buy: int
    receipt: int
    bought_hour: int

@dataclass
class Settlement:
    stock: Stock
    due: int
    amount: int
    success: bool

def poisson(rng, rate):
    if not math.isfinite(rate) or rate<0:
        raise ValueError('Invalid arrival rate')
    if rate==0:
        return 0
    # Split large rates: avoid exp(-rate) underflow.
    if rate>20:
        n=math.ceil(rate/20)
        return sum(poisson(rng,rate/n) for _ in range(n))
    limit,p,k=math.exp(-rate),1.,0
    while p>limit:
        k+=1
        p*=rng.random()
    return k-1

def scenarios():
    # All prices/capacities below are assumptions. Historical costs retained ONLY for sensitivity.
    ids=['steam','discord','cursor','exitlag','tg_premium','chatgpt']
    buys=[20,25,40,55,270,315]
    old_receipts=[78.32,131.12,148.72,166.32,307.12,571.12]
    supply=[2,1.5,1,.8,.3,.3]
    demand=[1,.8,.6,.5,.2,.2]
    base=tuple(Market(i,cents(b),cents(b*1.45),s,d)
               for i,b,s,d in zip(ids,buys,supply,demand))
    legacy=tuple(replace(m,receipt=cents(r),supply_daily=m.supply_daily*3,
                         demand_daily=m.demand_daily*3,confirmation_hours=6,
                         defect_rate=.03) for m,r in zip(base,old_receipts))
    stress=tuple(replace(m,receipt=fraction(m.buy,1)+fraction(m.buy,.25),
                         demand_daily=m.demand_daily*.5,defect_rate=.12,
                         shelf_life_hours=72) for m in base)
    return [
        Scenario('legacy_prices_with_constraints',legacy),
        Scenario('compressed_global48',base),
        Scenario('compressed_per_credit48',base,hold_mode='per_credit'),
        Scenario('compressed_no_hold',base,hold_mode='none',hold_hours=0),
        Scenario('compressed_cost_5rub_day',base,daily_cost=500),
        Scenario('stress',stress,daily_cost=500,supplier_shock_daily=.02),
    ]

class CompoundingSimulator:
    def __init__(self,scenario=None,initial_capital=350.,target_goal=25000.,seed=42):
        self.scenario=scenario or scenarios()[1]
        s=self.scenario
        if s.hold_mode not in ('global_48h','per_credit','none') or s.hold_hours<0 or s.daily_cost<0:
            raise ValueError('Invalid scenario')
        if s.hold_mode=='none' and s.hold_hours!=0:
            raise ValueError('No-hold scenario requires zero hold hours')
        for v in (s.supplier_shock_daily,s.reserve_ratio,s.category_cap,s.supplier_cap,s.lot_cap):
            fraction(0,v)
        if initial_capital<=0 or target_goal<=0 or s.reserve_minimum<0:
            raise ValueError('Invalid capital or reserve')
        if len({m.id for m in s.markets})!=len(s.markets) or not s.markets:
            raise ValueError('Markets must be nonempty and unique')
        for m in s.markets:
            if (m.buy<=0 or m.receipt<0 or m.supply_daily<0 or m.demand_daily<0 or
                m.min_sale_hours<1 or m.confirmation_hours<1 or m.shelf_life_hours<1 or
                m.recovery_delay_hours<1 or m.max_stock<1):
                raise ValueError('Invalid market')
            fraction(0,m.defect_rate)
            fraction(0,m.recovery_fraction)
        self.initial=cents(initial_capital)
        self.goal=cents(target_goal)
        self.cash=self.initial
        self.pnl=0
        self.hour=0
        self.holds=[]  # (unlock hour, amount)
        self.stock=[]
        self.settlements=[]
        self.rng=random.Random(seed)
        self.last_sale=-1_000_000
        self.completed=self.failed=self.expired=self.purchases=0
        self.blocked_hours=0
        self.insolvent_ever=False
        self.min_equity=self.initial
        self.first_goal={k:None for k in ('equity','profit','available_cash')}
        self.snapshots=[]
        self.record()

    @property
    def equity(self):
        return self.cash+sum(v for _,v in self.holds)+sum(x.buy for x in self.stock)+sum(x.stock.buy for x in self.settlements)

    def purchase_allowed(self):
        return self.scenario.hold_mode!='global_48h' or self.hour-self.last_sale>=self.scenario.hold_hours

    def record(self):
        assert self.equity==self.initial+self.pnl, 'Cash/accounting conservation violated'
        self.min_equity=min(self.min_equity,self.equity)
        self.insolvent_ever=self.insolvent_ever or self.cash<0
        available=self.cash if self.purchase_allowed() else 0
        for key,value in dict(equity=self.equity,profit=self.pnl,available_cash=available).items():
            if value>=self.goal and self.first_goal[key] is None:
                self.first_goal[key]=self.hour
        return dict(hour=self.hour,cash=rub(self.cash),available_cash=rub(max(0,available)),
                    held=rub(sum(v for _,v in self.holds)),
                    stock_cost=rub(sum(x.buy for x in self.stock)),
                    unsettled_cost=rub(sum(x.stock.buy for x in self.settlements)),
                    equity=rub(self.equity),profit=rub(self.pnl),
                    conservative_assets=rub(self.cash+sum(v for _,v in self.holds)),
                    reserve=rub(reserve_required(self.equity,self.scenario.reserve_minimum,self.scenario.reserve_ratio)),
                    completed=self.completed,failed=self.failed,expired=self.expired,purchases=self.purchases)

    def process_hour(self):
        self.hour+=1
        s=self.scenario
        markets={m.id:m for m in s.markets}
        # Matured receipts. No purchase can occur before account-level gate opens.
        remaining=[]
        for due,amount in self.holds:
            if due<=self.hour:
                self.cash+=amount
            else:
                remaining.append((due,amount))
        self.holds=remaining
        # Completion/recovery events: recognize actual simulated receipt and PnL once.
        remaining=[]
        for item in self.settlements:
            if item.due>self.hour:
                remaining.append(item)
                continue
            self.pnl+=item.amount-item.stock.buy
            if item.amount:
                self.holds.append((self.hour+s.hold_hours,item.amount))
            if item.success:
                self.completed+=1
                self.last_sale=self.hour
            else:
                self.failed+=1
        self.settlements=remaining
        # Zero-hour hold credits are usable on the same hour.
        self.cash+=sum(v for t,v in self.holds if t<=self.hour)
        self.holds=[(t,v) for t,v in self.holds if t>self.hour]
        # Correlated shock: one of six suppliers loses all currently held unsold stock.
        if self.hour%24==0:
            self.cash-=s.daily_cost
            self.pnl-=s.daily_cost
            if self.rng.random()<s.supplier_shock_daily:
                supplier=self.rng.randrange(6)
                doomed=[x for x in self.stock if x.supplier==supplier]
                self.pnl-=sum(x.buy for x in doomed)
                self.expired+=len(doomed)
                self.stock=[x for x in self.stock if x.supplier!=supplier]
        expired=[x for x in self.stock if self.hour-x.bought_hour>=markets[x.market].shelf_life_hours]
        self.pnl-=sum(x.buy for x in expired)
        self.expired+=len(expired)
        self.stock=[x for x in self.stock if self.hour-x.bought_hour<markets[x.market].shelf_life_hours]
        # Shared category demand, not an independent guaranteed sale for every bought unit.
        for m in s.markets:
            demand=poisson(self.rng,m.demand_daily/24)
            ready=[x for x in self.stock if x.market==m.id and self.hour-x.bought_hour>=m.min_sale_hours]
            for x in ready[:demand]:
                self.stock.remove(x)
                self.last_sale=self.hour
                success=self.rng.random()>=m.defect_rate
                amount=x.receipt if success else fraction(x.buy,m.recovery_fraction)
                delay=m.confirmation_hours if success else m.recovery_delay_hours
                self.settlements.append(Settlement(x,self.hour+delay,amount,success))
        self.record()  # capture transient available cash before reinvestment
        if not self.purchase_allowed():
            self.blocked_hours+=1
        # New qualified opportunities arrive stochastically. No additional deposits/loans.
        choices=list(s.markets)
        self.rng.shuffle(choices)
        for m in choices:
            offers=poisson(self.rng,m.supply_daily/24)
            for _ in range(offers):
                supplier=self.rng.randrange(6)
                exposure=self.stock+[p.stock for p in self.settlements]
                reserve=reserve_required(self.equity,s.reserve_minimum,s.reserve_ratio)
                if not self.purchase_allowed() or self.cash-m.buy<reserve:
                    continue
                if m.buy>fraction(max(0,self.equity),s.lot_cap):
                    continue
                if sum(x.market==m.id for x in exposure)>=m.max_stock:
                    continue
                if sum(x.buy for x in exposure if x.market==m.id)+m.buy>fraction(max(0,self.equity),s.category_cap):
                    continue
                if sum(x.buy for x in exposure if x.supplier==supplier)+m.buy>fraction(max(0,self.equity),s.supplier_cap):
                    continue
                self.cash-=m.buy
                self.stock.append(Stock(m.id,supplier,m.buy,m.receipt,self.hour))
                self.purchases+=1
        snapshot=self.record()
        if self.hour%24==0:
            self.snapshots.append(snapshot)
        return snapshot

    def run_simulation(self,max_days=30):
        if not isinstance(max_days,int) or max_days<1:
            raise ValueError('Horizon must be a positive integer')
        while self.hour<max_days*24:
            self.process_hour()
        # Always include final state, even when reused with a different horizon.
        return self.snapshots

def quantile(values,p):
    a=sorted(values)
    n=(len(a)-1)*p
    lo,hi=math.floor(n),math.ceil(n)
    return round(a[lo]+(a[hi]-a[lo])*(n-lo),2)

def run_research(runs=300,days=30):
    if runs<1:
        raise ValueError('Positive run count required')
    results=[]
    for scenario in scenarios():
        simulations=[]
        for seed in range(runs):
            sim=CompoundingSimulator(scenario,seed=seed)
            sim.run_simulation(days)
            simulations.append(sim)
        row=dict(scenario=scenario.name,runs=runs,days=days,assumption_only=True,
                 parameters=asdict(scenario),goals={})
        for metric in ('equity','profit','available_cash'):
            vals=[sim.record()[metric] for sim in simulations]
            row[metric]={f'p{int(p*100)}':quantile(vals,p) for p in (.1,.5,.9)}
            times=[sim.first_goal[metric] for sim in simulations if sim.first_goal[metric] is not None]
            row['goals'][metric]=dict(probability=len(times)/runs,
                                    median_day_if_reached=round(quantile(times,.5)/24,2) if times else None)
        row['loss_probability']=sum(sim.equity<sim.initial for sim in simulations)/runs
        row['half_capital_drawdown_probability']=sum(sim.min_equity<=sim.initial*.5 for sim in simulations)/runs
        row['cash_shortfall_probability']=sum(sim.insolvent_ever for sim in simulations)/runs
        row['mean_blocked_hours']=round(sum(sim.blocked_hours for sim in simulations)/runs,1)
        sample=simulations[0]
        row['seed0_daily']=sample.snapshots
        results.append(row)
    return dict(initial_capital=350,target=25000,seed_range=[0,runs-1],horizon_days=days,
                notice='Synthetic scenarios, NOT fitted market probabilities or income forecasts',results=results)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--runs',type=int,default=300)
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=run_research(args.runs,args.days)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k not in ('parameters','seed0_daily')} for r in result['results']],indent=2))
