"""Each adapter must turn its counterparty's wire format into identical normalized records."""

from datetime import date, datetime, timezone
from decimal import Decimal as D

from tlr.adapters import alphax, bondhub, meridian, northbridge
from tlr.calendar import ny
from tlr.model import (
    Confirmation,
    ExecEvent,
    ExecType,
    ExtTrade,
    OrderStatus,
    SettlementStatus,
    Side,
    Statement,
    StatementPosition,
)


def _fill(venue, iid, ccy, qty, px, ts, side=Side.BUY, msg="F1", fid="F1"):
    return ExecEvent(venue=venue, exec_type=ExecType.FILL, message_id=msg, content_hash="", client_order_id="O1",
                     venue_order_id="VO1", account="BLOCK", instrument_id=iid, side=side,
                     status=OrderStatus.PARTIALLY_FILLED, trade_ts=ts, currency=ccy, venue_fill_id=fid,
                     quantity=D(qty), price=D(px), settlement_date=date(2026, 3, 3))


def _same(a: ExecEvent, b: ExecEvent):
    for f in ("venue", "exec_type", "client_order_id", "venue_order_id", "account", "instrument_id", "side", "status",
              "trade_ts", "currency", "venue_fill_id", "quantity", "price", "settlement_date"):
        assert getattr(a, f) == getattr(b, f), f


def test_alphax_fix_round_trip_and_float_artifact():
    e = _fill("ALPHAX", "ACME", "USD", "1500", "42.13", ny(date(2026, 3, 2), "10:15", 7, 123), side=Side.SELL)
    _same(alphax.parse_exec(alphax.render_exec(e, 1)), e)
    noisy = alphax.render_exec(e, 1, price_text=repr(float("42.13") + 2e-10))
    assert "31=42.1300000002" in noisy
    assert alphax.parse_exec(noisy).price == D("42.13")


def test_bondhub_dealer_side_thousands_and_local_time_across_dst():
    before = ny(date(2026, 3, 6), "10:00")  # EST, UTC-5
    after = ny(date(2026, 3, 9), "10:00")  # EDT, UTC-4 (US DST began 2026-03-08)
    assert before.hour == 15 and after.hour == 14
    for ts in (before, after):
        e = _fill("BONDHUB", "SYNT-4.25-2031", "USD", "5000000", "99.4250", ts)
        raw = bondhub.render_exec(e)
        assert ",S,5000," in raw  # we BUY => dealer side S; face in thousands
        assert ts.astimezone(timezone.utc) == bondhub.parse_exec(raw).trade_ts
        parsed = bondhub.parse_exec(raw)
        assert parsed.side == Side.BUY and parsed.quantity == D(5_000_000)
        assert parsed.fee == D("248.56")  # 0.5bp of 4,971,250.00 = 248.5625 -> 248.56


def test_meridian_signed_qty_epoch_ms_and_nested_fees():
    e = _fill("MERIDIAN", "SYX-H26", "USD", "7", "5012.25", ny(date(2026, 3, 4), "11:00", 1, 999), side=Side.SELL,
              msg="EV-X1", fid="X1")
    raw = meridian.render_exec(e)
    assert '"qty":-7' in raw
    p = meridian.parse_exec(raw)
    _same(p, e)
    assert p.fee == D("17.50")  # 7 x (1.25 + 0.40 + 0.85)


def test_drop_copies_round_trip_with_float_prices():
    ts = ny(date(2026, 3, 4), "11:00")
    t = ExtTrade("MERIDIAN", "X1", "VO1", "O1", "SYN10Y-H26", Side.BUY, D(3), D("112.515625"), D("7.50"), ts, "USD",
                 "BLOCK")
    cov, rows = meridian.parse_drop_copy(meridian.render_drop_copy([(t, None)], ts, float_noise=True))
    assert cov == ts and rows == [t]
    for mod, venue, iid, qty, px in ((alphax, "ALPHAX", "NORD", D(700), D("54.31")),
                                     (bondhub, "BONDHUB", "SYNB-2.50-2034", D(1_250_000), D("96.1505"))):
        t = ExtTrade(venue, "F9", "VO9", "O9", iid, Side.SELL, qty, px, D("12.34"), ts, "EUR", "BLOCK")
        cov, rows = mod.parse_drop_copy(mod.render_drop_copy([(t, None)], ts))
        assert cov == ts and rows == [t]


def test_confirmations_round_trip():
    c = Confirmation("ALPHAX", "O1-FUND-A", "FUND-A", "ACME", Side.BUY, D(600), D("42.130000"), date(2026, 3, 3),
                     datetime(2026, 3, 2, 22, 15, 0, 0, tzinfo=timezone.utc))
    assert alphax.parse_confirm(alphax.render_confirm(c)) == c
    assert bondhub.parse_confirm(bondhub.render_confirm(c.__class__(**{**c.__dict__, "venue": "BONDHUB",
                                                                         "instrument_id": "SYNT-4.25-2031"}))).quantity == D(600)
    m = c.__class__(**{**c.__dict__, "venue": "MERIDIAN"})
    assert meridian.parse_confirm(meridian.render_confirm(m)) == m


def test_custodian_statement_comma_decimals_and_negative_quantities():
    s = Statement("NORTHBRIDGE", "FUND-B", date(2026, 3, 5), date(2026, 3, 4),
                  (StatementPosition("ACME", D(-1200)), StatementPosition("SYNT-4.25-2031", D(80_250_000))),
                  (("EUR", D("-1234.56")), ("USD", D("750000123.45"))),
                  datetime(2026, 3, 5, 23, 0, tzinfo=timezone.utc), "NB-1")
    raw = northbridge.render_statement(s)
    assert ":62F:D260305EUR1234,56" in raw and "UNIT/N1200" in raw and "FAMT/80250000" in raw
    assert northbridge.parse_statement(raw) == s
    st = SettlementStatus("NORTHBRIDGE", "O1-FUND-A", "FAILED", "LACK", date(2026, 3, 3),
                          datetime(2026, 3, 3, 21, 0, 5, tzinfo=timezone.utc))
    assert northbridge.parse_settlement(northbridge.render_settlement(st)) == st
    ms = Statement("MERIDIAN", "FUND-A", date(2026, 3, 5), date(2026, 3, 5), (StatementPosition("SYX-H26", D(-12)),),
                   (("USD", D("1.00")),), datetime(2026, 3, 5, 23, 30, tzinfo=timezone.utc), "MR-1")
    assert meridian.parse_statement(meridian.render_statement(ms)) == ms
