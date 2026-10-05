"""BondHub bond RFQ platform: CSV.

Gotchas this adapter normalizes away:
* the real-time feed reports the **dealer's** side (we buy => ``DealerSide=S``);
* real-time face amounts are in **thousands** (``FaceAmt000``);
* real-time timestamps are **New York local time** with no offset (DST matters);
* the fee arrives as **basis points**, not an amount;
* the drop-copy file uses a *different* layout: semicolons, the *client* side,
  full face, UTC ISO timestamps and a billed fee amount.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ..calendar import NEW_YORK
from ..model import Confirmation, ExecEvent, ExecType, ExtTrade, OrderStatus, Side, money
from ..refdata import ACCOUNT_CODE, ACCOUNT_XREF, BONDHUB, BONDHUB_FEE_BPS, INSTRUMENTS, SYMBOL_CODE, SYMBOL_XREF
from . import content_hash, dec, to_tick

RT_HEADER = ["RecType", "TicketNo", "ClientRef", "PlatformOrderId", "Isin", "DealerSide", "FaceAmt000",
             "CleanPx", "TradeDateTimeNY", "SettleDate", "Ccy", "FeeBps", "Account", "Status"]
REC_OUT = {ExecType.ACK: "ACK", ExecType.FILL: "FILL", ExecType.CORRECT: "CORR", ExecType.BUST: "BUST",
           ExecType.CANCEL: "CXL", ExecType.REJECT: "REJ"}
REC_IN = {v: k for k, v in REC_OUT.items()}
STATUS_OUT = {OrderStatus.ACKNOWLEDGED: "NEW-ACK", OrderStatus.PARTIALLY_FILLED: "PART",
              OrderStatus.FILLED: "DONE", OrderStatus.CANCELED: "CXL", OrderStatus.REJECTED: "REJ"}
STATUS_IN = {v: k for k, v in STATUS_OUT.items()}

DC_HEADER = ["TradeRef", "Isin", "ClientSide", "Nominal", "Price", "ExecTimeUTC", "FeeAmt", "ClientRef",
             "PlatformOrderId", "Account", "RecType"]


def _row(values: list[str], delim: str = ",") -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="").writerow(values)
    return buf.getvalue()


def _ny_text(ts: datetime) -> str:
    return ts.astimezone(NEW_YORK).strftime("%m/%d/%Y %H:%M:%S")


def render_exec(e: ExecEvent) -> str:
    is_trade = e.exec_type in (ExecType.FILL, ExecType.CORRECT)
    dealer_side = "S" if e.side == Side.BUY else "B"
    bps = BONDHUB_FEE_BPS[e.currency] if is_trade else ""
    return _row([
        REC_OUT[e.exec_type], e.message_id, e.client_order_id, e.venue_order_id,
        SYMBOL_CODE[BONDHUB][e.instrument_id], dealer_side,
        str(e.quantity / 1000) if is_trade else "", str(e.price) if is_trade else "",
        _ny_text(e.trade_ts), e.settlement_date.strftime("%m/%d/%Y") if e.settlement_date else "",
        e.currency, str(bps), ACCOUNT_CODE[BONDHUB][e.account], STATUS_OUT[e.status]])


def parse_exec(raw: str) -> ExecEvent:
    r = dict(zip(RT_HEADER, next(csv.reader([raw]))))
    exec_type = REC_IN[r["RecType"]]
    iid = SYMBOL_XREF[BONDHUB][r["Isin"]]
    inst = INSTRUMENTS[iid]
    is_trade = exec_type in (ExecType.FILL, ExecType.CORRECT)
    local = datetime.strptime(r["TradeDateTimeNY"], "%m/%d/%Y %H:%M:%S").replace(tzinfo=NEW_YORK)
    qty = (dec(r["FaceAmt000"]) * 1000).quantize(dec(1)) if is_trade else dec(0)
    price = to_tick(dec(r["CleanPx"]), inst.tick) if is_trade else dec(0)
    fee = money(qty * price * inst.price_factor * dec(r["FeeBps"]) / 10000) if is_trade else None
    return ExecEvent(
        venue=BONDHUB, exec_type=exec_type, message_id=f'{r["RecType"]}:{r["TicketNo"]}',
        content_hash=content_hash(raw), client_order_id=r["ClientRef"], venue_order_id=r["PlatformOrderId"],
        account=ACCOUNT_XREF[BONDHUB][r["Account"]], instrument_id=iid,
        side=Side.SELL if r["DealerSide"] == "B" else Side.BUY,  # flip dealer perspective
        status=STATUS_IN[r["Status"]], trade_ts=local.astimezone(timezone.utc), currency=r["Ccy"],
        venue_fill_id=r["TicketNo"] if is_trade else None, quantity=qty, price=price, fee=fee,
        settlement_date=datetime.strptime(r["SettleDate"], "%m/%d/%Y").date() if r["SettleDate"] else None)


# ------------------------------------------------------------ drop copy file
def render_drop_copy(trades: list[tuple[ExtTrade, str | None]], coverage_end: datetime) -> str:
    lines = [f"#COVERAGE_END_UTC={coverage_end.astimezone(timezone.utc).isoformat()}", _row(DC_HEADER, ";")]
    for t, corr in trades:
        lines.append(_row([t.venue_fill_id, SYMBOL_CODE[BONDHUB][t.instrument_id], "B" if t.side == Side.BUY else "S",
                           str(t.quantity), str(t.price), t.trade_ts.astimezone(timezone.utc).isoformat(),
                           str(t.fee), t.client_order_id, t.venue_order_id, ACCOUNT_CODE[BONDHUB][t.account],
                           "CORR" if corr else "NEW"], ";"))
    return "\n".join(lines)


def parse_drop_copy(raw: str) -> tuple[datetime, list[ExtTrade]]:
    head, *body = raw.splitlines()
    coverage_end = datetime.fromisoformat(head.split("=", 1)[1])
    out = []
    for r in csv.DictReader(body, delimiter=";"):
        iid = SYMBOL_XREF[BONDHUB][r["Isin"]]
        out.append(ExtTrade(
            venue=BONDHUB, venue_fill_id=r["TradeRef"], venue_order_id=r["PlatformOrderId"],
            client_order_id=r["ClientRef"], instrument_id=iid,
            side=Side.BUY if r["ClientSide"] == "B" else Side.SELL, quantity=dec(r["Nominal"]),
            price=to_tick(dec(r["Price"]), INSTRUMENTS[iid].tick), fee=dec(r["FeeAmt"]),
            trade_ts=datetime.fromisoformat(r["ExecTimeUTC"]), currency=INSTRUMENTS[iid].currency,
            account=ACCOUNT_XREF[BONDHUB][r["Account"]]))
    return coverage_end, out


# --------------------------------------------------- allocation confirmation
def render_confirm(c: Confirmation) -> str:
    return _row(["CONF", c.alloc_id, c.account, SYMBOL_CODE[BONDHUB][c.instrument_id],
                 "B" if c.side == Side.BUY else "S", str(c.quantity), str(c.avg_price),
                 c.settle_date.strftime("%m/%d/%Y"), c.received_at.astimezone(timezone.utc).isoformat()])


def parse_confirm(raw: str) -> Confirmation:
    _, alloc, acct, isin, side, nominal, px, sd, ts = next(csv.reader([raw]))
    return Confirmation(BONDHUB, alloc, acct, SYMBOL_XREF[BONDHUB][isin], Side.BUY if side == "B" else Side.SELL,
                        dec(nominal), dec(px), datetime.strptime(sd, "%m/%d/%Y").date(), datetime.fromisoformat(ts))
