"""Block allocation: split an executed block across accounts at the average price.

Conservation is exact by construction: quantities are split in lots, and gross
notional and fees are split in whole cents with the largest-remainder method,
so the account shares always sum to the block totals to the cent.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from .calendar import BusinessCalendar, accrued_fraction, settlement_date
from .model import AssetClass, Booking, Instrument, Order, money


def split_largest_remainder(total: int, weights: list[Decimal]) -> list[int]:
    """Split an integer ``total`` (e.g. cents or lots) proportionally to ``weights``."""
    wsum = sum(weights)
    if total == 0 or wsum == 0:
        return [0] * len(weights)
    raw = [Decimal(total) * w / wsum for w in weights]
    base = [int(r.to_integral_value(rounding=ROUND_FLOOR)) for r in raw]
    short = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:short]:
        base[i] += 1
    return base


def accrued_interest(inst: Instrument, face: Decimal, settle) -> Decimal:
    if inst.asset_class != AssetClass.BOND or settle is None:
        return Decimal("0.00")
    return money(face * inst.coupon * accrued_fraction(inst.maturity, inst.coupon_freq, inst.day_count, settle))


def allocate(order: Order, fills: list[tuple[Decimal, Decimal, Decimal]], inst: Instrument, location: str,
             cal: BusinessCalendar) -> list[Booking]:
    """``fills`` = [(quantity, price, fee)]. Returns one booking per account with a non-zero share."""
    block_qty = sum((q for q, _, _ in fills), Decimal(0))
    if block_qty == 0:
        return []
    block_gross = money(sum(q * p * inst.price_factor for q, p, _ in fills))
    block_fee = sum((f for _, _, f in fills), Decimal("0.00"))

    accounts = [a for a, _ in order.allocations]
    weights = [w for _, w in order.allocations]
    lots = split_largest_remainder(int(block_qty / inst.lot), weights)
    qtys = [inst.lot * n for n in lots]
    gross_c = split_largest_remainder(int(block_gross * 100), qtys)
    fee_c = split_largest_remainder(int(block_fee * 100), qtys)

    sd = settlement_date(order.trade_date, inst.asset_class, inst.currency, cal)
    out = []
    for acct, q, g, f in zip(accounts, qtys, gross_c, fee_c):
        if q == 0:
            continue
        out.append(Booking(
            alloc_id=f"{order.client_order_id}-{acct}", client_order_id=order.client_order_id, account=acct,
            location=location, instrument_id=inst.instrument_id, side=order.side, quantity=q,
            gross=Decimal(g) / 100, fee=Decimal(f) / 100, accrued=accrued_interest(inst, q, sd),
            trade_date=order.trade_date, settle_date=sd, currency=inst.currency))
    return out
