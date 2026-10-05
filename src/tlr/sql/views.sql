-- Derived views. Everything "current" is computed from append-only history.

-- latest version of every fill; busted fills drop out
CREATE OR REPLACE VIEW v_fills_current AS
SELECT * EXCLUDE (rn) FROM (
    SELECT f.*, row_number() OVER (PARTITION BY venue, venue_fill_id ORDER BY version DESC) AS rn
    FROM fills f
) WHERE rn = 1 AND NOT is_busted;

-- latest venue-reported record of every trade (corrections supersede)
CREATE OR REPLACE VIEW v_ext_trades_current AS
SELECT * EXCLUDE (rn) FROM (
    SELECT e.*, row_number() OVER (PARTITION BY venue, venue_fill_id ORDER BY ingest_seq DESC) AS rn
    FROM ext_trades e
) WHERE rn = 1;

-- how far each venue has vouched for its trades
CREATE OR REPLACE VIEW v_venue_watermark AS
SELECT venue, max(coverage_end) AS watermark FROM ext_feed_files GROUP BY venue;

-- T+0 trade matching: internal fills vs venue drop copy, inside the watermark
CREATE OR REPLACE VIEW v_unmatched_fills AS
WITH i AS (
    SELECT f.* FROM v_fills_current f JOIN v_venue_watermark w USING (venue) WHERE f.trade_ts <= w.watermark
), e AS (
    SELECT x.* FROM v_ext_trades_current x JOIN v_venue_watermark w USING (venue) WHERE x.trade_ts <= w.watermark
)
SELECT
    coalesce(i.venue, e.venue)                 AS venue,
    coalesce(i.venue_fill_id, e.venue_fill_id) AS venue_fill_id,
    coalesce(i.client_order_id, e.client_order_id) AS client_order_id,
    coalesce(i.venue_order_id, e.venue_order_id) AS venue_order_id,
    coalesce(i.instrument_id, e.instrument_id) AS instrument_id,
    i.quantity AS internal_qty, e.quantity AS venue_qty,
    i.price    AS internal_price, e.price AS venue_price,
    i.fee      AS expected_fee, e.fee AS billed_fee,
    coalesce(i.trade_ts, e.trade_ts) AS trade_ts,
    CASE
        WHEN i.venue_fill_id IS NULL THEN 'MISSING_INTERNALLY'
        WHEN e.venue_fill_id IS NULL THEN 'MISSING_AT_VENUE'
        WHEN i.quantity <> e.quantity THEN 'QUANTITY_DIFF'
        WHEN abs(i.price - e.price) > ins.price_tolerance THEN 'PRICE_DIFF'
        WHEN i.fee <> e.fee THEN 'FEE_DIFF'
    END AS match_status
FROM i
FULL OUTER JOIN e ON i.venue = e.venue AND i.venue_fill_id = e.venue_fill_id
JOIN instruments ins ON ins.instrument_id = coalesce(i.instrument_id, e.instrument_id)
WHERE i.venue_fill_id IS NULL OR e.venue_fill_id IS NULL OR i.quantity <> e.quantity
   OR abs(i.price - e.price) > ins.price_tolerance OR i.fee <> e.fee;

-- partial fills aggregated per order
CREATE OR REPLACE VIEW v_order_fill_summary AS
SELECT o.client_order_id, o.trade_date, o.instrument_id, o.side, o.venue,
       o.quantity                                   AS order_qty,
       count(f.venue_fill_id)                       AS n_fills,
       coalesce(sum(f.quantity), 0)                 AS cum_qty,
       o.quantity - coalesce(sum(f.quantity), 0)    AS leaves_qty,
       sum(f.quantity * f.price) / nullif(sum(f.quantity), 0) AS vwap,
       coalesce(sum(f.fee), 0)                      AS fees
FROM orders o LEFT JOIN v_fills_current f USING (client_order_id)
GROUP BY o.client_order_id, o.trade_date, o.instrument_id, o.side, o.venue, o.quantity;

CREATE OR REPLACE VIEW v_allocations_current AS
SELECT * EXCLUDE (rn) FROM (
    SELECT a.*, row_number() OVER (PARTITION BY alloc_id ORDER BY version DESC) AS rn FROM allocations a
) WHERE rn = 1 AND action <> 'CANCEL';

-- trade-date net position per location / account / instrument
CREATE OR REPLACE VIEW v_net_positions AS
WITH flows AS (
    SELECT location, account, instrument_id, quantity AS qty FROM opening_positions
    UNION ALL
    SELECT location, account, instrument_id,
           CASE side WHEN 'BUY' THEN quantity ELSE -quantity END
    FROM v_allocations_current
)
SELECT location, account, instrument_id, sum(qty) AS net_qty
FROM flows GROUP BY ALL HAVING sum(qty) <> 0;

-- one row per instrument: where the position is held, and the firm-wide net
CREATE OR REPLACE VIEW v_cross_venue_net AS
SELECT instrument_id,
       sum(net_qty) FILTER (WHERE location = 'NORTHBRIDGE') AS northbridge_qty,
       sum(net_qty) FILTER (WHERE location = 'MERIDIAN')    AS meridian_qty,
       sum(net_qty)                                         AS firm_net_qty
FROM v_net_positions GROUP BY instrument_id;

-- latest statement received per location / account / business date
CREATE OR REPLACE VIEW v_latest_statement AS
SELECT location, account, business_date, statement_id, as_of FROM (
    SELECT DISTINCT location, account, business_date, statement_id, as_of, received_at,
           dense_rank() OVER (PARTITION BY location, account, business_date ORDER BY received_at DESC, statement_id DESC) AS rk
    FROM ext_cash
) WHERE rk = 1;

-- internal settled cash per business date, recomputed in SQL, vs the custodian
CREATE OR REPLACE VIEW v_eod_cash AS
WITH days AS (SELECT DISTINCT business_date FROM ext_cash),
flows AS (
    SELECT d.business_date, a.location, a.account, a.currency,
           CASE
             WHEN i.asset_class = 'FUTURE' AND a.trade_date <= d.business_date THEN
                 round((CASE a.side WHEN 'BUY' THEN 1 ELSE -1 END)
                       * (a.quantity * m.price * i.price_factor - a.gross), 2) - a.fee
             WHEN i.asset_class <> 'FUTURE' AND a.settle_date <= d.business_date THEN
                 -(CASE a.side WHEN 'BUY' THEN 1 ELSE -1 END) * (a.gross + a.accrued) - a.fee
             ELSE 0
           END AS amount
    FROM v_allocations_current a
    JOIN instruments i USING (instrument_id)
    CROSS JOIN days d
    LEFT JOIN marks m ON m.instrument_id = a.instrument_id AND m.mark_date = d.business_date
),
internal AS (
    SELECT d.business_date, o.location, o.account, o.currency, o.balance AS amount
    FROM opening_cash o CROSS JOIN days d
    UNION ALL SELECT * FROM flows
),
internal_sum AS (
    SELECT business_date, location, account, currency, sum(amount) AS internal_cash
    FROM internal GROUP BY ALL
),
external AS (
    SELECT c.business_date, c.location, c.account, c.currency, c.balance AS custodian_cash, s.as_of
    FROM ext_cash c JOIN v_latest_statement s USING (location, account, business_date, statement_id)
)
SELECT n.business_date, n.location, n.account, n.currency, n.internal_cash, x.custodian_cash,
       x.custodian_cash - n.internal_cash AS difference, x.as_of AS statement_as_of
FROM internal_sum n
LEFT JOIN external x USING (business_date, location, account, currency);

-- open exceptions with their age on the simulated clock
CREATE OR REPLACE VIEW v_aged_exceptions AS
SELECT e.exc_id, e.break_type, e.severity, e.owner, e.status, e.break_key, e.impact_usd,
       e.opened_at,
       round(date_diff('minute', e.opened_at, c.now) / 60.0, 1) AS age_hours,
       CASE
         WHEN date_diff('minute', e.opened_at, c.now) < 240   THEN '0-4h'
         WHEN date_diff('minute', e.opened_at, c.now) < 1440  THEN '4-24h'
         WHEN date_diff('minute', e.opened_at, c.now) < 4320  THEN '1-3d'
         ELSE '>3d'
       END AS age_bucket
FROM exceptions e CROSS JOIN sim_clock c
WHERE e.closed_at IS NULL;

-- headline reconciliation summary by break type
CREATE OR REPLACE VIEW v_recon_summary AS
SELECT break_type,
       count(*)                                              AS raised,
       count(*) FILTER (WHERE status = 'RESOLVED')           AS resolved_by_ops,
       count(*) FILTER (WHERE status = 'AUTO_CLEARED')       AS auto_cleared,
       count(*) FILTER (WHERE closed_at IS NULL)             AS still_open,
       round(median(date_diff('minute', opened_at, closed_at)) / 60.0, 1) AS median_hours_to_close,
       sum(impact_usd)                                       AS total_impact_usd
FROM exceptions GROUP BY break_type ORDER BY raised DESC, break_type;
