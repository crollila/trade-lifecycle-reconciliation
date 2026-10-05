"""Static, dashboard-quality figures (PNG) generated from a run."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from .calendar import NEW_YORK  # noqa: E402
from .model import BreakType  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "legend.frameon": False, "font.family": "DejaVu Sans",
})

LABEL = {t: t.replace("_", " ").title().replace("Fee ", "Fee ") for t in BreakType.ALL}


def _save(fig, out: Path, name: str) -> Path:
    path = out / name
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return path


def _usd(x: float) -> str:
    ax = abs(x)
    if ax >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if ax >= 1e3:
        return f"${x / 1e3:,.1f}K"
    return f"${x:,.0f}"


# ------------------------------------------------------------------ lifecycle
def lifecycle_flow(out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(13, 6.2))
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 6.2)
    ax.axis("off")
    ax.grid(False)

    def box(x, y, w, h, text, color, fill="#ffffff", size=8.6, weight="normal"):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                    linewidth=1.6, edgecolor=color, facecolor=fill))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=INK, weight=weight)

    def arrow(x1, y1, x2, y2, color=INK2, style="-|>", ls="-"):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=11, color=color,
                                     linewidth=1.2, linestyle=ls))

    ax.text(0.1, 5.95, "OUR BOOKS (internal)", fontsize=9, color=BLUE, weight="bold")
    ax.text(0.1, 0.12, "COUNTERPARTIES (external: venues, brokers, custodians)", fontsize=9, color=ORANGE, weight="bold")
    ax.text(0.1, 3.25, "RECONCILIATION CONTROLS", fontsize=9, color=VIOLET, weight="bold")

    top = ["Order\n(OMS)", "Ack", "Fills\n(partial)", "Allocation\n17:00", "Trade\ncapture", "Settlement\ninstruction",
           "Settlement", "Position", "Cash", "P&L"]
    xs = [0.1 + i * 1.29 for i in range(len(top))]
    for x, t in zip(xs, top):
        box(x, 4.75, 1.08, 0.95, t, BLUE)
    for a, b in zip(xs, xs[1:]):
        arrow(a + 1.08, 5.22, b, 5.22)

    bottom = [(xs[1], "Exec reports\nFIX / CSV / JSON"), (xs[2], "Drop copies\n(trade files)"),
              (xs[4], "Confirmations"), (xs[6], "Settlement\nstatus"), (xs[7], "EOD statements\npositions + cash")]
    for x, t in bottom:
        box(x, 0.55, 1.2, 0.95, t, ORANGE, size=8.2)

    controls = [(xs[2] + 0.05, "T+0 trade\n+ fee recon", "every\n15 min"), (xs[4] + 0.05, "Confirm\nrecon", "21:05"),
                (xs[6] + 0.05, "Settlement\nrecon", "19:30"), (xs[7] + 0.05, "Position +\ncash recon", "19:30")]
    for x, t, when in controls:
        box(x, 2.45, 1.1, 0.85, t + "\n" + when.replace("\n", " "), VIOLET, fill="#f3f1fb", size=8)
    # adapters feed our books; controls compare both sides
    arrow(xs[1] + 0.54, 1.5, xs[1] + 0.54, 4.75, ORANGE)
    ax.text(xs[1] + 0.62, 3.9, "adapters\nnormalize", fontsize=7.5, color=ORANGE)
    for (bx, _), (cx, _, _) in zip([bottom[1], bottom[2], bottom[3], bottom[4]], controls):
        arrow(bx + 0.6, 1.5, cx + 0.55, 2.45, ORANGE, ls="--")
    for cx, tx in zip([c[0] for c in controls], [xs[2], xs[4], xs[6], xs[7]]):
        arrow(tx + 0.54, 4.75, cx + 0.55, 3.3, BLUE, ls="--")
    box(10.75, 2.35, 2.15, 1.05, "Exception queue\nseverity · owner · age\naudit trail", RED, fill="#fdf0f0",
        weight="bold")
    arrow(xs[7] + 1.15, 2.87, 10.75, 2.87, RED)
    ax.text(10.8, 2.2, "ops desk fixes flow back\nthrough the same idempotent\ncapture path, then recon\nre-verifies",
            fontsize=7.5, color=INK2, va="top")
    ax.set_title("Trade lifecycle and where each reconciliation control sits", pad=4)
    return _save(fig, out, "lifecycle_flow.png")


# -------------------------------------------------------------- breaks by type
def breaks_by_type(base: dict, stress: dict, out: Path) -> Path:
    types = list(BreakType.INJECTABLE) + [BreakType.POSITION_MISMATCH, BreakType.CASH_MISMATCH]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.6), sharey=True)
    for ax, sc, title in ((axes[0], base, "Baseline scenario (isolated incidents)"),
                          (axes[1], stress, "Stress scenario (3x incidents, allowed to overlap)")):
        bt = sc["by_type"]
        y = range(len(types))
        inj = [bt[t]["injected"] for t in types]
        det = [bt[t]["detected"] for t in types]
        bad = [bt[t]["raised"] - bt[t]["tp_exceptions"] for t in types]
        ax.barh([i + 0.2 for i in y], inj, height=0.38, color=MUTED, label="Injected")
        ax.barh([i - 0.2 for i in y], det, height=0.38, color=BLUE, label="Detected with correct type")
        ax.barh([i - 0.2 for i in y], bad, left=det, height=0.38, color=ORANGE, label="Extra / mis-typed exceptions")
        for i, (a, b, c) in enumerate(zip(inj, det, bad)):
            ax.text(max(a, b + c) + 0.4, i, f"{b}/{a}" + (f"  +{c}" if c else ""), va="center", fontsize=8, color=INK2)
        ax.set_yticks(list(y), [LABEL[t] for t in types])
        ax.invert_yaxis()
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("count")
        ax.text(0.99, 0.02, f"precision {sc['precision']:.2f} · recall {sc['recall']:.2f}", transform=ax.transAxes,
                ha="right", fontsize=9, color=INK)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.suptitle("Breaks by type: injected vs detected", x=0.01, ha="left", fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.legend(handles, labels, loc="lower center", ncol=3, fontsize=9)
    return _save(fig, out, "breaks_by_type.png")


# --------------------------------------------------- status over (sim) time
def recon_status_over_time(history: list[tuple[datetime, str, int]], out: Path) -> Path:
    by_ts: dict[datetime, dict[str, int]] = defaultdict(dict)
    for ts, status, n in history:
        by_ts[ts][status] = n
    ts_sorted = sorted(by_ts)
    open_states = ["OPEN", "INVESTIGATING", "PENDING_EXTERNAL", "FIX_APPLIED", "ESCALATED"]
    colors = {"OPEN": RED, "INVESTIGATING": YELLOW, "PENDING_EXTERNAL": VIOLET, "FIX_APPLIED": AQUA, "ESCALATED": MAGENTA}
    x = list(range(len(ts_sorted)))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 6.6), sharex=True, gridspec_kw={"height_ratios": [1.3, 1]})
    bottom = [0] * len(x)
    for st in open_states:
        vals = [by_ts[t].get(st, 0) for t in ts_sorted]
        if any(vals):
            ax1.fill_between(x, bottom, [b + v for b, v in zip(bottom, vals)], step="post", color=colors[st],
                             alpha=0.9, linewidth=0, label=st.replace("_", " ").title())
            bottom = [b + v for b, v in zip(bottom, vals)]
    ax1.set_ylabel("open exceptions")
    ax1.set_title("Exception queue status at every reconciliation run")
    ax1.legend(ncol=5, loc="upper left", fontsize=8)
    resolved = [by_ts[t].get("RESOLVED", 0) for t in ts_sorted]
    auto = [by_ts[t].get("AUTO_CLEARED", 0) for t in ts_sorted]
    total = [sum(by_ts[t].values()) for t in ts_sorted]
    ax2.step(x, total, where="post", color=INK2, linewidth=2, label="Raised (cumulative)")
    ax2.step(x, [a + b for a, b in zip(resolved, auto)], where="post", color=BLUE, linewidth=2,
             label="Closed (resolved by ops + auto-cleared)")
    ax2.set_ylabel("exceptions")
    ax2.legend(loc="upper left", fontsize=8)
    days, ticks = [], []
    for i, t in enumerate(ts_sorted):
        d = t.astimezone(NEW_YORK).date() if t.tzinfo else t.date()
        if not days or (days[-1] != d and i - ticks[-1] >= 8):
            days.append(d)
            ticks.append(i)
    for a in (ax1, ax2):
        for tk in ticks:
            a.axvline(tk, color=GRID, linewidth=0.8, zorder=0)
    ax2.set_xticks(ticks, [d.strftime("%a\n%d %b") for d in days], fontsize=8)
    ax2.set_xlabel("reconciliation runs (simulated clock; one gridline per business day)")
    fig.tight_layout()
    return _save(fig, out, "recon_status_over_time.png")


# ------------------------------------------- positions before / after recon
def positions_before_after(off: dict, on: dict, out: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    locs = list(off["position_td_usd_by_location"])
    vals_off = [off["position_td_usd_by_location"][l] for l in locs]
    vals_on = [on["position_td_usd_by_location"][l] for l in locs]
    x = range(len(locs))
    ax = axes[0]
    ax.bar([i - 0.2 for i in x], vals_off, width=0.38, color=ORANGE, label="Reconciliation disabled")
    ax.bar([i + 0.2 for i in x], vals_on, width=0.38, color=BLUE, label="Reconciliation enabled")
    for i, (a, b) in enumerate(zip(vals_off, vals_on)):
        ax.text(i - 0.2, a, _usd(a), ha="center", va="bottom", fontsize=9)
        ax.text(i + 0.2, b, _usd(b), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x), [f"{l.title()}\n(custody location)" for l in locs])
    ax.set_ylabel("gross |internal - expected| position, USD")
    ax.set_title("Position error by venue/custodian, end of window", fontsize=11)
    ax.yaxis.set_major_formatter(lambda v, _: _usd(v))
    ax.legend(fontsize=8)
    ax = axes[1]
    cats = ["internal_vs_expected_sd", "external_vs_expected_sd", "internal_vs_external_sd"]
    names = ["our books\nvs expected", "custodian\nvs expected", "our books\nvs custodian"]
    a_vals = [off["cash_usd"][c] for c in cats]
    b_vals = [on["cash_usd"][c] for c in cats]
    x = range(len(cats))
    ax.bar([i - 0.2 for i in x], a_vals, width=0.38, color=ORANGE, label="Reconciliation disabled")
    ax.bar([i + 0.2 for i in x], b_vals, width=0.38, color=BLUE, label="Reconciliation enabled")
    for i, (a, b) in enumerate(zip(a_vals, b_vals)):
        ax.text(i - 0.2, a, _usd(a), ha="center", va="bottom", fontsize=9)
        ax.text(i + 0.2, b, _usd(b), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(x), names)
    ax.set_title("Settled-cash error, end of window", fontsize=11)
    ax.yaxis.set_major_formatter(lambda v, _: _usd(v))
    fig.tight_layout()
    return _save(fig, out, "venue_positions_before_after.png")


# ------------------------------------------------------------------ aging
def exception_aging(excs: list, end: datetime, out: Path) -> Path:
    types = [t for t in BreakType.ALL if any(e.obs.break_type == t for e in excs)]
    fig, ax = plt.subplots(figsize=(12, 5.2))
    for i, t in enumerate(types):
        closed = [(e.closed_at - e.opened_at).total_seconds() / 3600 for e in excs if e.obs.break_type == t and e.closed_at]
        still = [(end - e.opened_at).total_seconds() / 3600 for e in excs if e.obs.break_type == t and not e.closed_at]
        ax.scatter(closed, [i + (j % 5 - 2) * 0.06 for j in range(len(closed))], s=26, color=BLUE, alpha=0.8,
                   edgecolors=SURFACE, linewidths=0.8, label="closed: time to close" if i == 0 else None)
        if still:
            ax.scatter(still, [i] * len(still), s=60, marker="D", color=RED, edgecolors=SURFACE, linewidths=0.8,
                       label="still open at end: age")
        if closed:
            med = sorted(closed)[len(closed) // 2]
            ax.plot([med, med], [i - 0.3, i + 0.3], color=INK, linewidth=2)
    for h, lab in ((4, "4h"), (24, "1d"), (72, "3d")):
        ax.axvline(h, color=MUTED, linestyle=":", linewidth=1)
        ax.text(h, -0.6, lab, color=MUTED, fontsize=8, ha="center")
    ax.set_xscale("log")
    ax.set_xlim(left=0.2)
    ax.set_yticks(range(len(types)), [LABEL[t] for t in types])
    ax.invert_yaxis()
    ax.set_xlabel("hours, log scale (black tick = median time to close)")
    ax.set_title("Exception aging: how long each break stayed open")
    handles, labels = ax.get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    ax.legend(uniq.values(), uniq.keys(), loc="lower left", fontsize=8)
    fig.tight_layout()
    return _save(fig, out, "exception_aging.png")


def detection_latency(score: dict, out: Path) -> Path:
    rows = [r for r in score["incident_rows"] if r["detection_latency_min"] is not None]
    types = [t for t in BreakType.INJECTABLE if any(r["expected_break"] == t for r in rows)]
    fig, ax = plt.subplots(figsize=(12, 4.8))
    for i, t in enumerate(types):
        vals = [r["detection_latency_min"] for r in rows if r["expected_break"] == t]
        ax.scatter(vals, [i + (j % 5 - 2) * 0.06 for j in range(len(vals))], s=26, color=VIOLET, alpha=0.85,
                   edgecolors=SURFACE, linewidths=0.8)
        med = sorted(vals)[len(vals) // 2]
        ax.plot([med, med], [i - 0.3, i + 0.3], color=INK, linewidth=2)
        ax.text(max(vals) * 1.05 + 1, i, f"median {med:.0f} min", va="center", fontsize=8, color=INK2)
    ax.set_xscale("log")
    ax.set_yticks(range(len(types)), [LABEL[t] for t in types])
    ax.invert_yaxis()
    ax.set_xlabel("minutes from the moment the problem became observable to the exception being raised (log scale)")
    ax.set_title("Detection latency by break type (baseline scenario)")
    fig.tight_layout()
    return _save(fig, out, "detection_latency.png")


def material_propagation(trace_off: dict, trace_on: dict, out: Path) -> Path:
    days = [r["as_of"][5:] for r in trace_off["daily"]]
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.2))
    series = [("position_error_td", "Position error (face, trade-date)", lambda v: f"{v / 1e6:,.0f}M"),
              ("cash_error_sd", "Settled-cash error (USD)", _usd),
              ("pnl_error_usd", "P&L error (USD)", _usd)]
    for ax, (key, title, fmt) in zip(axes, series):
        off = [r[key] for r in trace_off["daily"]]
        on = [r[key] for r in trace_on["daily"]]
        ax.plot(days, off, color=ORANGE, linewidth=2, marker="o", markersize=5, label="Reconciliation disabled")
        ax.plot(days, on, color=BLUE, linewidth=2, marker="o", markersize=5, label="Reconciliation enabled")
        ax.axhline(0, color=INK2, linewidth=0.8)
        ax.set_title(title, fontsize=10.5)
        ax.yaxis.set_major_formatter(lambda v, _, f=fmt: f(v))
        ax.tick_params(axis="x", labelrotation=60, labelsize=8)
    axes[0].legend(fontsize=8, loc="lower left")
    fig.suptitle(f"Material incident: {trace_off['incident'].replace('MATERIAL: ', '')}", x=0.01, ha="left",
                 fontsize=12, weight="bold")
    fig.tight_layout()
    return _save(fig, out, "material_incident_propagation.png")
