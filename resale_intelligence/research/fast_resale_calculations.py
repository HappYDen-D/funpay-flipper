"""Reproducible research calculations; no bot imports and no network calls.

All sale/risk probabilities below are sensitivity assumptions, not market estimates.
Run: python -B resale_intelligence/research/fast_resale_calculations.py
"""
import json
import argparse
from decimal import Decimal as D, ROUND_FLOOR
from pathlib import Path

HERE = Path(__file__).resolve().parent


def floor_cent(x):
    return x.quantize(D('.01'), rounding=ROUND_FLOOR)


def wilson_lower(successes, total, z=1.6448536269514722):
    """One-sided approximate 95% lower bound; independent trials assumed."""
    if total <= 0:
        return None
    p = successes / total
    return (p + z*z/(2*total) - z*(p*(1-p)/total + z*z/(4*total*total))**.5) / (1+z*z/total)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget', type=D, default=D('1000'))
    args = parser.parse_args()
    if not args.budget.is_finite() or args.budget <= 0:
        parser.error('budget must be finite and positive')
    data = json.loads((HERE / 'FAST_RESALE_EVIDENCE_2026-09-12.json').read_text(encoding='utf-8'))
    fx = {k:D(v) for k,v in data['fx'].items() if k != 'source'}
    books = []
    fee = D(data['marketplace_tf_seller_fee'])
    for row in data['marketplace_tf_books']:
        ask, bid = D(row['ask']), D(row['bids'][0][0])
        net = bid*(1-fee)
        books.append({'id':row['id'], 'ask_usd':ask, 'best_bid_usd':bid,
                      'net_bid_usd_before_payout':net, 'profit_usd_before_payout':net-ask,
                      'roi_pct_before_payout':(net/ask-1)*100,
                      'break_even_purchase_usd_before_other_costs':net,
                      'purchase_cap_usd_at_15pct_roi_before_other_costs':floor_cent(net/D('1.15'))})
    # One common stress screen prevents fabricated category-specific success rates.
    # Unsold goods and unsuccessful outcomes have zero terminal cash recovery here.
    u,q,d = D('.05'),D('.05'),D('.10')
    c,m,rho,markup = D('25'),D('30'),D('.15'),D('.12')
    screen=[]
    light_screen=[]
    for row in data['funpay_observations']:
        if not row['screen']:
            continue
        R=D(row['ask'])*fx[row['currency']]
        N=R/(1+markup)
        caps={}
        for p in (D('.5'),D('.8'),D('1')):
            w=(1-u)*p*(1-q)*(1-d)
            budget_for_B=w*N-c
            cap=max(D(0),min(budget_for_B-m,budget_for_B/(1+rho)))
            caps[str(p)]=floor_cent(cap)
        screen.append({'id':row['id'],'indicative_ask_rub':floor_cent(R),
                       'assumed_seller_net_rub':floor_cent(N),
                       'purchase_caps_rub_by_assumed_p':caps,
                       'discount_pct_at_p_08':(1-caps['0.8']/R)*100})
        if row['id'] in {'tf2_funpay_ticket','mm2_icewing','mm2_iceblaster','tf2_funpay_key'}:
            light_caps={}
            for p in (D('.5'),D('.8'),D('1')):
                # Optimistic operational scenario for inexpensive transferable items.
                w=D('.98')**3*p
                cash=w*N-D('5')
                raw=max(D(0),min(cash-D('10'),cash/D('1.15')))
                # Worst-case total loss must fit the 10% pilot loss budget,
                # including the 5 RUB assumed cost. This is research policy only.
                risk_cap=max(D(0),args.budget*D('.1')-D('5'))
                capped=min(raw,args.budget*D('.15'),risk_cap)
                light_caps[str(p)]={'economic_cap':floor_cent(raw),'budget_capped':floor_cent(capped)}
            light_screen.append({'id':row['id'],'caps_by_assumed_p':light_caps})
    # General worked example: all numbers illustrative and terminal inventory = 0.
    N,B,c_example = D('500'),D('300'),D('25')
    factors=(1-u)*(1-q)*(1-d)
    p_break_even=(B+c_example)/(factors*N)
    p_target=(B+c_example+max(m,rho*B))/(factors*N)
    ticket=data['marketplace_tf_books'][1]
    gross_20=sum(D(price)*qty for price,qty in ticket['bids'][:2])+D(ticket['bids'][2][0])*5
    # Price alone can produce a loss even for a highly liquid asset.
    assert books[0]['profit_usd_before_payout']==D('-.225')
    assert all(b['profit_usd_before_payout'] < 0 for b in books)
    assert gross_20==D('14.85')
    assert all(x['purchase_caps_rub_by_assumed_p']['0.5'] <= x['purchase_caps_rub_by_assumed_p']['0.8'] <= x['purchase_caps_rub_by_assumed_p']['1'] for x in screen)
    assert wilson_lower(9,10) < .8 < wilson_lower(28,30)
    output={
      'warning':'Sensitivity research, NOT calibrated probabilities, executable quotes or trading configuration.',
      'current_budget_rub':args.budget,
      'dynamic_budget_policy_examples':[
          {'capital':v,'reserve':max(D('50'),v*D('.4')),
           'inventory_ceiling':max(D(0),v-max(D('50'),v*D('.4'))),
           'lot_concentration_limit':floor_cent(v*D('.15')),
           'supplier_limit':floor_cent(v*D('.25')),
           'category_limit':floor_cent(v*D('.4')),
           'pilot_loss_budget':floor_cent(v*D('.1')),
           'max_new_purchase_zero_recovery_before_costs':floor_cent(v*D('.1'))}
          for v in sorted({args.budget,D('500'),D('1000'),D('3000')})],
      'book_roundtrips':books,
      'scrap_key_roundtrip_loss_pct':(D('62.66')/D('66.66')-1)*100,
      'stn_to_scrap_best_observed_roundtrip_loss_pct':(D('62.66')/D('66.55')-1)*100,
      'ticket_20_units':{'gross_exit_usd':gross_20,'net_exit_usd':gross_20*(1-fee),'purchase_usd':D('16.8'),'profit_usd_before_payout':gross_20*(1-fee)-D('16.8')},
      'screen_assumptions':{'intake_failure_u':u,'early_failure_q_conditional_sale':q,'late_failure_d_conditional_success':d,'operating_cost_rub':c,'min_profit_rub':m,'min_roi':rho,'assumed_buyer_markup_over_seller_net':markup,'terminal_recovery_all_failure_states':0},
      'funpay_sensitivity_screen':screen,
      'low_cost_items_sensitivity':{'warning':'Optimistic hypothetical costs/risks, not calibrated or approved. Unknown sale probabilities cannot authorize buying.',
          'u_q_d_each':'.02','cost_rub':5,'minimum_profit_rub':10,'roi':'.15','salvage':0,'rows':light_screen},
      'ask_to_next_ask_illusion':[
          {'buy_id':buy_id,'hypothetical_exit_ask_id':sell_id,
           'buy_indicative_rub':floor_cent(D(by_id[buy_id]['ask'])*fx['EUR']),
           'hypothetical_profit_rub_before_risk':floor_cent(D(by_id[sell_id]['ask'])*fx['EUR']/D('1.12')-D(by_id[buy_id]['ask'])*fx['EUR']-D('5'))}
          for by_id in [{x['id']:x for x in data['funpay_observations']}]
          for buy_id,sell_id in [('tf2_funpay_ticket','tf2_ticket_comparable'),('mm2_icewing','mm2_icewing_comparable'),('mm2_iceblaster','mm2_iceblaster_comparable')]],
      'worked_example':{'N':N,'B':B,'c':c_example,'ev_at_p_08':D('.8')*factors*N-B-c_example,'p_break_even':p_break_even,'p_for_both_targets':p_target},
      'wilson_one_sided_95pct':{f'{x}/{n}':wilson_lower(x,n) for x,n in [(9,10),(10,10),(18,20),(27,30),(28,30)]},
      'zero_failure_exact_one_sided_95pct_upper':{str(n):1-.05**(1/n) for n in [10,30,59]},
      'illustrative_unit_capital_days':{'buy_rub':300,'profit_rub':50,'cycle_6h_profit_per_capital_day':D('50')/(D('300')*D('6')/24),'cycle_54h_profit_per_capital_day':D('50')/(D('300')*D('54')/24)},
      'validation':'5 calculation invariants passed; not a market or live-trading test'
    }
    target=HERE/'FAST_RESALE_CALCULATIONS_2026-09-12.json'
    target.write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    main()
