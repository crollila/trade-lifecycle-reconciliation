"""End-to-end reconciliation behaviour on small deterministic simulations."""

from pathlib import Path

import pytest

from tlr import metrics
from tlr.config import SimConfig
from tlr.model import BreakType
from tlr.pipeline import run_simulation

SMALL = dict(orders_per_day=50, trading_days=5, exact_replays=40)
ZERO = {t: 0 for t in BreakType.INJECTABLE}


def _diffs_are_zero(r, keys=("internal_vs_expected_sd", "internal_vs_expected_td")):
    d = metrics.book_differences(r)
    return all(d["position_usd"][k] == 0 for k in keys) and all(d["cash_usd"][k] == 0 for k in keys)


@pytest.fixture(scope="module")
def clean_run():
    return run_simulation(SimConfig(incidents=dict(ZERO), material_incident=False, **SMALL))


@pytest.fixture(scope="module")
def full_run():
    return run_simulation(SimConfig(incidents={t: 1 for t in BreakType.INJECTABLE}, settle_days=6, **SMALL))


def test_clean_records_create_no_breaks(clean_run):
    assert clean_run.queue.all == []
    assert clean_run.capture.replays_ignored == 40  # benign replays absorbed
    d = metrics.book_differences(clean_run)
    assert all(v == 0 for v in d["position_usd"].values()) and all(v == 0 for v in d["cash_usd"].values())


@pytest.mark.parametrize("break_type", BreakType.INJECTABLE)
def test_each_injected_break_is_detected_and_correctly_classified(break_type):
    r = run_simulation(SimConfig(incidents={**ZERO, break_type: 2}, material_incident=False, **SMALL))
    sc = metrics.score(r)
    assert sc["by_type"][break_type]["injected"] == 2
    assert sc["by_type"][break_type]["detected"] == 2, sc["by_type"][break_type]
    assert sc["false_positives"] == 0 and sc["misclassified_exceptions"] == 0
    assert {e.obs.break_type for e in r.queue.all} == {break_type}


def test_injected_breaks_detected_and_resolution_restores_expected_state(full_run):
    sc = metrics.score(full_run)
    assert sc["recall"] == 1.0 and sc["precision"] == 1.0
    assert sc["unresolved_at_end"] == 0
    d = metrics.book_differences(full_run)
    assert all(v == 0 for v in d["position_usd"].values())
    assert all(v == 0 for v in d["cash_usd"].values())  # custodian side fixed too (fee credit, journal, reversal)
    assert d["pnl_error_usd"] == 0
    statuses = {e.status for e in full_run.queue.all}
    assert statuses <= {"RESOLVED", "AUTO_CLEARED"}
    # every exception carries a full audit trail ending in a recon-verified close
    for e in full_run.queue.all:
        assert e.audit[0]["action"] == "CREATED" and e.audit[-1]["to_status"] in ("RESOLVED", "AUTO_CLEARED")


def test_material_discrepancy_propagates_without_recon_and_is_caught_with_it():
    off = run_simulation(SimConfig(incidents=dict(ZERO), exact_replays=0, recon_enabled=False, **{
        k: v for k, v in SMALL.items() if k != "exact_replays"}))
    on = run_simulation(SimConfig(incidents=dict(ZERO), exact_replays=0, **{
        k: v for k, v in SMALL.items() if k != "exact_replays"}))
    t_off, t_on = metrics.material_trace(off), metrics.material_trace(on)
    last = t_off["daily"][-1]
    assert abs(last["position_error_td"]) == t_off["true_quantity"] * 9  # booked 10x
    assert abs(last["cash_error_sd"]) > 1_000_000  # the error reached settled cash
    assert not _diffs_are_zero(off)
    assert t_on["detected_as"] == BreakType.QUANTITY_MISMATCH and t_on["detection_latency_min"] < 120
    assert all(r["position_error_td"] == 0 and r["cash_error_sd"] == 0 for r in t_on["daily"])
    assert _diffs_are_zero(on)


def test_runs_are_deterministic():
    a = metrics.score(run_simulation(SimConfig(**SMALL)))
    b = metrics.score(run_simulation(SimConfig(**SMALL)))
    strip = lambda s: {k: v for k, v in s.items() if k != "exception_labels"}  # noqa: E731
    assert strip(a) == strip(b)


def test_recon_engine_never_reads_ground_truth():
    src = Path(__file__).resolve().parents[1] / "src" / "tlr"
    for name in ("recon.py", "capture.py", "ops.py", "exceptions.py"):
        text = (src / name).read_text()
        assert "world" not in text.lower().replace("outside world", ""), name
        assert "incident" not in text.lower(), name
