"""Scoring: compare what reconciliation found with what was injected.

An exception *matches* an incident when they share an identifier (fill, allocation,
account/instrument position or statement). It is a true positive only if the
break type is also right. Benign noise (exact replays) carries no expected break:
any exception touching it is a false positive.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from decimal import Decimal

from .model import BreakType
from .pipeline import RunResult
from .refdata import FX_TO_USD, INSTRUMENTS, LOCATIONS


def _f(x: Decimal | float | int) -> float:
    return round(float(x), 2)


def score(r: RunResult) -> dict:
    incidents = [i for i in r.world.incidents if i.expected_break]
    benign = [i for i in r.world.incidents if not i.expected_break]
    excs = r.queue.all
    by_key: dict[str, list] = defaultdict(list)
    for e in excs:
        for k in e.obs.related_keys:
            by_key[k].append(e)

    exc_label: dict[str, str] = {}
    per_type = {t: {"injected": 0, "detected": 0, "misclassified": 0, "missed": 0, "latencies": []}
                for t in BreakType.INJECTABLE}
    incident_rows = []
    for inc in incidents:
        matched = {e.exc_id: e for k in sorted(inc.match_keys) for e in by_key.get(k, [])}
        matched = dict(sorted(matched.items()))
        correct = sorted((e for e in matched.values() if e.obs.break_type == inc.expected_break),
                         key=lambda e: e.opened_at)
        row = per_type[inc.expected_break]
        row["injected"] += 1
        for e in matched.values():
            exc_label[e.exc_id] = "TP" if e.obs.break_type == inc.expected_break else exc_label.get(e.exc_id, "MISCLASSIFIED")
        latency = None
        if correct:
            row["detected"] += 1
            latency = (correct[0].opened_at - inc.observable_at).total_seconds() / 60
            row["latencies"].append(latency)
        elif matched:
            row["misclassified"] += 1
        else:
            row["missed"] += 1
        incident_rows.append({
            "incident_id": inc.incident_id, "kind": inc.kind, "expected_break": inc.expected_break,
            "observable_at": inc.observable_at, "match_keys": ",".join(sorted(inc.match_keys)),
            "description": inc.description, "material": inc.material, "detected": bool(correct),
            "detected_as": correct[0].obs.break_type if correct else (next(iter(matched.values())).obs.break_type if matched else None),
            "exc_id": correct[0].exc_id if correct else None, "detection_latency_min": latency})
    for e in excs:
        exc_label.setdefault(e.exc_id, "FP")
    benign_hits = sum(1 for i in benign for k in i.match_keys for e in by_key.get(k, []))

    labels = Counter(exc_label.values())
    tp, total = labels.get("TP", 0), len(excs)
    detected = sum(v["detected"] for v in per_type.values())
    fp_by_type = Counter(e.obs.break_type for e in excs if exc_label[e.exc_id] == "FP")
    lat_all = [x for v in per_type.values() for x in v["latencies"]]
    type_table = {}
    for t, v in per_type.items():
        lats = sorted(v["latencies"])
        type_table[t] = {
            "injected": v["injected"], "detected": v["detected"], "misclassified": v["misclassified"],
            "missed": v["missed"], "false_positives": fp_by_type.get(t, 0),
            "raised": sum(1 for e in excs if e.obs.break_type == t),
            "median_latency_min": round(statistics.median(lats), 1) if lats else None,
            "max_latency_min": round(max(lats), 1) if lats else None,
        }
    for t in type_table:
        type_table[t]["tp_exceptions"] = sum(1 for e in excs if e.obs.break_type == t and exc_label[e.exc_id] == "TP")
    for t in (BreakType.POSITION_MISMATCH, BreakType.CASH_MISMATCH):
        type_table[t] = {"injected": 0, "detected": 0, "misclassified": 0, "missed": 0,
                         "false_positives": fp_by_type.get(t, 0), "raised": sum(1 for e in excs if e.obs.break_type == t),
                         "median_latency_min": None, "max_latency_min": None, "tp_exceptions": 0}
    open_excs = [e for e in excs if e.is_open]
    return {
        "exceptions_raised": total,
        "true_positive_exceptions": tp,
        "misclassified_exceptions": labels.get("MISCLASSIFIED", 0),
        "false_positives": labels.get("FP", 0),
        "precision": round(tp / total, 4) if total else None,
        "incidents_injected": len(incidents),
        "incidents_detected": detected,
        "recall": round(detected / len(incidents), 4) if incidents else None,
        "benign_noise_events": len(benign),
        "benign_noise_exceptions": benign_hits,
        "median_detection_latency_min": round(statistics.median(lat_all), 1) if lat_all else None,
        "p90_detection_latency_min": round(sorted(lat_all)[int(0.9 * (len(lat_all) - 1))], 1) if lat_all else None,
        "unresolved_at_end": len(open_excs),
        "unresolved_by_type": dict(Counter(e.obs.break_type for e in open_excs)),
        "status_counts": r.queue.status_counts(),
        "by_type": type_table,
        "incident_rows": incident_rows,
        "exception_labels": exc_label,
    }


def book_differences(r: RunResult, as_of=None) -> dict:
    """Gross USD differences, at ``as_of`` (default: last day): internal vs expected, external vs expected."""
    d = as_of or max(s["as_of"] for s in r.snapshots)
    pos = {"internal_vs_expected_sd": Decimal(0), "internal_vs_expected_td": Decimal(0),
           "external_vs_expected_sd": Decimal(0), "internal_vs_external_sd": Decimal(0)}
    by_loc = {loc: Decimal(0) for loc in LOCATIONS}
    for s in r.snapshots:
        if s["as_of"] != d:
            continue
        inst = INSTRUMENTS[s["instrument_id"]]
        unit = s["mark"] * inst.price_factor * FX_TO_USD[inst.currency]
        pos["internal_vs_expected_sd"] += abs(s["internal_sd"] - s["expected_sd"]) * unit
        pos["internal_vs_expected_td"] += abs(s["internal_td"] - s["expected_td"]) * unit
        pos["external_vs_expected_sd"] += abs(s["external_sd"] - s["expected_sd"]) * unit
        pos["internal_vs_external_sd"] += abs(s["internal_sd"] - s["external_sd"]) * unit
        by_loc[s["location"]] += abs(s["internal_td"] - s["expected_td"]) * unit
    cash = {"internal_vs_expected_sd": Decimal(0), "internal_vs_expected_td": Decimal(0),
            "external_vs_expected_sd": Decimal(0), "internal_vs_external_sd": Decimal(0)}
    for c in r.cash_snapshots:
        if c["as_of"] != d:
            continue
        fx = FX_TO_USD[c["currency"]]
        ext = c["external_sd"] if c["external_sd"] is not None else c["expected_sd"]
        cash["internal_vs_expected_sd"] += abs(c["internal_sd"] - c["expected_sd"]) * fx
        cash["internal_vs_expected_td"] += abs(c["internal_td"] - c["expected_td"]) * fx
        cash["external_vs_expected_sd"] += abs(ext - c["expected_sd"]) * fx
        cash["internal_vs_external_sd"] += abs(c["internal_sd"] - ext) * fx
    pnl_err = sum(abs(p["internal_pnl_usd"] - p["expected_pnl_usd"]) for p in r.pnl if p["as_of"] == d)
    return {"as_of": str(d), "position_usd": {k: _f(v) for k, v in pos.items()},
            "position_td_usd_by_location": {k: _f(v) for k, v in by_loc.items()},
            "cash_usd": {k: _f(v) for k, v in cash.items()}, "pnl_error_usd": _f(pnl_err)}


def daily_differences(r: RunResult) -> list[dict]:
    days = sorted({s["as_of"] for s in r.snapshots})
    return [book_differences(r, d) for d in days]


def material_trace(r: RunResult) -> dict | None:
    """Follow the material incident's instrument through the books, day by day."""
    inc = next((i for i in r.world.incidents if i.material), None)
    if not inc:
        return None
    key = (inc.details["venue"], inc.details["fill"])
    t = r.world.truth_fills[key]
    iid = t.instrument_id
    days = sorted({s["as_of"] for s in r.snapshots})
    rows = []
    for d in days:
        snap = [s for s in r.snapshots if s["as_of"] == d and s["instrument_id"] == iid]
        dq_td = sum(s["internal_td"] - s["expected_td"] for s in snap)
        dq_sd = sum(s["internal_sd"] - s["expected_sd"] for s in snap)
        ccy = INSTRUMENTS[iid].currency
        cash = [c for c in r.cash_snapshots if c["as_of"] == d and c["currency"] == ccy]
        dc_td = sum(c["internal_td"] - c["expected_td"] for c in cash)
        dc_sd = sum(c["internal_sd"] - c["expected_sd"] for c in cash)
        dpnl = sum(p["internal_pnl_usd"] - p["expected_pnl_usd"] for p in r.pnl if p["as_of"] == d)
        rows.append({"as_of": str(d), "position_error_td": _f(dq_td), "position_error_sd": _f(dq_sd),
                     "cash_error_td": _f(dc_td), "cash_error_sd": _f(dc_sd), "pnl_error_usd": _f(dpnl)})
    exc = next((e for e in r.queue.all if e.obs.related_keys & inc.match_keys), None)
    return {"incident": inc.description, "instrument_id": iid, "venue": key[0], "fill_id": key[1],
            "true_quantity": _f(t.quantity), "booked_quantity": _f(t.quantity * 10),
            "trade_ts": t.trade_ts.isoformat(), "observable_at": inc.observable_at.isoformat(),
            "detected_at": exc.opened_at.isoformat() if exc else None,
            "detected_as": exc.obs.break_type if exc else None,
            "closed_at": exc.closed_at.isoformat() if exc and exc.closed_at else None,
            "detection_latency_min": round((exc.opened_at - inc.observable_at).total_seconds() / 60, 1) if exc else None,
            "daily": rows}


def volumes(r: RunResult) -> dict:
    w, cap = r.world, r.capture
    scripts = w.scripts.values()
    return {
        "trading_days": len(w.trading_days), "calendar_window": f"{w.all_days[0]} to {w.all_days[-1]}",
        "orders": len(w.scripts), "rejected_orders": sum(s.rejected for s in scripts),
        "partially_filled_then_cancelled": sum(1 for s in scripts if s.cancel_ts),
        "fills": len(w.truth_fills), "orders_with_multiple_fills": sum(1 for s in scripts if len(s.fills) > 1),
        "venue_messages_delivered": cap.counts["messages"], "exact_replays_ignored": cap.replays_ignored,
        "conflicting_messages_quarantined": cap.conflicts, "allocations": len(cap.bookings),
        "booking_versions": cap.counts["booking_versions"], "confirmations": len(cap.confirms),
        "statements": len(cap.statements),
        "recon_runs": int(r.store.rows("SELECT count(*) FROM recon_runs")[0][0]),
    }
