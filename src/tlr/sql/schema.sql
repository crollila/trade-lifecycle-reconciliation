-- Trade lifecycle & reconciliation store. Plain SQL types that port directly to
-- PostgreSQL. All timestamps are UTC (TIMESTAMP, no zone).
-- Operational tables are append-only: corrections add a new version row, and
-- the "current" state is a view. That makes every number auditable and replays
-- safe: nothing is ever updated in place except exception workflow state.

-- ------------------------------------------------------------ reference data
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   VARCHAR PRIMARY KEY,
    asset_class     VARCHAR NOT NULL,          -- EQUITY | BOND | FUTURE
    currency        VARCHAR NOT NULL,
    tick            DECIMAL(20, 8) NOT NULL,
    price_factor    DECIMAL(20, 8) NOT NULL,   -- notional = qty * price * price_factor
    price_tolerance DECIMAL(20, 8) NOT NULL,   -- half a tick
    description     VARCHAR,
    dormant         BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument_xref (
    source          VARCHAR NOT NULL,          -- venue / custodian
    external_code   VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL REFERENCES instruments (instrument_id),
    PRIMARY KEY (source, external_code)
);

CREATE TABLE IF NOT EXISTS account_xref (
    source          VARCHAR NOT NULL,
    external_code   VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,          -- FUND-A/B/C or BLOCK
    PRIMARY KEY (source, external_code)
);

CREATE TABLE IF NOT EXISTS marks (
    instrument_id   VARCHAR NOT NULL,
    mark_date       DATE NOT NULL,
    price           DECIMAL(20, 8) NOT NULL,
    PRIMARY KEY (instrument_id, mark_date)
);

-- --------------------------------------------------------- order lifecycle
CREATE TABLE IF NOT EXISTS orders (
    client_order_id VARCHAR PRIMARY KEY,
    trade_date      DATE NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    venue           VARCHAR NOT NULL,
    location        VARCHAR NOT NULL,          -- where the trade settles / is held
    allocation_rule VARCHAR NOT NULL,          -- e.g. FUND-A:0.5,FUND-B:0.3,FUND-C:0.2
    created_at      TIMESTAMP NOT NULL
);

-- every message that reached the gateway, including ignored replays
CREATE TABLE IF NOT EXISTS ingest_log (
    venue           VARCHAR NOT NULL,
    message_id      VARCHAR NOT NULL,
    content_hash    VARCHAR NOT NULL,
    exec_type       VARCHAR NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    outcome         VARCHAR NOT NULL           -- ACCEPTED | REPLAY_IGNORED | CONFLICT_QUARANTINED
);

CREATE TABLE IF NOT EXISTS order_events (
    venue           VARCHAR NOT NULL,
    client_order_id VARCHAR NOT NULL,
    venue_order_id  VARCHAR NOT NULL,
    exec_type       VARCHAR NOT NULL,          -- ACK | FILL | CORRECT | BUST | CANCEL | REJECT
    status          VARCHAR NOT NULL,
    event_ts        TIMESTAMP NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    source          VARCHAR NOT NULL           -- VENUE | OPS
);

-- normalized fills, append-only with versions (CORRECT / BUST add rows)
CREATE TABLE IF NOT EXISTS fills (
    venue           VARCHAR NOT NULL,
    venue_fill_id   VARCHAR NOT NULL,
    version         INTEGER NOT NULL,
    exec_type       VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    client_order_id VARCHAR NOT NULL,
    venue_order_id  VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    price           DECIMAL(20, 8) NOT NULL,
    currency        VARCHAR NOT NULL,
    fee             DECIMAL(20, 2) NOT NULL,   -- expected fee per our fee schedule
    trade_ts        TIMESTAMP NOT NULL,
    trade_date      DATE NOT NULL,
    settlement_date DATE NOT NULL,
    status          VARCHAR NOT NULL,
    recorded_at     TIMESTAMP NOT NULL,
    is_busted       BOOLEAN NOT NULL,
    PRIMARY KEY (venue, venue_fill_id, version)
);

CREATE TABLE IF NOT EXISTS allocations (
    alloc_id        VARCHAR NOT NULL,
    version         INTEGER NOT NULL,
    action          VARCHAR NOT NULL,          -- NEW | AMEND | CANCEL
    client_order_id VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    location        VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    gross           DECIMAL(20, 2) NOT NULL,
    fee             DECIMAL(20, 2) NOT NULL,
    accrued         DECIMAL(20, 2) NOT NULL,
    avg_price       DECIMAL(24, 10) NOT NULL,
    currency        VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    settle_date     DATE NOT NULL,
    booked_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (alloc_id, version)
);

CREATE TABLE IF NOT EXISTS settlement_instructions (
    ssi_id          VARCHAR PRIMARY KEY,
    alloc_id        VARCHAR NOT NULL,
    version         INTEGER NOT NULL,
    action          VARCHAR NOT NULL,          -- NEW | AMEND | CANCEL
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    direction       VARCHAR NOT NULL,          -- RVP (receive vs payment) | DVP (deliver vs payment)
    quantity        DECIMAL(20, 4) NOT NULL,
    settlement_amount DECIMAL(20, 2) NOT NULL,
    currency        VARCHAR NOT NULL,
    settle_date     DATE NOT NULL,
    sent_at         TIMESTAMP NOT NULL
);

-- ---------------------------------------------------- external (counterparty)
CREATE TABLE IF NOT EXISTS ext_feed_files (
    venue           VARCHAR NOT NULL,
    file_seq        INTEGER NOT NULL,
    coverage_end    TIMESTAMP NOT NULL,        -- the venue vouches for all trades up to here
    records         INTEGER NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (venue, file_seq)
);

CREATE TABLE IF NOT EXISTS ext_trades (
    venue           VARCHAR NOT NULL,
    venue_fill_id   VARCHAR NOT NULL,
    ingest_seq      INTEGER NOT NULL,          -- later rows supersede earlier ones
    venue_order_id  VARCHAR NOT NULL,
    client_order_id VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    price           DECIMAL(20, 8) NOT NULL,
    currency        VARCHAR NOT NULL,
    fee             DECIMAL(20, 2) NOT NULL,   -- fee the venue billed
    trade_ts        TIMESTAMP NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (venue, venue_fill_id, ingest_seq)
);

CREATE TABLE IF NOT EXISTS confirmations (
    venue           VARCHAR NOT NULL,
    alloc_id        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    avg_price       DECIMAL(24, 10) NOT NULL,
    settle_date     DATE NOT NULL,
    received_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_status (
    location        VARCHAR NOT NULL,
    alloc_id        VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,          -- SETTLED | FAILED
    reason          VARCHAR,
    status_date     DATE NOT NULL,
    received_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS ext_positions (
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    business_date   DATE NOT NULL,
    as_of           DATE NOT NULL,             -- < business_date means the statement is stale
    statement_id    VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL,
    received_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS ext_cash (
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    business_date   DATE NOT NULL,
    as_of           DATE NOT NULL,
    statement_id    VARCHAR NOT NULL,
    currency        VARCHAR NOT NULL,
    balance         DECIMAL(20, 2) NOT NULL,
    received_at     TIMESTAMP NOT NULL
);

-- ----------------------------------------------------------- reconciliation
CREATE TABLE IF NOT EXISTS exceptions (
    exc_id          VARCHAR PRIMARY KEY,
    break_type      VARCHAR NOT NULL,
    family          VARCHAR NOT NULL,          -- TRADE | FEE | CONFIRM | SETTLEMENT | POSITION | CASH
    break_key       VARCHAR NOT NULL,
    severity        VARCHAR NOT NULL,          -- CRITICAL | HIGH | MEDIUM | LOW
    owner           VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,
    as_of           DATE,
    venue           VARCHAR,
    location        VARCHAR,
    account         VARCHAR,
    instrument_id   VARCHAR,
    client_order_id VARCHAR,
    expected        VARCHAR,
    actual          VARCHAR,
    difference      DECIMAL(24, 4),
    impact_usd      DECIMAL(20, 2),
    source_records  VARCHAR,                   -- JSON list of the records compared
    related_keys    VARCHAR,
    resolution_action VARCHAR,
    opened_at       TIMESTAMP NOT NULL,
    last_seen_at    TIMESTAMP NOT NULL,
    closed_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exception_audit (
    exc_id          VARCHAR NOT NULL,
    seq             INTEGER NOT NULL,
    event_ts        TIMESTAMP NOT NULL,
    actor           VARCHAR NOT NULL,          -- RECON_ENGINE | OPS:<desk>
    action          VARCHAR NOT NULL,
    from_status     VARCHAR,
    to_status       VARCHAR,
    note            VARCHAR,
    PRIMARY KEY (exc_id, seq)
);

CREATE TABLE IF NOT EXISTS recon_runs (
    run_id          INTEGER PRIMARY KEY,
    run_ts          TIMESTAMP NOT NULL,
    kind            VARCHAR NOT NULL,          -- TRADE | EOD | CONFIRM (+ _RERUN)
    as_of           DATE,
    records_compared INTEGER NOT NULL,
    breaks_observed INTEGER NOT NULL,
    new_exceptions  INTEGER NOT NULL,
    closed_exceptions INTEGER NOT NULL,
    open_exceptions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exception_status_history (
    run_id          INTEGER NOT NULL,
    sample_ts       TIMESTAMP NOT NULL,
    status          VARCHAR NOT NULL,
    n               INTEGER NOT NULL
);

-- daily book snapshots taken by the measurement harness (internal / external / expected)
CREATE TABLE IF NOT EXISTS position_snapshots (
    run_mode        VARCHAR NOT NULL,          -- recon_on | recon_off
    as_of           DATE NOT NULL,
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    internal_td     DECIMAL(20, 4) NOT NULL,
    internal_sd     DECIMAL(20, 4) NOT NULL,
    external_sd     DECIMAL(20, 4),
    expected_td     DECIMAL(20, 4) NOT NULL,
    expected_sd     DECIMAL(20, 4) NOT NULL,
    mark            DECIMAL(20, 8) NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_snapshots (
    run_mode        VARCHAR NOT NULL,
    as_of           DATE NOT NULL,
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    currency        VARCHAR NOT NULL,
    internal_sd     DECIMAL(20, 2) NOT NULL,
    external_sd     DECIMAL(20, 2),
    expected_sd     DECIMAL(20, 2) NOT NULL,
    internal_td     DECIMAL(20, 2) NOT NULL,
    expected_td     DECIMAL(20, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_daily (
    run_mode        VARCHAR NOT NULL,
    as_of           DATE NOT NULL,
    account         VARCHAR NOT NULL,
    internal_nav_usd DECIMAL(20, 2) NOT NULL,
    expected_nav_usd DECIMAL(20, 2) NOT NULL,
    internal_pnl_usd DECIMAL(20, 2) NOT NULL,
    expected_pnl_usd DECIMAL(20, 2) NOT NULL
);

-- ground truth for scoring only; the recon engine never reads this table
CREATE TABLE IF NOT EXISTS scoring_incidents (
    incident_id     VARCHAR PRIMARY KEY,
    kind            VARCHAR NOT NULL,
    expected_break  VARCHAR,
    observable_at   TIMESTAMP NOT NULL,
    match_keys      VARCHAR NOT NULL,
    description     VARCHAR,
    material        BOOLEAN NOT NULL,
    detected        BOOLEAN,
    detected_as     VARCHAR,
    exc_id          VARCHAR,
    detection_latency_min DOUBLE
);

CREATE TABLE IF NOT EXISTS sim_clock (now TIMESTAMP NOT NULL);

CREATE TABLE IF NOT EXISTS opening_positions (
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    instrument_id   VARCHAR NOT NULL,
    quantity        DECIMAL(20, 4) NOT NULL
);

CREATE TABLE IF NOT EXISTS opening_cash (
    location        VARCHAR NOT NULL,
    account         VARCHAR NOT NULL,
    currency        VARCHAR NOT NULL,
    balance         DECIMAL(20, 2) NOT NULL
);
