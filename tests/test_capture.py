"""Trade capture: idempotent ingestion, partial-fill aggregation, corrections, re-allocation."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

from tlr import ledger
from tlr.adapters import alphax
from tlr.bus import InProcessBus
from tlr.calendar import BusinessCalendar, ny
from tlr.capture import Capture
from tlr.model import ExecEvent, ExecType, Order, OrderStatus, Side
from tlr.store import Store

DAY = date(2026, 3, 2)


def _setup():
    bus = InProcessBus(ny(DAY, "00:00"))
    store = Store(None)
    cap = Capture(bus, store, BusinessCalendar())
    order = Order("O1", DAY, "ACME", Side.BUY, D(1000), D(42), "ALPHAX",
                  (("FUND-A", D("0.6")), ("FUND-B", D("0.4"))), ny(DAY, "10:00"))
    bus.publish("oms.order", order, order.created_at)
    return bus, store, cap


def _fill_raw(fid, qty, px, minute, status=OrderStatus.PARTIALLY_FILLED, seq=1):
    e = ExecEvent("ALPHAX", ExecType.FILL, fid, "", "O1", "AXO1", "BLOCK", "ACME", Side.BUY, status,
                  ny(DAY, f"10:{minute:02d}"), "USD", venue_fill_id=fid, quantity=D(qty), price=D(px),
                  settlement_date=date(2026, 3, 3))
    return alphax.render_exec(e, seq)


def test_exact_duplicate_replay_is_idempotent():
    bus, store, cap = _setup()
    raw = _fill_raw("F1", 400, "42.10", 5)
    for i in range(3):  # the same bytes delivered three times
        bus.publish("raw.exec", ("ALPHAX", raw), ny(DAY, "10:06") + timedelta(seconds=i))
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F2", 600, "42.20", 7, OrderStatus.FILLED)), ny(DAY, "10:08"))
    bus.publish("alloc.run", DAY, ny(DAY, "17:00"))
    bus.run()
    assert cap.replays_ignored == 2 and len(cap.fills) == 2
    pos = ledger.positions(cap.bookings.values(), [], date(2026, 3, 3), "SD")
    assert sum(pos.values()) == D(1000)  # not 1,800
    outcomes = store.rows("SELECT outcome, count(*) FROM ingest_log GROUP BY 1 ORDER BY 1")
    assert outcomes == [("ACCEPTED", 2), ("REPLAY_IGNORED", 2)]


def test_conflicting_reuse_of_a_message_id_is_quarantined_not_applied():
    bus, store, cap = _setup()
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F1", 400, "42.10", 5)), ny(DAY, "10:06"))
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F1", 900, "42.10", 5)), ny(DAY, "10:07"))
    bus.run()
    assert cap.conflicts == 1 and cap.fills[("ALPHAX", "F1")].quantity == D(400)


def test_partial_fills_aggregate_and_post_allocation_correction_rebooks():
    bus, store, cap = _setup()
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F1", 300, "42.10", 5)), ny(DAY, "10:06"))
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F2", 500, "42.15", 6)), ny(DAY, "10:07"))
    bus.publish("raw.exec", ("ALPHAX", _fill_raw("F3", 200, "42.31", 7, OrderStatus.FILLED)), ny(DAY, "10:08"))
    bus.publish("alloc.run", DAY, ny(DAY, "17:00"))
    bus.run()
    (cum, leaves, n, vwap), = store.rows("SELECT cum_qty, leaves_qty, n_fills, vwap FROM v_order_fill_summary")
    assert (cum, leaves, n) == (D(1000), D(0), 3) and abs(float(vwap) - 42.167) < 1e-9
    assert cap.order_status["O1"] == OrderStatus.FILLED
    assert {b.account: b.quantity for b in cap.bookings.values()} == {"FUND-A": D(600), "FUND-B": D(400)}
    # an ops correction after allocation amends the bookings (new versions), never edits in place
    base = cap.fills[("ALPHAX", "F2")]
    corr = ExecEvent("ALPHAX", ExecType.CORRECT, "OPS-CORR-F2", "ops", "O1", "AXO1", "BLOCK", "ACME", Side.BUY,
                     OrderStatus.PARTIALLY_FILLED, base.trade_ts, "USD", venue_fill_id="F2", ref_fill_id="F2",
                     quantity=D(400), price=D("42.15"), source="OPS")
    bus.publish("ops.correction", corr, ny(DAY, "18:00"))
    bus.publish("ops.correction", replace(corr, message_id="OPS-BUST-F3", exec_type=ExecType.BUST, ref_fill_id="F3"),
                ny(DAY, "18:01"))
    bus.run()
    assert sum(b.quantity for b in cap.bookings.values()) == D(700)
    versions = store.rows("SELECT alloc_id, max(version) FROM allocations GROUP BY 1 ORDER BY 1")
    assert versions == [("O1-FUND-A", 3), ("O1-FUND-B", 3)]
    assert store.rows("SELECT count(*) FROM v_fills_current")[0][0] == 2
