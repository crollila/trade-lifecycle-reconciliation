"""Northbridge custodian: SWIFT-MT-*inspired* tag blocks.

The layout borrows the look of MT535 (holdings), MT950 (cash) and MT548
(settlement status): ``:tag:`` lines, ``YYYYMMDD`` / ``YYMMDD`` dates, ISO
15022-style qualifiers, ``C``/``D`` balance marks and **comma decimal
separators**. It is a teaching format, not a conformant SWIFT implementation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from ..model import AssetClass, SettlementStatus, Statement, StatementPosition
from ..refdata import ACCOUNT_CODE, ACCOUNT_XREF, INSTRUMENTS, NORTHBRIDGE, SYMBOL_CODE, SYMBOL_XREF
from . import dec


def _amt(x: Decimal) -> str:
    """Quantity in custodian notation: comma decimals, ``N`` prefix when negative."""
    return ("N" if x < 0 else "") + str(abs(x)).replace(".", ",")


def _parse_amt(s: str) -> Decimal:
    neg = s.startswith("N")
    v = dec(s.lstrip("N").replace(",", "."))
    return -v if neg else v


def render_statement(s: Statement) -> str:
    lines = ["{1:NBCUSTUS}{2:535}{4:", f":20:{s.statement_id}",
             f":97A::SAFE//{ACCOUNT_CODE[NORTHBRIDGE][s.account]}",
             f":98A::STAT//{s.as_of:%Y%m%d}", f":98A::PREP//{s.business_date:%Y%m%d}",
             f":98C::PREP//{s.received_at.astimezone(timezone.utc):%Y%m%d%H%M%S}"]
    for p in s.positions:
        inst = INSTRUMENTS[p.instrument_id]
        code = SYMBOL_CODE[NORTHBRIDGE][p.instrument_id]
        if inst.asset_class == AssetClass.BOND:
            lines += [f":35B:/XS/{code}", f":93B::AGGR//FAMT/{_amt(p.quantity)}"]
        else:
            lines += [f":35B:/TS/{code}", f":93B::AGGR//UNIT/{_amt(p.quantity)}"]
    for ccy, bal in s.cash:  # MT950-style closing balance
        mark = "C" if bal >= 0 else "D"
        lines.append(f":62F:{mark}{s.business_date:%y%m%d}{ccy}{abs(bal):.2f}".replace(".", ","))
    lines.append("-}")
    return "\n".join(lines)


_POS = re.compile(r":35B:/(XS|TS)/(\S+)\n:93B::AGGR//(FAMT|UNIT)/(\S+)")
_CASH = re.compile(r":62F:([CD])(\d{6})([A-Z]{3})([\d,]+)")


def parse_statement(raw: str) -> Statement:
    tag = dict(re.findall(r"^:(20|97A|98C):(?::\w+//)?(.+)$", raw, re.M))
    stat = re.search(r":98A::STAT//(\d{8})", raw).group(1)
    prep = re.search(r":98A::PREP//(\d{8})", raw).group(1)
    positions = tuple(StatementPosition(SYMBOL_XREF[NORTHBRIDGE][code], _parse_amt(q))
                      for _, code, _, q in _POS.findall(raw))
    cash = tuple((ccy, _parse_amt(amt) * (1 if mark == "C" else -1)) for mark, _, ccy, amt in _CASH.findall(raw))
    return Statement(NORTHBRIDGE, ACCOUNT_XREF[NORTHBRIDGE][tag["97A"]],
                     datetime.strptime(prep, "%Y%m%d").date(), datetime.strptime(stat, "%Y%m%d").date(),
                     positions, cash,
                     datetime.strptime(tag["98C"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc), tag["20"])


# MT548-style settlement status: STLD = settled, PEND/LACK|MONY = failing
def render_settlement(s: SettlementStatus) -> str:
    sett = "STLD" if s.status == "SETTLED" else "PENF"
    return (f"{{2:548}}:20C::RELA//{s.alloc_id}:25D::SETT//{sett}:24B::PEND//{s.reason or 'NONE'}"
            f":98A::ESET//{s.status_date:%Y%m%d}:98C::STAT//{s.received_at.astimezone(timezone.utc):%Y%m%d%H%M%S}")


def parse_settlement(raw: str) -> SettlementStatus:
    f = dict(re.findall(r":(\w+)::\w+//([^:]+)", raw))
    reason = f["24B"] if f["24B"] != "NONE" else ""
    return SettlementStatus(NORTHBRIDGE, f["20C"], "SETTLED" if f["25D"] == "STLD" else "FAILED", reason,
                            datetime.strptime(f["98A"], "%Y%m%d").date(),
                            datetime.strptime(f["98C"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc))

