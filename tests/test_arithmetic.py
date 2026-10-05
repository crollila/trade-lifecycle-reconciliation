"""Settlement dates, day counts, fees, allocation and ledger conservation."""

import random
from datetime import date, datetime, timezone
from decimal import Decimal as D

from tlr import ledger
from tlr.allocation import accrued_interest, allocate, split_largest_remainder
from tlr.calendar import BusinessCalendar, accrued_fraction, days_30_360, settlement_date
from tlr.model import AssetClass, Order, Side
from tlr.refdata import INSTRUMENTS, expected_fee

CAL = BusinessCalendar()


def test_settlement_cycles_weekends_and_holidays():
    fri, thu = date(2026, 3, 6), date(2026, 3, 5)
    assert settlement_date(fri, AssetClass.EQUITY, "USD", CAL) == date(2026, 3, 9)  # T+1 over the weekend
    assert settlement_date(thu, AssetClass.EQUITY, "EUR", CAL) == date(2026, 3, 9)  # T+2 over the weekend
    assert settlement_date(thu, AssetClass.BOND, "USD", CAL) == date(2026, 3, 6)
    assert settlement_date(fri, AssetClass.FUTURE, "USD", CAL) == fri  # margined, no DVP
    holiday_cal = BusinessCalendar({date(2026, 3, 9)})  # synthetic holiday for the test
    assert settlement_date(fri, AssetClass.EQUITY, "USD", holiday_cal) == date(2026, 3, 10)
    assert holiday_cal.prev_business_day(date(2026, 3, 10)) == fri


def test_day_counts_and_accrued():
    assert days_30_360(date(2025, 12, 15), date(2026, 3, 3)) == 78
    assert days_30_360(date(2026, 1, 30), date(2026, 3, 31)) == 60
    acme = INSTRUMENTS["ACME-5.10-2030"]  # 30/360, Jun/Dec 15
    assert accrued_fraction(acme.maturity, 2, "30/360", date(2026, 3, 3)) == D(78) / D(360)
    assert accrued_interest(acme, D(1_000_000), date(2026, 3, 3)) == D("11050.00")  # 1mm * 5.10% * 78/360
    synt = INSTRUMENTS["SYNT-4.25-2031"]  # ACT/ACT semiannual, Nov/May 15
    frac = accrued_fraction(synt.maturity, 2, "ACT/ACT", date(2026, 3, 3))
    assert frac == D(108) / D(181) / 2  # 108 of 181 days in the Nov-15 -> May-15 period


def test_fee_arithmetic_rounds_half_up_to_cents():
    acme = INSTRUMENTS["ACME"]
    assert expected_fee("ALPHAX", acme, D(1430), D("42.00")) == D("5.01")  # 5.005 -> 5.01, not banker's 5.00
    assert expected_fee("ALPHAX", INSTRUMENTS["NORD"], D(1000), D("54.30")) == D("16.29")  # 3bp of 54,300
    assert expected_fee("MERIDIAN", acme, D(2500), D("42.00")) == D("10.00")
    assert expected_fee("MERIDIAN", INSTRUMENTS["SYX-H26"], D(7), D("5000")) == D("17.50")
    assert expected_fee("BONDHUB", INSTRUMENTS["SYNB-2.50-2034"], D(2_000_000), D("96.15")) == D("144.23")


def test_largest_remainder_is_exact():
    rng = random.Random(1)
    for _ in range(500):
        total = rng.randint(0, 10_000_000)
        w = [D(rng.randint(1, 9)) for _ in range(rng.randint(1, 4))]
        parts = split_largest_remainder(total, w)
        assert sum(parts) == total and all(p >= 0 for p in parts)


def _order(iid, qty, weights, side=Side.BUY):
    accts = ["FUND-A", "FUND-B", "FUND-C"][: len(weights)]
    return Order("O1", date(2026, 3, 2), iid, side, D(qty), D(1), "ALPHAX", tuple(zip(accts, map(D, weights))),
                 datetime(2026, 3, 2, 15, tzinfo=timezone.utc))


def test_partial_fills_aggregate_into_block_vwap():
    inst = INSTRUMENTS["ACME"]
    fills = [(D(300), D("42.10"), D("1.05")), (D(500), D("42.15"), D("1.75")), (D(200), D("42.31"), D("0.70"))]
    bks = allocate(_order("ACME", 1000, ["0.5", "0.3", "0.2"]), fills, inst, "NORTHBRIDGE", CAL)
    assert sum(b.quantity for b in bks) == D(1000)
    assert sum(b.gross for b in bks) == D("42167.00")  # 12630 + 21075 + 8462
    assert sum(b.fee for b in bks) == D("3.50")
    vwap = D("42167") / D(1000)
    assert all(abs(b.gross / b.quantity - vwap) < D("0.0001") for b in bks)
    assert [b.quantity for b in bks] == [D(500), D(300), D(200)]


def test_position_and_cash_conservation_across_random_blocks():
    rng = random.Random(7)
    marks = {(iid, date(2026, 3, d)): D("100") for iid in INSTRUMENTS for d in range(1, 20)}
    for _ in range(300):
        iid = rng.choice(["ACME", "NORD", "SYNT-4.25-2031", "SYX-H26"])
        inst = INSTRUMENTS[iid]
        unit = {AssetClass.EQUITY: 100, AssetClass.BOND: 250_000, AssetClass.FUTURE: 1}[inst.asset_class]
        fills = [(D(unit * rng.randint(1, 30)), D(rng.randint(9000, 11000)) / 100, D(rng.randint(0, 999)) / 100)
                 for _ in range(rng.randint(1, 5))]
        side = rng.choice((Side.BUY, Side.SELL))
        weights = rng.choice((["0.5", "0.3", "0.2"], ["0.6", "0.4"], ["1"]))
        bks = allocate(_order(iid, sum(q for q, _, _ in fills), weights, side), fills, inst, "NORTHBRIDGE", CAL)
        block_q = sum(q for q, _, _ in fills)
        assert sum(b.quantity for b in bks) == block_q  # position conservation
        block_gross = sum(q * p * inst.price_factor for q, p, _ in fills)
        assert sum(b.gross for b in bks) == block_gross.quantize(D("0.01"), rounding="ROUND_HALF_UP")
        assert sum(b.fee for b in bks) == sum(f for _, _, f in fills)  # cash/fee conservation
        pos = ledger.positions(bks, [], date(2026, 3, 18), "SD")
        assert sum(pos.values()) == side * block_q
        cash = ledger.cash(bks, {}, date(2026, 3, 18), marks, "SD")
        if inst.asset_class != AssetClass.FUTURE:
            assert sum(cash.values()) == -side * sum(b.gross + b.accrued for b in bks) - sum(b.fee for b in bks)
