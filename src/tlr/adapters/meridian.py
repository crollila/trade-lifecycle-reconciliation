"""Meridian prime broker / futures clearing broker: JSON.

Gotchas: side is encoded as a *signed* quantity; timestamps are epoch
milliseconds; securities use Meridian's own ``mrdId``; fees arrive as nested
components; the end-of-day activity file sends prices as JSON *numbers*
(binary floats), so they must be snapped back onto the tick grid.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from ..model import (
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
from ..refdata import ACCOUNT_CODE, ACCOUNT_XREF, INSTRUMENTS, MERIDIAN, SYMBOL_CODE, SYMBOL_XREF, fee_components
from . import content_hash, dec, to_tick

EVENT_OUT = {ExecType.ACK: "ACK", ExecType.FILL: "EXECUTION", ExecType.CORRECT: "EXECUTION_CORRECT",
             ExecType.BUST: "EXECUTION_BUST", ExecType.CANCEL: "CANCEL", ExecType.REJECT: "REJECT"}
EVENT_IN = {v: k for k, v in EVENT_OUT.items()}
STATE_OUT = {OrderStatus.ACKNOWLEDGED: "ACCEPTED", OrderStatus.PARTIALLY_FILLED: "WORKING",
             OrderStatus.FILLED: "FILLED", OrderStatus.CANCELED: "CANCELLED", OrderStatus.REJECTED: "REJECTED"}
STATE_IN = {v: k for k, v in STATE_OUT.items()}


def _ms(ts: datetime) -> int:
    return int(ts.astimezone(timezone.utc).timestamp() * 1000 + 0.5)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _signed(side: int, qty) -> int:
    return int(qty) * (1 if side == Side.BUY else -1)


def render_exec(e: ExecEvent) -> str:
    inst = INSTRUMENTS[e.instrument_id]
    body = {
        "eventType": EVENT_OUT[e.exec_type], "eventId": e.message_id,
        "account": {"id": ACCOUNT_CODE[MERIDIAN][e.account]},
        "order": {"clientRef": e.client_order_id, "brokerOrderId": e.venue_order_id,
                  "state": STATE_OUT[e.status], "sideSign": 1 if e.side == Side.BUY else -1},
        "security": {"mrdId": SYMBOL_CODE[MERIDIAN][e.instrument_id], "ccy": e.currency},
        "ts": _ms(e.trade_ts),
    }
    if e.exec_type in (ExecType.FILL, ExecType.CORRECT):
        body["execution"] = {"execId": e.venue_fill_id, "qty": _signed(e.side, e.quantity), "price": str(e.price)}
        body["fees"] = {k: str(v) for k, v in fee_components(MERIDIAN, inst, e.quantity, e.price).items()}
    if e.settlement_date:
        body["valueDate"] = e.settlement_date.isoformat()
    return json.dumps(body, separators=(",", ":"))


def parse_exec(raw: str) -> ExecEvent:
    m = json.loads(raw)
    exec_type = EVENT_IN[m["eventType"]]
    iid = SYMBOL_XREF[MERIDIAN][m["security"]["mrdId"]]
    ex = m.get("execution")
    qty = dec(abs(ex["qty"])) if ex else dec(0)
    side = (Side.BUY if ex["qty"] > 0 else Side.SELL) if ex else (Side.BUY if m["order"]["sideSign"] > 0 else Side.SELL)
    return ExecEvent(
        venue=MERIDIAN, exec_type=exec_type, message_id=m["eventId"], content_hash=content_hash(raw),
        client_order_id=m["order"]["clientRef"], venue_order_id=m["order"]["brokerOrderId"],
        account=ACCOUNT_XREF[MERIDIAN][m["account"]["id"]], instrument_id=iid, side=side,
        status=STATE_IN[m["order"]["state"]], trade_ts=_from_ms(m["ts"]), currency=m["security"]["ccy"],
        venue_fill_id=ex["execId"] if ex else None, quantity=qty,
        price=to_tick(dec(ex["price"]), INSTRUMENTS[iid].tick) if ex else dec(0),
        fee=sum((dec(v) for v in m["fees"].values()), dec("0.00")) if ex else None,
        settlement_date=date.fromisoformat(m["valueDate"]) if "valueDate" in m else None)


# ------------------------------------------------------- trade activity file
def render_drop_copy(trades: list[tuple[ExtTrade, str | None]], coverage_end: datetime,
                     float_noise: bool = True) -> str:
    rows = []
    for t, corr in trades:
        inst = INSTRUMENTS[t.instrument_id]
        comps = fee_components(MERIDIAN, inst, t.quantity, t.price)
        billed_extra = t.fee - sum(comps.values())  # anything off-schedule shows up as brokerage
        comps["brokerage"] += billed_extra
        price = float(t.price) * (1 + 1e-13) if float_noise else float(t.price)  # binary-float artefact
        rows.append({"execId": t.venue_fill_id, "brokerOrderId": t.venue_order_id, "clientRef": t.client_order_id,
                     "mrdId": SYMBOL_CODE[MERIDIAN][t.instrument_id], "qty": _signed(t.side, t.quantity),
                     "price": price, "ts": _ms(t.trade_ts), "ccy": t.currency,
                     "account": ACCOUNT_CODE[MERIDIAN][t.account],
                     "fees": {k: str(v) for k, v in comps.items()}, "correction": corr is not None})
    return json.dumps({"type": "TRADE_ACTIVITY", "coverageEnd": _ms(coverage_end), "trades": rows})


def parse_drop_copy(raw: str) -> tuple[datetime, list[ExtTrade]]:
    m = json.loads(raw)
    out = []
    for r in m["trades"]:
        iid = SYMBOL_XREF[MERIDIAN][r["mrdId"]]
        out.append(ExtTrade(
            venue=MERIDIAN, venue_fill_id=r["execId"], venue_order_id=r["brokerOrderId"], client_order_id=r["clientRef"],
            instrument_id=iid, side=Side.BUY if r["qty"] > 0 else Side.SELL, quantity=dec(abs(r["qty"])),
            price=to_tick(dec(repr(r["price"])), INSTRUMENTS[iid].tick),
            fee=sum((dec(v) for v in r["fees"].values()), dec("0.00")),
            trade_ts=_from_ms(r["ts"]), currency=r["ccy"], account=ACCOUNT_XREF[MERIDIAN][r["account"]]))
    return _from_ms(m["coverageEnd"]), out


# --------------------------------------------------- confirmation / custody
def render_confirm(c: Confirmation) -> str:
    return json.dumps({"eventType": "ALLOCATION_CONFIRM", "allocRef": c.alloc_id,
                       "account": ACCOUNT_CODE[MERIDIAN][c.account], "mrdId": SYMBOL_CODE[MERIDIAN][c.instrument_id],
                       "qty": _signed(c.side, c.quantity), "avgPrice": str(c.avg_price),
                       "valueDate": c.settle_date.isoformat(), "ts": _ms(c.received_at)})


def parse_confirm(raw: str) -> Confirmation:
    m = json.loads(raw)
    return Confirmation(MERIDIAN, m["allocRef"], ACCOUNT_XREF[MERIDIAN][m["account"]], SYMBOL_XREF[MERIDIAN][m["mrdId"]],
                        Side.BUY if m["qty"] > 0 else Side.SELL, dec(abs(m["qty"])), dec(m["avgPrice"]),
                        date.fromisoformat(m["valueDate"]), _from_ms(m["ts"]))


def render_settlement(s: SettlementStatus) -> str:
    return json.dumps({"eventType": "SETTLEMENT_STATUS", "allocRef": s.alloc_id, "state": s.status,
                       "reason": s.reason, "valueDate": s.status_date.isoformat(), "ts": _ms(s.received_at)})


def parse_settlement(raw: str) -> SettlementStatus:
    m = json.loads(raw)
    return SettlementStatus(MERIDIAN, m["allocRef"], m["state"], m["reason"], date.fromisoformat(m["valueDate"]),
                            _from_ms(m["ts"]))


def render_statement(s: Statement) -> str:
    return json.dumps({"eventType": "EOD_STATEMENT", "statementId": s.statement_id,
                       "account": ACCOUNT_CODE[MERIDIAN][s.account], "businessDate": s.business_date.isoformat(),
                       "asOf": s.as_of.isoformat(),
                       "positions": [{"mrdId": SYMBOL_CODE[MERIDIAN][p.instrument_id], "settledQty": str(p.quantity)}
                                     for p in s.positions],
                       "cash": [{"ccy": c, "balance": str(b)} for c, b in s.cash], "ts": _ms(s.received_at)})


def parse_statement(raw: str) -> Statement:
    m = json.loads(raw)
    return Statement(MERIDIAN, ACCOUNT_XREF[MERIDIAN][m["account"]], date.fromisoformat(m["businessDate"]),
                     date.fromisoformat(m["asOf"]),
                     tuple(StatementPosition(SYMBOL_XREF[MERIDIAN][p["mrdId"]], dec(p["settledQty"]))
                           for p in m["positions"]),
                     tuple((c["ccy"], dec(c["balance"])) for c in m["cash"]), _from_ms(m["ts"]), m["statementId"])
