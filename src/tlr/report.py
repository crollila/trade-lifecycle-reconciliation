"""One-command reproduction: run every scenario, score it, draw figures, regenerate docs.

Every number in README's results block and every table in SQL_EXAMPLES.md is
written here from the run itself; nothing is typed by hand.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from . import figures, metrics
from .config import SimConfig
from .pipeline import RunResult, run_simulation
from .refdata import FEE_SCHEDULE_TEXT

START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"


def _jsonable(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def md_table(df: pd.DataFrame, max_rows: int = 15) -> str:
    if df.empty:
        return "_(no rows)_"
    shown = df.head(max_rows)

    def cell(v):
        if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
            return ""
        if isinstance(v, float):
            if v.is_integer():
                return f"{v:,.0f}"
            txt = f"{v:,.6f}".rstrip("0")
            return txt + "0" * max(0, 2 - len(txt.split(".")[1]))
        if isinstance(v, pd.Timestamp):
            return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
        if isinstance(v, Decimal):
            return f"{v:,}" if v == v.to_integral_value() else f"{v:,.2f}" if abs(v.as_tuple().exponent) <= 2 else f"{v:,}"
        return str(v).replace("|", "\\|")

    lines = ["| " + " | ".join(shown.columns) + " |", "|" + "---|" * len(shown.columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in shown.itertuples(index=False)]
    if len(df) > max_rows:
        lines.append(f"\n_{len(df) - max_rows} more rows not shown ({len(df)} total)._")
    return "\n".join(lines)


def persist_scoring(r: RunResult, sc: dict) -> None:
    rows = [{**{k: v for k, v in row.items()}} for row in sc["incident_rows"]]
    r.store.replace_table("scoring_incidents", rows)


# ------------------------------------------------------------------ scenarios
def run_all(root: Path, quiet: bool = False) -> dict:
    out, res, figs = root / "out", root / "results", root / "docs" / "figures"
    for p in (out, res, figs):
        p.mkdir(parents=True, exist_ok=True)
    log = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    t0 = time.time()

    log("[1/5] baseline, reconciliation ENABLED ...")
    on = run_simulation(SimConfig(), out / "tlr.duckdb")
    sc_on = metrics.score(on)
    persist_scoring(on, sc_on)
    log("[2/5] baseline, reconciliation DISABLED ...")
    off = run_simulation(SimConfig(recon_enabled=False), out / "tlr_recon_off.duckdb")
    sc_off = metrics.score(off)
    log("[3/5] material incident in isolation, recon off / on ...")
    m_off = run_simulation(SimConfig(incidents={}, exact_replays=0, recon_enabled=False))
    m_on = run_simulation(SimConfig(incidents={}, exact_replays=0, recon_enabled=True))
    log("[4/5] stress scenario (3x overlapping incidents) ...")
    stress = run_simulation(SimConfig.stress(), out / "tlr_stress.duckdb")
    sc_stress = metrics.score(stress)
    persist_scoring(stress, sc_stress)

    log("[5/5] figures, results and docs ...")
    diff_on, diff_off = metrics.book_differences(on), metrics.book_differences(off)
    trace_off, trace_on = metrics.material_trace(m_off), metrics.material_trace(m_on)
    results = {
        "scenario": {"seed": on.cfg.seed, "window": metrics.volumes(on)["calendar_window"],
                     "trading_days": on.cfg.trading_days, "orders_per_day": on.cfg.orders_per_day,
                     "fee_schedules": FEE_SCHEDULE_TEXT},
        "volumes": metrics.volumes(on),
        "baseline": {k: v for k, v in sc_on.items() if k not in ("incident_rows", "exception_labels")},
        "stress": {k: v for k, v in sc_stress.items() if k not in ("incident_rows", "exception_labels")},
        "recon_disabled": {"exceptions_raised": sc_off["exceptions_raised"], "end_of_window": diff_off},
        "recon_enabled": {"end_of_window": diff_on},
        "daily_differences": {"recon_off": metrics.daily_differences(off), "recon_on": metrics.daily_differences(on)},
        "material_incident": {"recon_off": trace_off, "recon_on": trace_on},
    }
    # LF everywhere so files generated on Windows and Linux are byte-identical
    (res / "results.json").write_text(json.dumps(results, indent=2, default=_jsonable) + "\n", newline="\n")
    pd.DataFrame(sc_on["incident_rows"]).to_csv(res / "incidents_baseline.csv", index=False, lineterminator="\n")
    on.store.query("SELECT * EXCLUDE (source_records) FROM exceptions ORDER BY exc_id").to_csv(
        res / "exception_queue.csv", index=False, lineterminator="\n")
    on.store.query("SELECT * FROM exception_audit ORDER BY exc_id, seq").to_csv(
        res / "exception_audit.csv", index=False, lineterminator="\n")

    hist = [(ts.replace(tzinfo=timezone.utc), st, int(n)) for ts, st, n in
            on.store.rows("SELECT sample_ts, status, n FROM exception_status_history ORDER BY run_id")]
    end = on.bus.now
    figures.lifecycle_flow(figs)
    figures.breaks_by_type(sc_on, sc_stress, figs)
    figures.recon_status_over_time(hist, figs)
    figures.positions_before_after(diff_off, diff_on, figs)
    figures.exception_aging(on.queue.all, end, figs)
    figures.detection_latency(sc_on, figs)
    figures.material_propagation(trace_off, trace_on, figs)

    write_sql_examples(on.store.query, root / "SQL_EXAMPLES.md")
    update_readme(root / "README.md", results)
    for r in (on, off, stress):
        r.store.close()
    log(f"done in {time.time() - t0:.0f}s -> {res / 'results.json'}")
    return results


# ------------------------------------------------------------------ README
def results_block(r: dict) -> str:
    b, s, v = r["baseline"], r["stress"], r["volumes"]
    off, on = r["recon_disabled"]["end_of_window"], r["recon_enabled"]["end_of_window"]
    m_off, m_on = r["material_incident"]["recon_off"], r["material_incident"]["recon_on"]
    worst_off = max(m_off["daily"], key=lambda d: abs(d["cash_error_sd"]))
    last_off = m_off["daily"][-1]

    def usd(x):
        return f"${x:,.0f}"

    lines = [
        START,
        "_Generated by `python -m tlr all` from the run itself (seed "
        f"{r['scenario']['seed']}, window {r['scenario']['window']}). Do not edit by hand._",
        "",
        "| What was simulated | Count |",
        "|---|---:|",
        f"| Orders (incl. {v['rejected_orders']} rejected, {v['partially_filled_then_cancelled']} partially filled then cancelled) | {v['orders']:,} |",
        f"| Fills (partial fills; {v['orders_with_multiple_fills']:,} orders filled in more than one piece) | {v['fills']:,} |",
        f"| Venue messages delivered (FIX, CSV, JSON) | {v['venue_messages_delivered']:,} |",
        f"| Exact duplicate messages replayed / absorbed by idempotency | {r['baseline']['benign_noise_events']} / {v['exact_replays_ignored']} |",
        f"| Account allocations booked | {v['allocations']:,} |",
        f"| Custodian statements reconciled | {v['statements']} |",
        f"| Reconciliation runs | {v['recon_runs']} |",
        "",
        "| Reconciliation quality | Baseline | Stress (3x incidents, overlapping) |",
        "|---|---:|---:|",
        f"| Incidents injected | {b['incidents_injected']} | {s['incidents_injected']} |",
        f"| Recall (incidents detected with the correct break type) | {b['recall']:.1%} | {s['recall']:.1%} |",
        f"| Precision (exceptions that point at a real injected problem, correctly typed) | {b['precision']:.1%} | {s['precision']:.1%} |",
        f"| False-positive exceptions | {b['false_positives']} | {s['false_positives']} |",
        f"| Mis-typed exceptions | {b['misclassified_exceptions']} | {s['misclassified_exceptions']} |",
        f"| Exceptions caused by {b['benign_noise_events']} benign replays | {b['benign_noise_exceptions']} | {s['benign_noise_exceptions']} |",
        f"| Median / p90 detection latency (minutes) | {b['median_detection_latency_min']} / {b['p90_detection_latency_min']} | {s['median_detection_latency_min']} / {s['p90_detection_latency_min']} |",
        f"| Breaks still unresolved at end of window | {b['unresolved_at_end']} ({', '.join(f'{k} x{n}' for k, n in b['unresolved_by_type'].items()) or 'none'}) | {s['unresolved_at_end']} |",
        "",
        "| End-of-window book error vs expected state (gross, USD) | Recon disabled | Recon enabled |",
        "|---|---:|---:|",
        f"| Positions, trade date | {usd(off['position_usd']['internal_vs_expected_td'])} | {usd(on['position_usd']['internal_vs_expected_td'])} |",
        f"| Settled cash | {usd(off['cash_usd']['internal_vs_expected_sd'])} | {usd(on['cash_usd']['internal_vs_expected_sd'])} |",
        f"| P&L | {usd(off['pnl_error_usd'])} | {usd(on['pnl_error_usd'])} |",
        f"| Custodian cash vs expected (counterparty-side errors not yet fixed) | {usd(off['cash_usd']['external_vs_expected_sd'])} | {usd(on['cash_usd']['external_vs_expected_sd'])} |",
        "",
        f"**Material incident** - {m_off['incident'].replace('MATERIAL: ', '')}, on {m_off['venue']} "
        f"fill `{m_off['fill_id']}`. With reconciliation enabled it was raised as `{m_on['detected_as']}` "
        f"{m_on['detection_latency_min']:.0f} minutes after the trade and the books were corrected the same day "
        f"(closed {m_on['closed_at'][:16].replace('T', ' ')} UTC). With reconciliation disabled the error flowed "
        f"through allocation, settlement instructions, positions and cash: at the end of the window our books "
        f"still carried a {abs(last_off['position_error_td']):,.0f} face position error and a "
        f"{usd(abs(last_off['cash_error_sd']))} settled-cash error (peak {usd(abs(worst_off['cash_error_sd']))}), "
        f"with P&L misstated by {usd(abs(last_off['pnl_error_usd']))}.",
        END,
    ]
    return "\n".join(lines)


def update_readme(path: Path, results: dict) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else f"{START}\n{END}\n"
    block = results_block(results)
    new = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    path.write_text(new, encoding="utf-8", newline="\n")


def check_readme(root: Path) -> bool:
    results = json.loads((root / "results" / "results.json").read_text())
    text = (root / "README.md").read_text(encoding="utf-8")
    return results_block(results) in text


# ------------------------------------------------------------- SQL examples
SQL_EXAMPLES = [
    ("Net positions by location (trade-date)",
     "Opening holdings plus every current allocation, netted per custody location, account and instrument.",
     "SELECT * FROM v_net_positions WHERE instrument_id IN ('ACME', 'SYNT-4.25-2031', 'SYX-H26') ORDER BY instrument_id, location, account"),
    ("Cross-venue net position",
     "The same instrument can be held at more than one place (ACME trades through AlphaX, settling at Northbridge, and through the Meridian prime broker). This rolls every location up to one firm-wide number.",
     "SELECT * FROM v_cross_venue_net ORDER BY instrument_id"),
    ("Unmatched fills right now",
     "The live T+0 matching view. Anything still listed at the end of the window is a break that is still open "
     "(compare with the aged exceptions below).",
     "SELECT venue, venue_fill_id, instrument_id, internal_qty, venue_qty, expected_fee, billed_fee, match_status "
     "FROM v_unmatched_fills ORDER BY venue, venue_fill_id"),
    ("Unmatched fills as of a past moment (time travel)",
     "Because every table is append-only, the matching can be replayed as of any instant. Here: one minute after the material incident was detected - it shows up as a QUANTITY_DIFF alongside whatever else was open at that moment.",
     """WITH t AS (SELECT opened_at + INTERVAL 1 MINUTE AS asof FROM exceptions
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
ORDER BY 1, 2"""),
    ("Partial fills aggregated per order",
     "Cumulative quantity, leaves quantity and volume-weighted average price from the current fills.",
     "SELECT * FROM v_order_fill_summary WHERE n_fills >= 3 ORDER BY client_order_id LIMIT 8"),
    ("Idempotency: what happened to every inbound message",
     "Exact replays are recognized by (venue, message id, content hash) and ignored.",
     "SELECT outcome, exec_type, count(*) AS messages FROM ingest_log GROUP BY ALL ORDER BY outcome, exec_type"),
    ("EOD cash: internal vs custodian",
     "Settled cash recomputed in SQL from allocations and marks (futures as variation margin) next to each custodian statement. Rows with a difference are days where a known break (failed settlement, off-schedule fee, wrong-account booking) was open.",
     "SELECT * FROM v_eod_cash WHERE difference <> 0 ORDER BY business_date, location, account, currency"),
    ("Aged exceptions",
     "Open exceptions with their age on the simulated clock at the end of the window.",
     "SELECT * FROM v_aged_exceptions ORDER BY age_hours DESC, exc_id"),
    ("Reconciliation summary",
     "Every exception raised, how it closed and how long it took.",
     "SELECT * FROM v_recon_summary"),
    ("Audit history of the material exception",
     "Every state change, who made it and why.",
     """SELECT a.seq, a.event_ts, a.actor, a.action, a.from_status, a.to_status, a.note
FROM exception_audit a JOIN (SELECT exc_id FROM exceptions WHERE break_type = 'QUANTITY_MISMATCH'
                              ORDER BY impact_usd DESC LIMIT 1) e USING (exc_id)
ORDER BY a.seq"""),
    ("One order through the whole lifecycle",
     "Order -> acknowledgement -> partial fills -> allocations -> settlement instructions -> confirmations -> settlement. Timestamps are UTC (New York 17:00 = 22:00 UTC before the DST change).",
     """WITH o AS (SELECT client_order_id FROM v_order_fill_summary s JOIN orders USING (client_order_id)
           WHERE n_fills >= 2 AND s.venue = 'BONDHUB' AND allocation_rule LIKE '%FUND-C%' ORDER BY client_order_id LIMIT 1)
SELECT 'order event' AS stage, e.exec_type AS detail, e.status, CAST(e.event_ts AS VARCHAR) AS ts FROM order_events e JOIN o USING (client_order_id)
UNION ALL SELECT 'allocation', a.account || ' ' || CAST(a.quantity AS BIGINT), a.action, CAST(a.booked_at AS VARCHAR) FROM allocations a JOIN o USING (client_order_id)
UNION ALL SELECT 'settlement instruction', s.direction || ' ' || s.account || ' ' || CAST(s.quantity AS BIGINT), s.action, CAST(s.sent_at AS VARCHAR)
  FROM settlement_instructions s JOIN allocations a ON a.alloc_id = s.alloc_id AND a.version = s.version JOIN o USING (client_order_id)
UNION ALL SELECT 'confirmation', c.account || ' ' || CAST(c.quantity AS BIGINT), 'RECEIVED', CAST(c.received_at AS VARCHAR)
  FROM confirmations c WHERE c.alloc_id IN (SELECT alloc_id FROM allocations JOIN o USING (client_order_id))
UNION ALL SELECT 'settlement', st.alloc_id, st.status, CAST(st.received_at AS VARCHAR)
  FROM settlement_status st WHERE st.alloc_id IN (SELECT alloc_id FROM allocations JOIN o USING (client_order_id))
ORDER BY ts, stage, detail"""),
]


def write_sql_examples(query, path: Path) -> None:
    """``query``: callable SQL -> DataFrame (a Store's ``query`` or a DuckDB connection wrapper)."""
    parts = ["# SQL examples", "",
             "Every query below runs against `out/tlr.duckdb` (baseline run, reconciliation enabled) and the "
             "result tables are pasted by `python -m tlr all`, so they always match the code. Views are defined in "
             "[`src/tlr/sql/views.sql`](src/tlr/sql/views.sql); tables in "
             "[`src/tlr/sql/schema.sql`](src/tlr/sql/schema.sql).", "",
             "Open the database yourself with `duckdb out/tlr.duckdb` or `python -c \"import duckdb; "
             "print(duckdb.connect('out/tlr.duckdb').sql('SELECT * FROM v_recon_summary'))\"`.", ""]
    for i, (title, blurb, sql) in enumerate(SQL_EXAMPLES, 1):
        parts += [f"## {i}. {title}", "", blurb, "", "```sql", sql, "```", "", md_table(query(sql)), ""]
    path.write_text("\n".join(parts), encoding="utf-8", newline="\n")
