"""Books and records as pure functions of bookings, opening balances and marks.

The same functions compute (a) our internal books, (b) the scoring harness's
"expected" books and (c) the recon engine's model of what the custodian should
show. Because they are pure, any date can be recomputed after a correction.

Cash conventions
* cash securities (equities, bonds) move cash on the settlement date:
  ``-side * (gross + accrued) - fee``;
* futures never exchange notional; the account is margined daily
  (variation margin), so cumulative cash is ``side*(qty*mark*mult - gross) - fee``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable

from .allocation import accrued_interest
from .model import AssetClass, Booking, Holding, money
from .refdata import FX_TO_USD, INSTRUMENTS

Key3 = tuple[str, str, str]
Marks = dict[tuple[str, date], Decimal]


def _included(b: Booking, asof: date, basis: str) -> bool:
    if basis == "TD":
        return b.trade_date <= asof
    return b.settle_date is not None and b.settle_date <= asof


def positions(bookings: Iterable[Booking], holdings: Iterable[Holding], asof: date, basis: str = "SD") -> dict[Key3, Decimal]:
    """{(location, account, instrument): quantity}; basis 'TD' (trade date) or 'SD' (settled)."""
    out: dict[Key3, Decimal] = defaultdict(Decimal)
    for h in holdings:
        out[(h.location, h.account, h.instrument_id)] += h.quantity
    for b in bookings:
        inst = INSTRUMENTS[b.instrument_id]
        eff = b.trade_date <= asof if inst.asset_class == AssetClass.FUTURE else _included(b, asof, basis)
        if eff:
            out[(b.location, b.account, b.instrument_id)] += b.side * b.quantity
    return dict(out)


def booking_cash(b: Booking, asof: date, basis: str, marks: Marks) -> Decimal:
    inst = INSTRUMENTS[b.instrument_id]
    if inst.asset_class == AssetClass.FUTURE:
        if b.trade_date > asof:
            return Decimal("0.00")
        return money(b.side * (b.quantity * marks[(b.instrument_id, asof)] * inst.price_factor - b.gross)) - b.fee
    if not _included(b, asof, basis):
        return Decimal("0.00")
    return -b.side * (b.gross + b.accrued) - b.fee


def cash(bookings: Iterable[Booking], opening: dict[Key3, Decimal], asof: date, marks: Marks,
         basis: str = "SD") -> dict[Key3, Decimal]:
    """{(location, account, currency): balance}."""
    out: dict[Key3, Decimal] = defaultdict(Decimal, opening)
    for b in bookings:
        out[(b.location, b.account, b.currency)] += booking_cash(b, asof, basis, marks)
    return dict(out)


def market_value(instrument_id: str, qty: Decimal, asof: date, marks: Marks) -> Decimal:
    """Local-currency value incl. bond accrued interest; futures carry no value (margined to cash)."""
    inst = INSTRUMENTS[instrument_id]
    if inst.asset_class == AssetClass.FUTURE or qty == 0:
        return Decimal("0.00")
    mv = qty * marks[(instrument_id, asof)] * inst.price_factor
    if inst.asset_class == AssetClass.BOND:
        mv += accrued_interest(inst, qty, asof)
    return money(mv)


def nav_usd(bookings: list[Booking], holdings: list[Holding], opening: dict[Key3, Decimal], asof: date,
            marks: Marks) -> dict[str, Decimal]:
    """Trade-date NAV per account in USD (positions at marks + projected cash)."""
    nav: dict[str, Decimal] = defaultdict(Decimal)
    for (loc, acct, iid), q in positions(bookings, holdings, asof, "TD").items():
        nav[acct] += market_value(iid, q, asof, marks) * FX_TO_USD[INSTRUMENTS[iid].currency]
    for (loc, acct, ccy), bal in cash(bookings, opening, asof, marks, "TD").items():
        nav[acct] += bal * FX_TO_USD[ccy]
    return {a: money(v) for a, v in nav.items()}
