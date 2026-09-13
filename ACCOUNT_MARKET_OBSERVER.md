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

`RiskEvidenceCoverage` separately records how much listing evidence exists for
access, email, recovery, transfer, warranty, and platform-linking dimensions.
Missing evidence is `LOW` confidence, not proof of safety. A low risk score may
therefore be displayed as, for example, `Risk: 14; Risk evidence: LOW`.

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

Observation Confidence describes the accumulated price/cohort history of the
visible window. Both COMPLETE and stable successful PARTIAL samples count;
FAILED, structurally invalid, count-collapsed, and coverage-collapsed samples
do not.

- LOW: elapsed < 1h or fewer than 12 stable successful samples;
- MEDIUM: elapsed >= 1h and at least 12 stable successful samples;
- HIGH: elapsed >= 6h and at least 60 stable successful samples.

Turnover Confidence is independent. It is NONE with no complete samples, and
can become LOW/MEDIUM/HIGH only from actual COMPLETE history. Thus stable
truncated data can honestly reach Observation Confidence: HIGH while remaining
Turnover Confidence: NONE. A zero turnover score with NONE is unavailable, not
evidence of zero market activity. No backfill invents night observations.

## Snapshot integrity and runtime truncation

The exact 4,000 / 2,000 / 2,000 result is classified as
`RUNTIME_TRUNCATION`, not `SMOKE_ONLY_LIMIT`. There is no `max_lots`, numeric
slice, pagination cap, or debug cap in the observer or smoke runner. FunPay's
public server response contains only those listing rows even though the active
category counters advertise more. The bundled page JavaScript progressively
reveals batches already present in the DOM; it does not fetch the missing rows.
The runtime and smoke runner use the same one-GET fetch path, so both see the
same truncated response.

Every scan stores `advertised_market_count`, `parsed_count`, `coverage_ratio`,
fetch/parse success, HTTP status/failure reason, and total scan duration. Its
quality is fail-closed:

- `COMPLETE`: transport and parsing succeeded, advertised coverage is at least
  98%, and the parsed count did not collapse below 60% of the previous complete
  scan;
- `PARTIAL`: the response parsed, but advertised coverage or the previous-scan
  count gate indicates truncation;
- `FAILED`: transport, HTTP, structural HTML, or parser validation failed.

The two thresholds are configurable. Only COMPLETE scans reconcile absence.
PARTIAL scans upsert positively observed rows but preserve unseen active rows;
they never create NEW, DISAPPEARED, or REAPPEARED. If the same active lot ID is
directly observed again, its PRICE_CHANGE and STOCK_CHANGE are safe positive
evidence and are retained, but do not become turnover evidence. FAILED scans do
not mutate lot state or create a sample. This prevents a lot crossing the
server response boundary from being mistaken for a disappearance.

## Polling and selection

After baseline data exists, eligible markets are ranked only by MOPS. At most
two are ACTIVE WATCH (default random interval 3–5 minutes); the rest are
BACKGROUND (12–15 minutes). Names receive no bonus. A challenger must exceed
an incumbent by 5 points for 3 consecutive samples before replacement, avoiding
selection thrashing. All intervals and hysteresis values are configurable.

Each market has a non-overlap lock: a due tick is skipped while that market's
previous scan is still running. Failures are isolated per market. A timeout,
429, invalid HTML, or parser exception records a `FAILED` diagnostic, preserves
existing rows, and applies configurable exponential backoff (default 5 to 30
minutes) without immediate retries; other markets continue normally.

Legacy market scanning and account observation use the same per-client FunPay
market GET pacer. Requests are serialized and normally spaced by 2-2.75 seconds
(configurable). HTTP 429 applies a shared 60-second cooldown before either
component may request another market node, while the account scheduler also
keeps its existing 5-30 minute per-market backoff.

## Telegram

- `/accountmarkets` — ranking, MOPS, observed risk, confidence, core metrics,
  and ACTIVE/BACKGROUND mode;
- `/accountmarkets brawl|bs|бравл`;
- `/accountmarkets coc|клеш|кок`;
- `/accountmarkets fortnite|fn|фортнайт`;
- `/accountmarkets summary` — only actually recorded history and top cohorts.

Every detail and summary says OBSERVATION ONLY and reports Observation
Confidence separately from Turnover Confidence. Details also include
parsed/advertised counts, coverage, snapshot quality, scan age/duration, and
risk-evidence confidence.

## FutureFlipEligibility

`NOT_READY`, `WATCH`, and `PROMISING` describe whether the market is worth a
future Account Flip Score stage. `PROMISING` requires at least MEDIUM
confidence, MOPS >= 60, observed risk <= 70, sufficient cohort size, acceptable
dispersion, non-excessive reappearance, and at least MEDIUM risk-evidence
coverage. Turnover Confidence must not be NONE; stable partial history alone
therefore remains WATCH. It never authorizes BUY.

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

Executed 2026-09-13 03:43:33–03:43:38 Europe/Moscow using
`run_account_market_smoke.py` and a disposable SQLite database:

| Market | Parsed / advertised | Coverage | Snapshot | Sellers | <=500 RUB | Classified | MOPS | Risk / evidence | Eligible |
|---|---:|---:|---|---:|---:|---:|---:|---:|---|
| Brawl Stars | 4,000 / 14,410 | 27.76% | PARTIAL | 2,015 | 1,297 | 3,569 | 83.47 | 14.29 / LOW | yes |
| Clash of Clans | 2,000 / 2,268 | 88.18% | PARTIAL | 585 | 472 | 775 | 74.08 | 13.54 / LOW | yes |
| Fortnite | 2,000 / 2,415 | 82.82% | PARTIAL | 916 | 581 | 114 | 69.82 | 23.88 / LOW | no |

Requests: exactly three public GETs, one for each allowlisted URL. POST count:
zero. All three responses were correctly rejected for reconciliation as
`PARTIAL`; none produced transition events or turnover. These are one-snapshot
values, not night history, so observation confidence remains LOW. Risk-evidence
coverage was also LOW (3.23%, 4.02%, and 8.66%, respectively), independently of
the numeric risk score. Fortnite fails the parseable-ratio gate (5.7%).

The most important finding is that large generic cohorts still have very wide
within-cohort price dispersion (for example, the largest Brawl cohort had
dispersion about 4.82). MOPS does penalize this, but tighter progression bands
or more structured features may be needed before any later Account Flip Score.
