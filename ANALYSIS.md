# Analysis

Every number here comes from `results/results.json` and the DuckDB files written by
`python -m tlr all` (seed 20260302). The run is deterministic, so re-running reproduces them
exactly. The data is synthetic. The point is to show *how* the controls behave and where
they stop working, not to claim a production hit rate.

## 1. Baseline: what the controls catch, and how fast

88 problems were injected across ten break types into 3,200 orders and 8,003 fills. All 88
were raised with the correct break type, no exception was raised that did not point at an
injected problem, and none of the 150 exact message replays produced an exception.

| Break type | Injected | Detected | Median latency | Max latency | What sets the latency |
|---|---:|---:|---:|---:|---|
| Missing trade | 12 | 12 | 29 min | 305 min | the venue's drop-copy cadence |
| Duplicate fill | 10 | 10 | 115 min | 264 min | same; this sample happened to fall more on BondHub and Meridian |
| Quantity mismatch | 10 | 10 | 29 min | 333 min | same |
| Price mismatch | 10 | 10 | 34 min | 302 min | same |
| Fee mismatch | 12 | 12 | 26 min | 241 min | same |
| Wrong-account allocation | 6 | 6 | 210 min | 210 min | custodian books at 16:00; statements at 19:00; recon at 19:30 |
| Stale position | 5 | 5 | 60 min | 60 min | statement at 18:30 / 19:00, recon at 19:30 |
| Failed settlement | 8 | 8 | 204 min | 208 min | status at ~16:00, recon at 19:30 |
| Late confirmation | 10 | 10 | 5 min | 5 min | cutoff 21:00, recon 21:05 |
| Unexpected position | 5 | 5 | 30 min | 60 min | statement time to the 19:30 recon |

**Trade-level detection is bounded by the counterparty, not by us.** The T+0 trade recon
runs every 15 minutes, but it can only compare trades a venue has vouched for. AlphaX
publishes every 15 minutes, BondHub hourly, Meridian at 12:00 and 17:00. A Meridian fill at
12:05 cannot be matched before the 17:30 file, which explains the ~5-hour maxima. Running the
recon more often would not help. Asking the venue for a streaming drop copy would.

**The coverage watermark is what keeps precision at 100% intraday.** Without it, every fill
executed after the latest drop copy would look "missing at the venue" for up to five hours.
In this run that would be thousands of transient false positives per day. Each file carries
the time up to which the venue vouches for its trades, and only trades before it are compared.

**Custody breaks are found the evening they become visible.** Wrong-account bookings and fails
both appear on the 16:00 custodian activity and are raised at 19:30. Most then wait for the
08:00 ops desk. That overnight gap is visible as the red band in
`recon_status_over_time.png` and as the jump in the exception-aging chart.

## 2. Without reconciliation, errors do not stay small

The recon-disabled run has the same orders, fills and incidents, with the reconciliation
engine and ops desk switched off. At the end of the window:

| Gross error vs expected state | Recon disabled | Recon enabled |
|---|---:|---:|
| Positions (USD at marks) | $92.5M | $0 |
| Settled cash | $53.3M | $0 |
| P&L (sum over funds, absolute) | $1.1M | $0 |
| Custodian cash vs expected | $2.3M | $15 |

The disabled run's error grows over time (`daily_differences.recon_off` in results.json):
$1.5M of position error after day 1, $48M after the material incident on day 3, $91M by day 8.
Errors are never removed, only added. Every unresolved trade break also reaches settled cash
one or two business days later. The custodian-side errors (wrong-account bookings,
unexpected positions, unrefunded fees) also stay uncorrected, because nobody asked the
custodian to fix them.

With reconciliation enabled, the books are not error-free at every moment. The 22:00 daily
snapshots show transient differences (up to about $8M of position on one evening), because
breaks found at 19:30 wait for the next morning. Every one of them is closed by the end of the
window. The remaining $15 is two fee disputes where the venue's credit had not arrived by the
last day. Those two exceptions are correctly still open (`PENDING_EXTERNAL`, aged more than
3 days).

### The material incident

A 5,000,000-face sale of `SYNT-4.25-2031` is booked as 50,000,000 (a 10x fat-finger in the
real-time BondHub message; the venue's own file is correct).

* **Recon enabled.** It was raised as a `CRITICAL` `QUANTITY_MISMATCH` 23 minutes after the
  trade (at the first trade recon after BondHub's hourly file covered it). Trade Support
  corrected the fill, the 17:00 allocation used the corrected quantity, and the re-run closed
  the exception about 1h40m after the trade. Our books never carried the error into a
  settlement instruction.
* **Recon disabled.** The 45,000,000 face difference flows into the allocation (every fund in
  the block over-sells), into the delivery instructions, into the trade-date position the same day,
  into settled cash on T+1 (+$45.5M: cash we expect to receive for bonds we never sold), and
  into every day's P&L as the bond reprices. It was still there on the last day of the
  window. In a real firm, that $45M phantom cash balance is exactly the kind of number that
  gets lent out overnight or used to size the next trade.

## 3. Stress: where rule-based classification breaks

The stress scenario triples every incident count (258 incidents) and removes the placement
rule that kept root causes apart. Recall stays at 100%: every incident is eventually raised
with the right type. Precision falls to 94.1%: 270 exceptions were raised, 254 point at the
right incident with the right type, **9** are residual `CASH_MISMATCH` exceptions matching no
incident, and **7** are residual `POSITION_MISMATCH` exceptions on positions that *were*
affected by a real incident but were labelled wrongly. Three mechanisms explain all 16.

**1. A stale statement hides half of a pair.** A wrong-account booking shows up as two equal
and opposite residuals in two funds. On 6 March, the custodian sent a stale statement for
FUND-B on the same evening it booked one of FUND-B's bond settlements into FUND-A. The engine
correctly refused to reconcile FUND-B's stale data, so it saw only FUND-A's half. A one-sided
residual cannot be a wrong-account booking under the rule, so it became `POSITION_MISMATCH`
(plus a matching `CASH_MISMATCH`). When FUND-B's next fresh statement arrived on Monday the
pair re-formed and was correctly raised as `WRONG_ACCOUNT_ALLOCATION`, 66 hours after the
booking. The same mechanism delayed an unexpected-position detection from Friday evening to
Monday morning, when the stale FUND-A statement it coincided with was re-sent.

**2. Two custody breaks in the same instrument and fund stop the residuals from pairing.**
In this run, this was mostly two wrong-account bookings in the same instrument on the same or
consecutive days that both touched one fund (for example, two `ACME-5.10-2030` allocations
misbooked on 3 March, one out of FUND-B and one into it). The fund's residual is then the sum
of two moves, so no single allocation explains it. The engine pairs what it can, and the
leftover becomes `POSITION_MISMATCH` plus a `CASH_MISMATCH` for the money that moved with it.
These auto-cleared within one to two business days, once the custodian journaled one of the
bookings and the other became explainable on its own.

**3. Duplicate injections collapse into one exception.** With placement rules off, the
injector sometimes put two incidents on the same key: two stale-statement incidents on the
same Meridian statement, or two wrong-account bookings between the same pair of funds in the
same instrument. They are one break from the point of view of the records, so they produce one
exception (18 wrong-account incidents → 15 exceptions; 15 stale → 14). This is correct
behaviour. It is why "raised" is below "injected" for those rows in `breaks_by_type.png`.

**What would fix it.** Mechanisms 1 and 2 are the classic limits of deterministic matching
rules. Practical mitigations, in order of value:

1. Suppress residual position/cash breaks for any fund/instrument that has *another* open
   custody break, and re-classify on the next fresh statement. This trades latency for
   precision.
2. Allow N-way netting: search for any subset of open allocations whose re-attribution
   explains the residual set, instead of only pairwise.
3. Score candidate explanations (e.g. by how many records each explains) instead of applying
   rules in a fixed order.

None of these is implemented. The residual types exist so the system says "I don't know"
instead of guessing, and in this run every residual exception closed itself once the
underlying causes were resolved.

## 4. Things that are deliberately *not* breaks

All of these happen in the simulation, and each one has a test or a run-level assertion
showing it produces zero exceptions:

* **Exact replays** (150): absorbed by the `(venue, message id, content hash)` gate and logged
  as `REPLAY_IGNORED`.
* **Binary-float price artefacts** (`41.9200000002` on AlphaX, JSON float prices from
  Meridian): snapped back to the instrument's tick grid in the adapter.
* **Daylight-saving time.** BondHub sends New York local time without an offset. Trades on
  6 March are UTC-5 and trades on 9 March are UTC-4. A naive parser would put every Monday
  trade an hour off and outside the venue's coverage window.
* **Dealer-perspective sides** (BondHub) and **signed quantities** (Meridian).
* **Drop-copy lag**: handled by the coverage watermark.
* **Different drop-copy and real-time layouts from the same venue** (BondHub's two CSVs).

## 5. Operational picture

* 68 of 88 baseline exceptions were resolved by an ops action that recon then verified. 18
  cleared on their own (late confirmations arriving, failing trades settling). 2 were still
  open at the end of the window.
* Median time to close (`v_recon_summary`): about 1 to 2 hours for trade breaks we fix
  ourselves, 15 hours for stale statements (re-sent next morning), 24 hours for wrong-account
  bookings (custody journal next day), 48 hours for fails, 71 hours for late confirmations,
  95 hours for unexpected positions, and about 112 hours for fee disputes. Anything that needs
  a counterparty to act is slow.
* Severity is driven by USD impact: the material incident and other large bond breaks are
  `CRITICAL`; fee breaks are `LOW` no matter how many there are, which is right for the queue
  but means fee leakage should also be tracked in aggregate (`v_recon_summary.total_impact_usd`).

## 6. Threats to validity

* The incident taxonomy and the classifier were designed together. A real feed has failure
  modes not in the list: corporate actions, partial settlements, counterparty-initiated
  corrections, SSI mismatches at the depository, FX.
* The venues are always right about trade economics here. In reality the venue is sometimes
  the one in error, and resolution requires negotiation.
* Volumes, prices, fees, delays and the ops desk's response times are invented. Latency and
  aging numbers show the *shape* of the process, not a benchmark.
* All baseline incidents are isolated by construction. The stress scenario is the better guide
  to behaviour on messy data.
