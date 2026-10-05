# Data dictionary

All tables live in `out/tlr.duckdb` after `python -m tlr all`. DDL:
[`src/tlr/sql/schema.sql`](src/tlr/sql/schema.sql); views:
[`src/tlr/sql/views.sql`](src/tlr/sql/views.sql).

Conventions

* **Timestamps** are UTC `TIMESTAMP` without zone. New York local times are converted at the adapter.
* **Money** is `DECIMAL(20,2)`, prices `DECIMAL(20,8)`, quantities `DECIMAL(20,4)`. Python uses `Decimal` end to end; no floats touch money.
* **Quantity units**: shares for equities, **face amount** for bonds, contracts for futures.
* **Notional** = `quantity × price × price_factor` (`price_factor` is 1 for equities, 0.01 for bonds priced per 100, and the contract multiplier for futures).
* **Side** is `BUY` / `SELL` from *our* perspective, whatever the venue's convention.
* **Append-only**: operational tables never update rows. Corrections add a new `version` / `ingest_seq`, and "current" state is a view.
* **Account** `BLOCK` is the execution account before allocation; `FUND-A/B/C` are the synthetic funds.
* **Location** is where a position settles and is held: `NORTHBRIDGE` (custodian) or `MERIDIAN` (prime broker / FCM).
* Everything is **synthetic**.

## Reference data

### `instruments`
| column | meaning |
|---|---|
| instrument_id | internal id (e.g. `ACME`, `SYNT-4.25-2031`, `SYX-H26`) |
| asset_class | `EQUITY`, `BOND`, `FUTURE` |
| currency | `USD` or `EUR` |
| tick | minimum price increment |
| price_factor | see conventions |
| price_tolerance | reconciliation price tolerance (half a tick) |
| description | human-readable name |
| dormant | in the security master but never traded (used for unexpected-position incidents) |

### `instrument_xref`, `account_xref`
Maps each venue's or custodian's own codes (`ACME.N`, `ZZ00SYNT2031`, `MRD-100101`;
`AX-7741-BLK`, `NB-102938`, `MRD-ACC-501`) to internal ids. `source` is the venue/custodian.

### `marks`
End-of-day price per instrument and date (`mark_date`). These are futures settlement prices and
bond clean prices. Used for valuation, P&L and futures variation margin.

### `opening_positions`, `opening_cash`
Synthetic balances at the start of the window per location / account / instrument (or currency).

## Order lifecycle

### `orders`
| column | meaning |
|---|---|
| client_order_id | our order id (`O<MMDD>-<n>`) |
| trade_date | trading day (New York) |
| instrument_id, side, quantity | what we asked for |
| venue | `ALPHAX`, `BONDHUB` or `MERIDIAN` |
| location | where the resulting position settles |
| allocation_rule | target split, e.g. `FUND-A:0.5,FUND-B:0.3,FUND-C:0.2` |
| created_at | when the OMS created the order |

### `ingest_log`
One row per inbound execution message, including ignored ones.
| column | meaning |
|---|---|
| venue, message_id | the venue's unique id for the message (FIX ExecID, BondHub `RecType:TicketNo`, Meridian eventId) |
| content_hash | SHA-256 prefix of the exact raw bytes |
| exec_type | normalized event type |
| received_at | gateway receive time |
| outcome | `ACCEPTED`, `REPLAY_IGNORED` (same id, same bytes) or `CONFLICT_QUARANTINED` (same id, different bytes; never applied) |

### `order_events`
Accepted acknowledgements, fills, corrections, busts, cancels and rejects with the
normalized `status` (`ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`),
`event_ts` (venue time) and `source` (`VENUE` or `OPS` for operations repairs).

### `fills`
Versioned normalized fills. Key: (`venue`, `venue_fill_id`, `version`).
| column | meaning |
|---|---|
| venue, venue_fill_id | the venue's execution id |
| version | 1 for the original; +1 per correction or bust |
| exec_type | `FILL`, `CORRECT` or `BUST` |
| source | `VENUE` or `OPS` |
| client_order_id, venue_order_id | both sides' order ids |
| account | execution account (`BLOCK`) |
| instrument_id, side, quantity, price, currency | normalized trade economics |
| fee | **expected** fee from our negotiated schedule, per fill, rounded half-up to cents |
| trade_ts, trade_date | execution time (UTC) and New York trade date |
| settlement_date | contractual settlement date (T+0 futures, T+1 USD cash, T+2 EUR cash) |
| status | order status carried on the message |
| recorded_at | when we recorded this version |
| is_busted | true on the version that cancels the fill |

### `allocations`
Versioned per-fund bookings of each block. Key: (`alloc_id`, `version`). `alloc_id` = `<client_order_id>-<account>`.
| column | meaning |
|---|---|
| action | `NEW`, `AMEND` (re-allocation after a correction) or `CANCEL` |
| account, location, instrument_id, side, quantity | the fund's share |
| gross | notional in cents; per-fund shares sum to the block exactly |
| fee | the fund's share of block fees (exact cents) |
| accrued | bond accrued interest at the settlement date (30/360 or ACT/ACT) |
| avg_price | gross / (quantity × price_factor) |
| trade_date, settle_date, currency, booked_at | |

### `settlement_instructions`
One row per allocation version for securities that settle: `direction` `RVP` (receive vs
payment, a buy) or `DVP` (deliver vs payment, a sell), `quantity`, `settlement_amount`
(gross + accrued), `settle_date`, `sent_at`, `action`.

## External (counterparty) records

### `ext_feed_files`
Every drop copy / trade-activity file: `venue`, `file_seq`, `coverage_end` (the venue vouches
for all trades up to this time, which is the reconciliation watermark), `records`, `received_at`.

### `ext_trades`
The venue's own record of each trade, as reported in its files. Key: (`venue`,
`venue_fill_id`, `ingest_seq`); later `ingest_seq` supersedes (e.g. a fee credit). `fee` is
the fee the venue **billed**.

### `confirmations`
Allocation confirmations: `alloc_id`, `account`, `quantity`, `avg_price`, `settle_date`, `received_at`.

### `settlement_status`
Custodian status per allocation per day: `status` `SETTLED` or `FAILED`, ISO-15022-style
`reason` (`LACK` = lack of securities), `status_date`, `received_at`.

### `ext_positions`, `ext_cash`
End-of-day statement lines per `location`, `account`, `business_date`. `as_of` is the date the
balances actually reflect; `as_of < business_date` means the statement is **stale**. A re-sent
statement for the same business date has a later `received_at`.

## Reconciliation

### `exceptions`
| column | meaning |
|---|---|
| exc_id | `EXC-nnnnn` |
| break_type | `MISSING_TRADE`, `DUPLICATE_FILL`, `QUANTITY_MISMATCH`, `PRICE_MISMATCH`, `FEE_MISMATCH`, `WRONG_ACCOUNT_ALLOCATION`, `STALE_POSITION`, `FAILED_SETTLEMENT`, `LATE_CONFIRMATION`, `UNEXPECTED_POSITION`, or the residual types `POSITION_MISMATCH`, `CASH_MISMATCH` |
| family | the control that raised it: `TRADE`, `FEE`, `CONFIRM`, `SETTLEMENT`, `POSITION`, `CASH` |
| break_key | de-duplication key; the same unresolved break seen on many runs stays one exception |
| severity | `CRITICAL` ≥ $1M, `HIGH` ≥ $100k, `MEDIUM` ≥ $5k, else `LOW`, with per-type floors |
| owner | desk responsible |
| status | `OPEN`, `INVESTIGATING`, `PENDING_EXTERNAL`, `FIX_APPLIED`, `ESCALATED`, `RESOLVED`, `AUTO_CLEARED` |
| as_of | business date for end-of-day breaks |
| venue, location, account, instrument_id, client_order_id | context |
| expected, actual, difference | what should be vs what is |
| impact_usd | absolute USD size used for severity |
| source_records | JSON of the records compared |
| related_keys | JSON list of identifiers (`fill:…`, `alloc:…`, `pos:…`, `stmt:…`, `order:…`) |
| resolution_action | what operations did |
| opened_at, last_seen_at, closed_at | detection, last observation, verified close |

### `exception_audit`
Every state change: `exc_id`, `seq`, `event_ts`, `actor` (`RECON_ENGINE` or `OPS:<desk>`),
`action`, `from_status`, `to_status`, `note`.

### `recon_runs`, `exception_status_history`
One row per reconciliation run (`kind`, `as_of`, `records_compared`, `breaks_observed`,
`new_exceptions`, `closed_exceptions`, `open_exceptions`), and the queue's status counts
sampled at each run.

### `scoring_incidents` (measurement only)
Ground truth of what was injected and whether it was found: `kind`, `expected_break`,
`observable_at`, `match_keys`, `detected`, `detected_as`, `exc_id`, `detection_latency_min`.
The reconciliation engine never reads this table.

### `position_snapshots`, `cash_snapshots`, `pnl_daily` (measurement only)
Nightly comparison of our books (`internal_*`), the custodian (`external_*`) and the
contractually correct state (`expected_*`), on trade-date (`td`) and settled (`sd`) basis.
P&L is NAV change in USD (EUR at a fixed synthetic 1.08).

### `sim_clock`
The simulated time at the end of the run, which views use to compute exception age.

## Views

| view | purpose |
|---|---|
| `v_fills_current` | latest non-busted version of every fill |
| `v_ext_trades_current` | latest venue record of every trade |
| `v_venue_watermark` | latest `coverage_end` per venue |
| `v_unmatched_fills` | T+0 matching of fills vs venue records inside the watermark, with `match_status` |
| `v_order_fill_summary` | partial fills aggregated per order: cumulative / leaves quantity, VWAP, fees |
| `v_allocations_current` | latest non-cancelled allocation version |
| `v_net_positions` | trade-date net position per location / account / instrument |
| `v_cross_venue_net` | per instrument: Northbridge, Meridian and firm-wide net |
| `v_latest_statement` | the statement in force per location / account / business date |
| `v_eod_cash` | internal settled cash recomputed in SQL vs custodian cash, per business date |
| `v_aged_exceptions` | open exceptions with age and age bucket |
| `v_recon_summary` | per break type: raised, resolved, auto-cleared, open, median hours to close, impact |

## Normalized record (Python: `tlr.model.ExecEvent`)

`venue`, `account`, `client_order_id`, `venue_order_id`, `venue_fill_id`, `instrument_id`,
`side`, `quantity`, `price`, `currency`, `fee`, `trade_ts`, `settlement_date`, `status`, plus
`exec_type`, `message_id`, `content_hash`, `ref_fill_id` (for corrections and busts) and
`source`.
