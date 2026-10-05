"""Business-day calendars, settlement cycles, local-time helpers and day counts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .model import AssetClass

NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc

# Settlement cycles in business days. US equities and Treasuries moved to T+1
# in May 2024; EU cash markets remain T+2 during the simulated window. Futures
# do not settle by delivery-versus-payment; they are margined daily (T+0).
SETTLEMENT_CYCLE: dict[tuple[str, str], int] = {
    (AssetClass.EQUITY, "USD"): 1,
    (AssetClass.EQUITY, "EUR"): 2,
    (AssetClass.BOND, "USD"): 1,
    (AssetClass.BOND, "EUR"): 2,
    (AssetClass.FUTURE, "USD"): 0,
}


class BusinessCalendar:
    """Weekends plus an explicit holiday set (no holidays fall in the default window)."""

    def __init__(self, holidays: frozenset[date] | set[date] = frozenset()):
        self.holidays = frozenset(holidays)

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def add_business_days(self, d: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        cur = d
        while remaining:
            cur += timedelta(days=step)
            if self.is_business_day(cur):
                remaining -= 1
        return cur

    def next_business_day(self, d: date) -> date:
        return self.add_business_days(d, 1)

    def prev_business_day(self, d: date) -> date:
        return self.add_business_days(d, -1)

    def business_days(self, start: date, count: int) -> list[date]:
        days: list[date] = []
        cur = start
        while len(days) < count:
            if self.is_business_day(cur):
                days.append(cur)
            cur += timedelta(days=1)
        return days


def settlement_date(trade_date: date, asset_class: str, currency: str, cal: BusinessCalendar) -> date:
    cycle = SETTLEMENT_CYCLE[(asset_class, currency)]
    return cal.add_business_days(trade_date, cycle) if cycle else trade_date


def ny(d: date, hhmm: str, seconds: int = 0, millis: int = 0) -> datetime:
    """New York wall-clock time on ``d`` as an aware UTC datetime (DST-correct)."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = datetime.combine(d, time(hh, mm), tzinfo=NEW_YORK)
    return (local + timedelta(seconds=seconds, milliseconds=millis)).astimezone(UTC)


def to_ny(ts: datetime) -> datetime:
    return ts.astimezone(NEW_YORK)


def ny_date(ts: datetime) -> date:
    return ts.astimezone(NEW_YORK).date()


# ---------------------------------------------------------------- day counts

def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, d.day)  # synthetic bonds use day-of-month <= 28


def coupon_period(maturity: date, freq: int, settle: date) -> tuple[date, date]:
    """(last coupon date <= settle, next coupon date > settle) stepping back from maturity."""
    step = 12 // freq
    nxt = maturity
    while True:
        prev = _add_months(nxt, -step)
        if prev <= settle:
            return prev, nxt
        nxt = prev


def days_30_360(d1: date, d2: date) -> int:
    """30/360 (US bond basis), ignoring the February end-of-month rules."""
    dd1 = min(d1.day, 30)
    dd2 = 30 if (d2.day == 31 and dd1 == 30) else d2.day
    return 360 * (d2.year - d1.year) + 30 * (d2.month - d1.month) + (dd2 - dd1)


def accrued_fraction(maturity: date, freq: int, day_count: str, settle: date) -> Decimal:
    """Fraction of the *annual* coupon accrued at ``settle``."""
    last, nxt = coupon_period(maturity, freq, settle)
    if day_count == "30/360":
        return Decimal(days_30_360(last, settle)) / Decimal(360)
    if day_count == "ACT/ACT":  # ACT/ACT ICMA
        return Decimal((settle - last).days) / Decimal((nxt - last).days) / Decimal(freq)
    raise ValueError(f"unsupported day count {day_count}")
