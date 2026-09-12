# Account Market Observer (v0.5)

## Scope and architecture

This stage is a production read-only observer. It does not build Account Flip
Score and cannot purchase, message, publish, modify, or check out an account.

`AccountMarketObserver` uses the existing `FunPayClient` transport and the
existing `Database`/`MarketHistoryStore`. Account state, transition events,
market samples, and cohort samples are additional tables in the same SQLite
history architecture. `auto_flipper.account_cohort_normalizer` is the single
canonical deterministic account matcher. The pre-existing canonical liquidity
and TF2 SKU implementations remain in `auto_flipper`; modules under
`resale_intelligence/models` are re-export wrappers, not competing copies.

## Seed markets

Only three confirmed public FunPay account nodes are enabled:

| Market | Canonical ID | Public URL |
|---|---|---|
| Brawl Stars | `funpay:account:436` | `https://funpay.com/lots/436/` |
| Clash of Clans | `funpay:account:147` | `https://funpay.com/lots/147/` |
| Fortnite | `funpay:account:248` | `https://funpay.com/lots/248/` |

The allowlist is intentional. Other games are not runtime candidates until
their public node and normalization model are separately validated.

## Eligibility gates

Defaults are configurable with environment variables:

- active lots >= 100;
- independent sellers >= 20;
- RUB lots <= 500: at least 10;
- independent sellers in that cheap segment: at least 5;
- parseable account ratio >= 0.35;
- largest seller share <= 0.50.

The 500 RUB boundary is only a research accessibility measure. It is not a
spending authorization. Non-RUB prices are retained with their displayed or
unknown currency but excluded from RUB accessibility and cohort price samples;
there is no invented FX conversion.

## Market Observation Priority Score (MOPS)

Each component is normalized to 0..100 and uses saturating/logarithmic scaling:

```
MOPS = 0.25 * depth_score
     + 0.20 * seller_diversity_score
     + 0.20 * budget_accessibility_score
     + 0.15 * cohortability_score
     + 0.10 * turnover_proxy_score
     + 0.10 * price_structure_score
```

Depth saturates instead of rewarding raw size linearly. Seller diversity uses
seller count, largest/top-3 share, and HHI. Budget accessibility uses cheap lot
count, cheap sellers, and cheap-market share. Cohortability uses classified
coverage, cohort count, and median cohort size. Turnover uses disappearances,
reappearances, and listing churn. Price structure uses only sufficiently sized
within-cohort samples, robust percentiles, MAD, and dispersion. A global
Fortnite median is never used.

`DISAPPEARED != SOLD`. A disappearance is only a turnover proxy. High
reappearance degrades the signal.

## AccountMarketRiskScore

Observed risk is a separate 0..100 result and never enters MOPS. It aggregates
observable access/recovery claims, rental/access ambiguity, email claims,
platform linking, seller reputation, reappearance, seller concentration, and
mass-identical listings. It is a risk proxy, not a statement that an account is
safe or unsafe. Thus a market may legitimately have both high MOPS and high
observed risk.

## Cohort normalization

Normalization is deterministic, uses no LLM, and prefers false splits to false
merges.

- Brawl Stars: trophy band + brawler-count band. Missing/conflicting key fields
  are `UNCLASSIFIED`.
- Clash of Clans: Town Hall + broad summed hero progression band. Missing hero
  data gets a lower-confidence `heroes_unknown` cohort; missing/conflicting TH
  is `UNCLASSIFIED`.
- Fortnite: SALE and RENT are always separate, then platform, skin-count band,
  access claim, and email claim. Known specialty/rare cosmetics are
  `UNCLASSIFIED_SPECIALTY` and excluded from generic cohort medians.

Stable cohort IDs are derived only from normalized feature buckets and remain
stable between scans.

## History and confidence

Account lot state contains market/lot ID, seller evidence, price/currency,
stock, title, cohort/features/risk flags, first/last seen, active state,
reappearance count, and price-change count. Strict event types are `NEW`,
`PRICE_CHANGE`, `STOCK_CHANGE`, `DISAPPEARED`, and `REAPPEARED`.

Per-cohort samples contain P10/P25/P50/P75/P90, min/max, MAD, dispersion,
sellers, cheap-lot count, and transition counts.

- LOW: elapsed < 1h or fewer than 12 samples;
- MEDIUM: elapsed >= 1h and at least 12 samples;
- HIGH: elapsed >= 6h and at least 60 samples.

No backfill invents night observations, and one HTTP GET can never yield HIGH.

## Polling and selection

After baseline data exists, eligible markets are ranked only by MOPS. At most
two are ACTIVE WATCH (default random interval 3–5 minutes); the rest are
BACKGROUND (12–15 minutes). Names receive no bonus. A challenger must exceed
an incumbent by 5 points for 3 consecutive samples before replacement, avoiding
selection thrashing. All intervals and hysteresis values are configurable.

## Telegram

- `/accountmarkets` — ranking, MOPS, observed risk, confidence, core metrics,
  and ACTIVE/BACKGROUND mode;
- `/accountmarkets brawl|bs|бравл`;
- `/accountmarkets coc|клеш|кок`;
- `/accountmarkets fortnite|fn|фортнайт`;
- `/accountmarkets summary` — only actually recorded history and top cohorts.

Every detail view says `OBSERVATION ONLY`.

## FutureFlipEligibility

`NOT_READY`, `WATCH`, and `PROMISING` describe whether the market is worth a
future Account Flip Score stage. `PROMISING` requires at least MEDIUM
confidence, MOPS >= 60, observed risk <= 70, sufficient cohort size, acceptable
dispersion, and non-excessive reappearance. It never authorizes BUY.

## Safety guarantees

- observer transport is allowlisted to nodes 436/147/248 and delegates only to
  the GET market fetcher;
- stored rows have `source_type=ACCOUNT_OBSERVATION`,
  `purchase_eligible=0`, plus a database CHECK constraint;
- account sources/market IDs are rejected by assistant review, capital
  admission, and atomic purchase claim paths;
- account observations never enter `trade_candidates` or the auto-buy scan;
- no checkout, payment, message, offer-save, listing-change, or boost method is
  called by the observer;
- existing capital and 10% open-risk policy is unchanged.

## Limitations

Public listing HTML may change, omit key fields, or show only part of a market.
Text parsing supports conservative common Russian/English patterns rather than
every seller spelling. Disappearance is not proof of sale. Risk features are
observational proxies. Specialty Fortnite cosmetics need a later dedicated
model. Meaningful turnover and FutureFlipEligibility require actual elapsed
history; a smoke test cannot supply it.

## Live smoke

Executed 2026-09-13 02:48:18–02:48:29 Europe/Moscow using
`run_account_market_smoke.py` and a disposable SQLite database:

| Market | Parsed | Sellers | <=500 RUB | Classified | Unclassified | MOPS | Risk | Confidence | Eligible |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| Brawl Stars | 4,000 | 2,010 | 1,304 | 3,559 | 441 | 83.45 | 13.39 | LOW | yes |
| Clash of Clans | 2,000 | 578 | 471 | 778 | 1,222 | 74.08 | 12.75 | LOW | yes |
| Fortnite | 2,000 | 912 | 585 | 112 | 1,888 | 69.80 | 23.87 | LOW | no |

Requests: exactly three public GETs, one for each allowlisted URL. POST count:
zero. These are one-snapshot values, not night history. Turnover is therefore
zero and confidence remains LOW. The initial active candidates are Brawl Stars
and Clash of Clans; Fortnite fails the parseable-ratio gate (5.6%). This can
change only after real subsequent observations and hysteresis confirmation.

The most important finding is that large generic cohorts still have very wide
within-cohort price dispersion (for example, the largest Brawl cohort had
dispersion about 4.70). MOPS does penalize this, but tighter progression bands
or more structured features may be needed before any later Account Flip Score.
