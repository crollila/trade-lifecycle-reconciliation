"""AlphaX equity broker: FIX 4.4 tag=value (SOH shown as ``|``)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from ..model import Confirmation, ExecEvent, ExecType, ExtTrade, OrderStatus, Side
from ..refdata import ACCOUNT_CODE, ACCOUNT_XREF, ALPHAX, INSTRUMENTS, SYMBOL_CODE, SYMBOL_XREF
from . import content_hash, dec, to_tick

EXEC_TYPE_OUT = {ExecType.ACK: "0", ExecType.FILL: "F", ExecType.CORRECT: "G", ExecType.BUST: "H",
                 ExecType.CANCEL: "4", ExecType.REJECT: "8"}
EXEC_TYPE_IN = {v: k for k, v in EXEC_TYPE_OUT.items()}
ORD_STATUS_OUT = {OrderStatus.ACKNOWLEDGED: "0", OrderStatus.PARTIALLY_FILLED: "1", OrderStatus.FILLED: "2",
                  OrderStatus.CANCELED: "4", OrderStatus.REJECTED: "8"}
ORD_STATUS_IN = {v: k for k, v in ORD_STATUS_OUT.items()}
SIDE_IN = {"1": Side.BUY, "2": Side.SELL, "5": Side.SELL}  # 5 = sell short


def _fix_ts(ts: datetime) -> str:
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def _parse_fix_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d-%H:%M:%S.%f").replace(tzinfo=timezone.utc)


def _join(fields: list[tuple[str, str]]) -> str:
    return "|".join(f"{k}={v}" for k, v in fields)


def _split(raw: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in raw.strip().split("|") if part)


def render_exec(e: ExecEvent, seq: int, price_text: str | None = None) -> str:
    f = [("8", "FIX.4.4"), ("35", "8"), ("49", "ALPHAX"), ("56", "TLRFIRM"), ("34", str(seq)),
         ("52", _fix_ts(e.trade_ts)), ("1", ACCOUNT_CODE[ALPHAX][e.account]), ("11", e.client_order_id),
         ("37", e.venue_order_id), ("17", e.message_id), ("150", EXEC_TYPE_OUT[e.exec_type]),
         ("39", ORD_STATUS_OUT[e.status]), ("55", SYMBOL_CODE[ALPHAX][e.instrument_id]),
         ("54", "1" if e.side == Side.BUY else "2"), ("15", e.currency), ("60", _fix_ts(e.trade_ts))]
    if e.exec_type in (ExecType.FILL, ExecType.CORRECT):
        f += [("32", str(e.quantity)), ("31", price_text or str(e.price))]
    if e.settlement_date:
        f.append(("64", e.settlement_date.strftime("%Y%m%d")))
    if e.ref_fill_id:
        f.append(("19", e.ref_fill_id))
    f.append(("10", "000"))
    return _join(f)


def parse_exec(raw: str) -> ExecEvent:
    t = _split(raw)
    exec_type = EXEC_TYPE_IN[t["150"]]
    iid = SYMBOL_XREF[ALPHAX][t["55"]]
    inst = INSTRUMENTS[iid]
    is_trade = exec_type in (ExecType.FILL, ExecType.CORRECT)
    return ExecEvent(
        venue=ALPHAX, exec_type=exec_type, message_id=t["17"], content_hash=content_hash(raw),
        client_order_id=t["11"], venue_order_id=t["37"], account=ACCOUNT_XREF[ALPHAX][t["1"]],
        instrument_id=iid, side=SIDE_IN[t["54"]], status=ORD_STATUS_IN[t["39"]],
        trade_ts=_parse_fix_ts(t["60"]), currency=t["15"],
        venue_fill_id=(t.get("19") or t["17"]) if is_trade or exec_type == ExecType.BUST else None,
        ref_fill_id=t.get("19"),
        quantity=dec(t["32"]) if is_trade else dec(0),
        price=to_tick(dec(t["31"]), inst.tick) if is_trade else dec(0),
        settlement_date=datetime.strptime(t["64"], "%Y%m%d").date() if "64" in t else None,
    )


# ------------------------------------------------------------ drop copy file
def render_drop_copy(trades: list[tuple[ExtTrade, str | None]], coverage_end: datetime) -> str:
    """``trades`` = (record, correction exec id or None)."""
    lines = [f"#ALPHAX-DROPCOPY coverage_end={_fix_ts(coverage_end)}"]
    for t, corr_id in trades:
        f = [("35", "8"), ("150", "G" if corr_id else "F"), ("17", corr_id or t.venue_fill_id),
             ("1", ACCOUNT_CODE[ALPHAX][t.account]), ("11", t.client_order_id), ("37", t.venue_order_id),
             ("55", SYMBOL_CODE[ALPHAX][t.instrument_id]), ("54", "1" if t.side == Side.BUY else "2"),
             ("32", str(t.quantity)), ("31", str(t.price)), ("15", t.currency), ("60", _fix_ts(t.trade_ts)),
             ("12", str(t.fee)), ("13", "3")]
        if corr_id:
            f.append(("19", t.venue_fill_id))
        lines.append(_join(f))
    return "\n".join(lines)


def parse_drop_copy(raw: str) -> tuple[datetime, list[ExtTrade]]:
    head, *rows = raw.splitlines()
    coverage_end = _parse_fix_ts(head.split("coverage_end=")[1])
    out = []
    for row in rows:
        t = _split(row)
        iid = SYMBOL_XREF[ALPHAX][t["55"]]
        out.append(ExtTrade(
            venue=ALPHAX, venue_fill_id=t.get("19") or t["17"], venue_order_id=t["37"], client_order_id=t["11"],
            instrument_id=iid, side=SIDE_IN[t["54"]], quantity=dec(t["32"]),
            price=to_tick(dec(t["31"]), INSTRUMENTS[iid].tick), fee=dec(t["12"]),
            trade_ts=_parse_fix_ts(t["60"]), currency=t["15"], account=ACCOUNT_XREF[ALPHAX][t["1"]]))
    return coverage_end, out


# --------------------------------------------------- allocation confirmation
def render_confirm(c: Confirmation) -> str:
    return _join([("35", "AK"), ("70", c.alloc_id), ("79", c.account), ("55", SYMBOL_CODE[ALPHAX][c.instrument_id]),
                  ("54", "1" if c.side == Side.BUY else "2"), ("53", str(c.quantity)), ("6", str(c.avg_price)),
                  ("64", c.settle_date.strftime("%Y%m%d")), ("60", _fix_ts(c.received_at))])


def parse_confirm(raw: str) -> Confirmation:
    t = _split(raw)
    return Confirmation(ALPHAX, t["70"], t["79"], SYMBOL_XREF[ALPHAX][t["55"]], SIDE_IN[t["54"]], dec(t["53"]),
                        dec(t["6"]), datetime.strptime(t["64"], "%Y%m%d").date(), _parse_fix_ts(t["60"]))


def settle_date_from_tag(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()
