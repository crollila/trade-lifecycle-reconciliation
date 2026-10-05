# SQL examples

Every query below runs against `out/tlr.duckdb` (baseline run, reconciliation enabled) and the result tables are pasted by `python -m tlr all`, so they always match the code. Views are defined in [`src/tlr/sql/views.sql`](src/tlr/sql/views.sql); tables in [`src/tlr/sql/schema.sql`](src/tlr/sql/schema.sql).

Open the database yourself with `duckdb out/tlr.duckdb` or `python -c "import duckdb; print(duckdb.connect('out/tlr.duckdb').sql('SELECT * FROM v_recon_summary'))"`.

## 1. Net positions by location (trade-date)

Opening holdings plus every current allocation, netted per custody location, account and instrument.

```sql
SELECT * FROM v_net_positions WHERE instrument_id IN ('ACME', 'SYNT-4.25-2031', 'SYX-H26') ORDER BY instrument_id, location, account
```

| location | account | instrument_id | net_qty |
|---|---|---|---|
| MERIDIAN | FUND-A | ACME | 243,660 |
| MERIDIAN | FUND-B | ACME | 363,140 |
| MERIDIAN | FUND-C | ACME | 349,100 |
| NORTHBRIDGE | FUND-A | ACME | 538,500 |
| NORTHBRIDGE | FUND-B | ACME | 482,640 |
| NORTHBRIDGE | FUND-C | ACME | 440,460 |
| NORTHBRIDGE | FUND-A | SYNT-4.25-2031 | 98,900,000 |
| NORTHBRIDGE | FUND-B | SYNT-4.25-2031 | 68,650,000 |
| NORTHBRIDGE | FUND-C | SYNT-4.25-2031 | 86,950,000 |
| MERIDIAN | FUND-A | SYX-H26 | 1,009 |
| MERIDIAN | FUND-B | SYX-H26 | -295 |
| MERIDIAN | FUND-C | SYX-H26 | 378 |

## 2. Cross-venue net position

The same instrument can be held at more than one place (ACME trades through AlphaX, settling at Northbridge, and through the Meridian prime broker). This rolls every location up to one firm-wide number.

```sql
SELECT * FROM v_cross_venue_net ORDER BY instrument_id
```

| instrument_id | northbridge_qty | meridian_qty | firm_net_qty |
|---|---|---|---|
| ACME | 1,461,600 | 955,900 | 2,417,500 |
| ACME-5.10-2030 | 299,750,000 |  | 299,750,000 |
| BOLT | 994,200 | 1,114,500 | 2,108,700 |
| CRUX | 1,188,200 | 929,400 | 2,117,600 |
| DYNA | 1,084,600 | 892,700 | 1,977,300 |
| EMBR | 1,192,600 | 799,100 | 1,991,700 |
| FLUX | 1,102,300 | 1,083,600 | 2,185,900 |
| NORD | 1,188,300 |  | 1,188,300 |
| SYN10Y-H26 |  | 1,062 | 1,062 |
| SYNB-2.50-2034 | 283,250,000 |  | 283,250,000 |
| SYNT-4.25-2031 | 254,500,000 |  | 254,500,000 |
| SYX-H26 |  | 1,092 | 1,092 |
| VELA | 1,334,300 |  | 1,334,300 |

## 3. Unmatched fills right now

The live T+0 matching view. Anything still listed at the end of the window is a break that is still open (compare with the aged exceptions below).

```sql
SELECT venue, venue_fill_id, instrument_id, internal_qty, venue_qty, expected_fee, billed_fee, match_status FROM v_unmatched_fills ORDER BY venue, venue_fill_id
```

| venue | venue_fill_id | instrument_id | internal_qty | venue_qty | expected_fee | billed_fee | match_status |
|---|---|---|---|---|---|---|---|
| BONDHUB | BHT0001061 | SYNT-4.25-2031 | 250,000 | 250,000 | 12.44 | 19.90 | FEE_DIFF |
| MERIDIAN | MRX0002319 | SYX-H26 | 5 | 5 | 12.50 | 20 | FEE_DIFF |

## 4. Unmatched fills as of a past moment (time travel)

Because every table is append-only, the matching can be replayed as of any instant. Here: one minute after the material incident was detected - it shows up as a QUANTITY_DIFF alongside whatever else was open at that moment.

```sql
WITH t AS (SELECT opened_at + INTERVAL 1 MINUTE AS asof FROM exceptions
           WHERE break_type = 'QUANTITY_MISMATCH' ORDER BY impact_usd DESC LIMIT 1),
f AS (SELECT * EXCLUDE (rn) FROM (SELECT fl.*, row_number() OVER (PARTITION BY venue, venue_fill_id ORDER BY version DESC) rn
      FROM fills fl, t WHERE fl.recorded_at <= t.asof) WHERE rn = 1 AND NOT is_busted),
x AS (SELECT * EXCLUDE (rn) FROM (SELECT e.*, row_number() OVER (PARTITION BY venue, venue_fill_id ORDER BY ingest_seq DESC) rn
      FROM ext_trades e, t WHERE e.received_at <= t.asof) WHERE rn = 1),
w AS (SELECT venue, max(coverage_end) wm FROM ext_feed_files, t WHERE received_at <= t.asof GROUP BY venue)
SELECT coalesce(f.venue, x.venue) venue, coalesce(f.venue_fill_id, x.venue_fill_id) fill_id,
       coalesce(f.instrument_id, x.instrument_id) instrument_id, f.quantity internal_qty, x.quantity venue_qty,
       f.price internal_px, x.price venue_px, f.fee expected_fee, x.fee billed_fee
FROM (SELECT f.* FROM f JOIN w USING (venue) WHERE f.trade_ts <= w.wm) f
FULL OUTER JOIN (SELECT x.* FROM x JOIN w USING (venue) WHERE x.trade_ts <= w.wm) x
  ON f.venue = x.venue AND f.venue_fill_id = x.venue_fill_id
JOIN instruments i ON i.instrument_id = coalesce(f.instrument_id, x.instrument_id)
WHERE f.venue_fill_id IS NULL OR x.venue_fill_id IS NULL OR f.quantity <> x.quantity
   OR abs(f.price - x.price) > i.price_tolerance OR f.fee <> x.fee
ORDER BY 1, 2
```

| venue | fill_id | instrument_id | internal_qty | venue_qty | internal_px | venue_px | expected_fee | billed_fee |
|---|---|---|---|---|---|---|---|---|
| ALPHAX | AXF0000072 | CRUX | 100 | 100 | 23.29 | 23.29 | 0.35 | 0.60 |
| ALPHAX | AXF0000220 | CRUX | 1,400 | 1,400 | 23.21 | 23.21 | 4.90 | 7.84 |
| ALPHAX | AXF0000987 | FLUX | 3,700 | 3,700 | 12.83 | 12.54 | 12.95 | 12.95 |
| BONDHUB | BHT0000355 | SYNT-4.25-2031 | 50,000,000 | 5,000,000 | 99.754 | 99.754 | 2,493.85 | 249.39 |
| MERIDIAN | MRX0000041 | ACME | 800 | 800 | 41.36 | 41.36 | 3.20 | 5.12 |
| MERIDIAN | MRX0000097 | EMBR | 7,700 | 7,700 | 68.13 | 68.13 | 30.80 | 49.28 |

## 5. Partial fills aggregated per order

Cumulative quantity, leaves quantity and volume-weighted average price from the current fills.

```sql
SELECT * FROM v_order_fill_summary WHERE n_fills >= 3 ORDER BY client_order_id LIMIT 8
```

| client_order_id | trade_date | instrument_id | side | venue | order_qty | n_fills | cum_qty | leaves_qty | vwap | fees |
|---|---|---|---|---|---|---|---|---|---|---|
| O0302-0000 | 2026-03-02 | BOLT | SELL | MERIDIAN | 1,000 | 5 | 1,000 | 0 | 118.807 | 4 |
| O0302-0003 | 2026-03-02 | NORD | SELL | ALPHAX | 12,100 | 3 | 12,100 | 0 | 54.761488 | 198.78 |
| O0302-0004 | 2026-03-02 | BOLT | SELL | ALPHAX | 7,800 | 3 | 7,800 | 0 | 118.751154 | 27.30 |
| O0302-0007 | 2026-03-02 | SYX-H26 | BUY | MERIDIAN | 5 | 4 | 5 | 0 | 5,002.70 | 12.50 |
| O0302-0010 | 2026-03-02 | CRUX | BUY | ALPHAX | 18,100 | 5 | 18,100 | 0 | 23.223149 | 63.35 |
| O0302-0011 | 2026-03-02 | BOLT | BUY | ALPHAX | 11,500 | 3 | 11,500 | 0 | 118.570174 | 40.25 |
| O0302-0014 | 2026-03-02 | SYX-H26 | BUY | MERIDIAN | 66 | 4 | 66 | 0 | 5,002.734848 | 165 |
| O0302-0016 | 2026-03-02 | SYN10Y-H26 | SELL | MERIDIAN | 52 | 4 | 52 | 0 | 111.924579 | 130 |

## 6. Idempotency: what happened to every inbound message

Exact replays are recognized by (venue, message id, content hash) and ignored.

```sql
SELECT outcome, exec_type, count(*) AS messages FROM ingest_log GROUP BY ALL ORDER BY outcome, exec_type
```

| outcome | exec_type | messages |
|---|---|---|
| ACCEPTED | ACK | 3158 |
| ACCEPTED | BUST | 10 |
| ACCEPTED | CANCEL | 188 |
| ACCEPTED | CORRECT | 20 |
| ACCEPTED | FILL | 8013 |
| ACCEPTED | REJECT | 42 |
| REPLAY_IGNORED | FILL | 150 |

## 7. EOD cash: internal vs custodian

Settled cash recomputed in SQL from allocations and marks (futures as variation margin) next to each custodian statement. Rows with a difference are days where a known break (failed settlement, off-schedule fee, wrong-account booking) was open.

```sql
SELECT * FROM v_eod_cash WHERE difference <> 0 ORDER BY business_date, location, account, currency
```

| business_date | location | account | currency | internal_cash | custodian_cash | difference | statement_as_of |
|---|---|---|---|---|---|---|---|
| 2026-03-03 | MERIDIAN | FUND-B | USD | 395,213,843.18 | 395,213,830.94 | -12.24 | 2026-03-03 |
| 2026-03-03 | MERIDIAN | FUND-C | USD | 406,355,403.15 | 406,355,394.99 | -8.16 | 2026-03-03 |
| 2026-03-03 | NORTHBRIDGE | FUND-A | USD | 741,460,067.07 | 741,488,257.69 | 28,190.62 | 2026-03-03 |
| 2026-03-03 | NORTHBRIDGE | FUND-B | USD | 759,179,635.61 | 759,179,634.73 | -0.88 | 2026-03-03 |
| 2026-03-03 | NORTHBRIDGE | FUND-C | USD | 752,413,804.52 | 752,385,611.59 | -28,192.93 | 2026-03-03 |
| 2026-03-04 | MERIDIAN | FUND-B | USD | 392,063,934.65 | 392,431,859.50 | 367,924.85 | 2026-03-04 |
| 2026-03-04 | MERIDIAN | FUND-C | USD | 404,070,619.82 | 403,702,693.05 | -367,926.77 | 2026-03-04 |
| 2026-03-04 | NORTHBRIDGE | FUND-A | USD | 748,606,636.93 | 747,678,135.16 | -928,501.77 | 2026-03-04 |
| 2026-03-04 | NORTHBRIDGE | FUND-B | EUR | 305,964,336.41 | 305,571,126.51 | -393,209.90 | 2026-03-04 |
| 2026-03-04 | NORTHBRIDGE | FUND-B | USD | 760,555,747.23 | 760,555,746.35 | -0.88 | 2026-03-04 |
| 2026-03-04 | NORTHBRIDGE | FUND-C | EUR | 302,773,095.45 | 303,166,305.35 | 393,209.90 | 2026-03-04 |
| 2026-03-04 | NORTHBRIDGE | FUND-C | USD | 755,150,604.66 | 755,150,603.82 | -0.84 | 2026-03-04 |
| 2026-03-05 | MERIDIAN | FUND-B | USD | 395,629,700.98 | 395,629,699.83 | -1.15 | 2026-03-05 |
| 2026-03-05 | MERIDIAN | FUND-C | USD | 398,577,139.23 | 398,577,138.46 | -0.77 | 2026-03-05 |
| 2026-03-05 | NORTHBRIDGE | FUND-A | EUR | 308,288,303.54 | 308,470,106.07 | 181,802.53 | 2026-03-05 |

_38 more rows not shown (53 total)._

## 8. Aged exceptions

Open exceptions with their age on the simulated clock at the end of the window.

```sql
SELECT * FROM v_aged_exceptions ORDER BY age_hours DESC, exc_id
```

| exc_id | break_type | severity | owner | status | break_key | impact_usd | opened_at | age_hours | age_bucket |
|---|---|---|---|---|---|---|---|---|---|
| EXC-00074 | FEE_MISMATCH | LOW | Middle Office - Fees | PENDING_EXTERNAL | FEE\|MERIDIAN\|MRX0002319 | 7.50 | 2026-03-11 21:45 | 150.20 | >3d |
| EXC-00085 | FEE_MISMATCH | LOW | Middle Office - Fees | PENDING_EXTERNAL | FEE\|BONDHUB\|BHT0001061 | 7.46 | 2026-03-13 17:15 | 106.70 | >3d |

## 9. Reconciliation summary

Every exception raised, how it closed and how long it took.

```sql
SELECT * FROM v_recon_summary
```

| break_type | raised | resolved_by_ops | auto_cleared | still_open | median_hours_to_close | total_impact_usd |
|---|---|---|---|---|---|---|
| FEE_MISMATCH | 12 | 10 | 0 | 2 | 112.10 | 156.67 |
| MISSING_TRADE | 12 | 12 | 0 | 0 | 1.60 | 8,399,008.71 |
| DUPLICATE_FILL | 10 | 10 | 0 | 0 | 1.50 | 45,599,203.82 |
| LATE_CONFIRMATION | 10 | 0 | 10 | 0 | 71 | 13,845,925.84 |
| PRICE_MISMATCH | 10 | 10 | 0 | 0 | 1.80 | 37,390.44 |
| QUANTITY_MISMATCH | 10 | 10 | 0 | 0 | 1 | 48,019,332.07 |
| FAILED_SETTLEMENT | 8 | 0 | 8 | 0 | 48 | 9,288,696.26 |
| WRONG_ACCOUNT_ALLOCATION | 6 | 6 | 0 | 0 | 24 | 1,477,283.69 |
| STALE_POSITION | 5 | 5 | 0 | 0 | 15 | 0 |
| UNEXPECTED_POSITION | 5 | 5 | 0 | 0 | 95 | 6,354,970 |

## 10. Audit history of the material exception

Every state change, who made it and why.

```sql
SELECT a.seq, a.event_ts, a.actor, a.action, a.from_status, a.to_status, a.note
FROM exception_audit a JOIN (SELECT exc_id FROM exceptions WHERE break_type = 'QUANTITY_MISMATCH'
                              ORDER BY impact_usd DESC LIMIT 1) e USING (exc_id)
ORDER BY a.seq
```

| seq | event_ts | actor | action | from_status | to_status | note |
|---|---|---|---|---|---|---|
| 1 | 2026-03-04 16:15 | RECON_ENGINE | CREATED | OPEN | OPEN | QUANTITY_MISMATCH: expected qty 5000000.0000; actual qty 50000000.0000 |
| 2 | 2026-03-04 16:38 | OPS:Trade Support | PICKED_UP | OPEN | INVESTIGATING | assigned analyst began investigation |
| 3 | 2026-03-04 17:14 | OPS:Trade Support | FIX_APPLIED | INVESTIGATING | FIX_APPLIED | Corrected internal fill quantity to venue-confirmed quantity |
| 4 | 2026-03-04 17:15 | RECON_ENGINE | VERIFIED_CLEAR | FIX_APPLIED | RESOLVED | re-run of the reconciliation no longer observes the break |

## 11. One order through the whole lifecycle

Order -> acknowledgement -> partial fills -> allocations -> settlement instructions -> confirmations -> settlement. Timestamps are UTC (New York 17:00 = 22:00 UTC before the DST change).

```sql
WITH o AS (SELECT client_order_id FROM v_order_fill_summary s JOIN orders USING (client_order_id)
           WHERE n_fills >= 2 AND s.venue = 'BONDHUB' AND allocation_rule LIKE '%FUND-C%' ORDER BY client_order_id LIMIT 1)
SELECT 'order event' AS stage, e.exec_type AS detail, e.status, CAST(e.event_ts AS VARCHAR) AS ts FROM order_events e JOIN o USING (client_order_id)
UNION ALL SELECT 'allocation', a.account || ' ' || CAST(a.quantity AS BIGINT), a.action, CAST(a.booked_at AS VARCHAR) FROM allocations a JOIN o USING (client_order_id)
UNION ALL SELECT 'settlement instruction', s.direction || ' ' || s.account || ' ' || CAST(s.quantity AS BIGINT), s.action, CAST(s.sent_at AS VARCHAR)
  FROM settlement_instructions s JOIN allocations a ON a.alloc_id = s.alloc_id AND a.version = s.version JOIN o USING (client_order_id)
UNION ALL SELECT 'confirmation', c.account || ' ' || CAST(c.quantity AS BIGINT), 'RECEIVED', CAST(c.received_at AS VARCHAR)
  FROM confirmations c WHERE c.alloc_id IN (SELECT alloc_id FROM allocations JOIN o USING (client_order_id))
UNION ALL SELECT 'settlement', st.alloc_id, st.status, CAST(st.received_at AS VARCHAR)
  FROM settlement_status st WHERE st.alloc_id IN (SELECT alloc_id FROM allocations JOIN o USING (client_order_id))
ORDER BY ts, stage, detail
```

| stage | detail | status | ts |
|---|---|---|---|
| order event | ACK | ACKNOWLEDGED | 2026-03-02 17:03:52 |
| order event | FILL | PARTIALLY_FILLED | 2026-03-02 17:04:00 |
| order event | FILL | FILLED | 2026-03-02 17:07:37 |
| allocation | FUND-A 500000 | NEW | 2026-03-02 22:00:00 |
| allocation | FUND-B 300000 | NEW | 2026-03-02 22:00:00 |
| allocation | FUND-C 200000 | NEW | 2026-03-02 22:00:00 |
| settlement instruction | RVP FUND-A 500000 | NEW | 2026-03-02 22:00:00 |
| settlement instruction | RVP FUND-B 300000 | NEW | 2026-03-02 22:00:00 |
| settlement instruction | RVP FUND-C 200000 | NEW | 2026-03-02 22:00:00 |
| confirmation | FUND-B 300000 | RECEIVED | 2026-03-02 22:38:01 |
| confirmation | FUND-A 500000 | RECEIVED | 2026-03-02 23:33:34 |
| confirmation | FUND-C 200000 | RECEIVED | 2026-03-02 23:58:01 |
| settlement | O0302-0025-FUND-A | SETTLED | 2026-03-03 21:03:09 |
| settlement | O0302-0025-FUND-C | SETTLED | 2026-03-03 21:04:25 |
| settlement | O0302-0025-FUND-B | SETTLED | 2026-03-03 21:05:00 |
