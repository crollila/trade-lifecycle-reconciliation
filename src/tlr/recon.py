"""Reconciliation engine.

Runs on the bus clock and never looks at simulator ground truth: its only
inputs are our books (``Capture``), the counterparties' records (drop copies,
confirmations, settlement statuses, statements), public marks and the
exception queue itself.

Families and schedule
* TRADE / FEE   - every 15 minutes intraday + after the close: internal fills
  vs venue drop copies, inside each venue's coverage watermark (T+0).
* SETTLEMENT    - end of day: contractually due settlements vs custodian status.
* POSITION/CASH - end of day: custodian statements vs an *expected-external*
  model = our books + the known effect of every break already open. Only the
  unexplained residual can create a new position/cash break, so one root cause
  produces one exception, not a cascade.
* CONFIRM       - after the confirmation cutoff: allocations with no confirm.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from . import ledger
from .allocation import allocate
from .bus import Event, InProcessBus
from .calendar import BusinessCalendar, ny
from .capture import Capture
from .exceptions import ExceptionQueue, Observation
from .model import Booking, Holding, Side, money
from .model import BreakType as B
from .refdata import FX_TO_USD, INSTRUMENTS, SETTLEMENT_LOCATION
from .store import Store

TRADE_SQL = """
SELECT venue, venue_fill_id, client_order_id, instrument_id, internal_qty, venue_qty, internal_price,
       venue_price, expected_fee, billed_fee, trade_ts, match_status
FROM v_unmatched_fills ORDER BY venue, venue_fill_id
"""
COMPARED_SQL = """
SELECT count(*) FROM v_fills_current f JOIN v_venue_watermark w USING (venue) WHERE f.trade_ts <= w.watermark
"""


def _usd(amount: Decimal, ccy: str) -> Decimal:
    return money(abs(amount) * FX_TO_USD[ccy])


class ReconEngine:
    def __init__(self, bus: InProcessBus, capture: Capture, store: Store, queue: ExceptionQueue,
                 cal: BusinessCalendar, marks, holdings: list[Holding], opening_cash, confirm_cutoff: str):
        self.bus, self.cap, self.store, self.queue, self.cal = bus, capture, store, queue, cal
        self.marks, self.holdings, self.opening_cash = marks, holdings, opening_cash
        self.opening_keys = {(h.location, h.account, h.instrument_id) for h in holdings}
        self.confirm_cutoff = confirm_cutoff
        self.run_id = 0
        self._trade_cache: tuple[int, list, int] | None = None
        bus.subscribe("recon.tick", self.on_tick)

    # --------------------------------------------------------------- driver
    def on_tick(self, ev: Event) -> None:
        kind, as_of, now = ev.payload["kind"], ev.payload.get("as_of"), ev.ts
        label = kind + ("_RERUN" if ev.payload.get("rerun") else "")
        if kind == "TRADE":
            self._record(label, now, None, *self.run_trade(now))
        elif kind == "EOD":
            self._record("TRADE", now, None, *self.run_trade(now))
            self._record("SETTLEMENT" + label[3:], now, as_of, *self.run_settlement(as_of, now))
            self._record("POSITION_CASH" + label[3:], now, as_of, *self.run_positions_cash(as_of, now))
        elif kind == "CONFIRM":
            self._record(label, now, as_of, *self.run_confirm(as_of, now))

    def _record(self, kind: str, now: datetime, as_of: date | None, compared: int, observed: int, new: int,
                closed: int) -> None:
        self.run_id += 1
        self.store.add("recon_runs", run_id=self.run_id, run_ts=now, kind=kind, as_of=as_of, records_compared=compared,
                       breaks_observed=observed, new_exceptions=new, closed_exceptions=closed,
                       open_exceptions=len(self.queue.open_by_key))
        for status, n in sorted(self.queue.status_counts().items()):
            self.store.add("exception_status_history", run_id=self.run_id, sample_ts=now, status=status, n=n)

    def _under_trade_break(self, oid: str) -> bool:
        return bool(self.queue.open_for(f"order:{oid}", ("TRADE", "FEE")))

    # ---------------------------------------------------------- T+0 trades
    def run_trade(self, now: datetime) -> tuple[int, int, int, int]:
        if self._trade_cache and self._trade_cache[0] == self.cap.trade_data_version:
            _, rows, compared = self._trade_cache  # nothing new arrived: same inputs, same answer
        else:
            tables = ("fills", "ext_trades", "ext_feed_files", "instruments")
            rows = self.store.rows(TRADE_SQL, tables=tables)
            compared = self.store.rows(COMPARED_SQL, tables=tables)[0][0]
            self._trade_cache = (self.cap.trade_data_version, rows, compared)
        trade_obs, fee_obs = [], []
        for (venue, fid, oid, iid, iq, vq, ip, vp, efee, bfee, ts, status) in rows:
            inst = INSTRUMENTS[iid]
            ccy, fx_factor = inst.currency, inst.price_factor
            related = {f"fill:{venue}:{fid}", f"order:{oid}"}
            src = [{"side": "internal", "qty": iq, "price": ip, "fee": efee},
                   {"side": "venue", "qty": vq, "price": vp, "fee": bfee, "trade_ts": ts}]
            common = dict(venue=venue, instrument_id=iid, client_order_id=oid, account="BLOCK", source_records=src)
            key = f"TRADE|{venue}|{fid}"
            if status == "MISSING_INTERNALLY":
                ext = self.cap.ext[(venue, fid)]
                trade_obs.append(Observation(
                    B.MISSING_TRADE, key, related, f"fill {vq} @ {vp} (venue)", "no internal fill", vq,
                    _usd(vq * vp * fx_factor, ccy), fix={"action": "LATE_BOOK", "ext": ext}, **common))
            elif status == "MISSING_AT_VENUE":
                dup_of = self._duplicate_of(venue, fid)
                if dup_of:
                    trade_obs.append(Observation(
                        B.DUPLICATE_FILL, key, related | {f"fill:{venue}:{dup_of}"},
                        f"single fill {dup_of}", f"second fill {fid} with identical qty/price/time", iq,
                        _usd(iq * ip * fx_factor, ccy), fix={"action": "BUST", "venue": venue, "fill": fid}, **common))
                else:
                    trade_obs.append(Observation(
                        B.MISSING_TRADE, key, related, "venue record", f"internal fill {iq} @ {ip} unknown to venue",
                        iq, _usd(iq * ip * fx_factor, ccy), fix={"action": "ESCALATE_VENUE"}, **common))
            elif status == "QUANTITY_DIFF":
                trade_obs.append(Observation(
                    B.QUANTITY_MISMATCH, key, related, f"qty {vq}", f"qty {iq}", iq - vq,
                    _usd((iq - vq) * vp * fx_factor, ccy), fix={"action": "CORRECT", "ext": self.cap.ext[(venue, fid)]},
                    **common))
            elif status == "PRICE_DIFF":
                trade_obs.append(Observation(
                    B.PRICE_MISMATCH, key, related, f"price {vp}", f"price {ip}", ip - vp,
                    _usd((ip - vp) * iq * fx_factor, ccy), fix={"action": "CORRECT", "ext": self.cap.ext[(venue, fid)]},
                    **common))
            elif status == "FEE_DIFF":
                fee_obs.append(Observation(
                    B.FEE_MISMATCH, f"FEE|{venue}|{fid}", related, f"fee {efee} (schedule)", f"billed {bfee}",
                    bfee - efee, _usd(bfee - efee, ccy), fix={"action": "DISPUTE", "venue": venue, "fill": fid},
                    **common))
        n1, c1 = self.queue.reconcile_run(("TRADE",), trade_obs, now)
        n2, c2 = self.queue.reconcile_run(("FEE",), fee_obs, now)
        return compared, len(trade_obs) + len(fee_obs), n1 + n2, c1 + c2

    def _duplicate_of(self, venue: str, fid: str) -> str | None:
        f = self.cap.fills.get((venue, fid))
        if f is None:
            return None
        for key in sorted(self.cap.fills_by_order.get(f.client_order_id, ())):
            g = self.cap.fills[key]
            if (key[1] != fid and key in self.cap.ext and g.quantity == f.quantity and g.price == f.price
                    and abs((g.trade_ts - f.trade_ts).total_seconds()) <= 5):
                return key[1]
        return None

    # ----------------------------------------------------------- settlement
    def run_settlement(self, as_of: date, now: datetime) -> tuple[int, int, int, int]:
        obs, compared = [], 0
        for alloc_id in sorted(self.cap.bookings):
            b = self.cap.bookings[alloc_id]
            if not INSTRUMENTS[b.instrument_id].settles or b.settle_date > as_of:
                continue
            compared += 1
            st = self.cap.settlement_as_of(alloc_id, as_of)
            if st and st.status == "SETTLED":
                continue
            if st is None and self._under_trade_break(b.client_order_id):
                continue  # upstream break explains it; don't double-report
            obs.append(Observation(
                B.FAILED_SETTLEMENT, f"SETTLE|{alloc_id}", {f"alloc:{alloc_id}", f"order:{b.client_order_id}"},
                f"settled on {b.settle_date}", f"{st.status} ({st.reason})" if st else "no settlement status",
                b.quantity, _usd(b.gross, b.currency), as_of=as_of, location=b.location, account=b.account,
                instrument_id=b.instrument_id, client_order_id=b.client_order_id,
                source_records=[{"alloc_id": alloc_id, "qty": b.quantity, "settle_date": b.settle_date,
                                 "custodian_status": st.status if st else None, "reason": st.reason if st else None}],
                fix={"action": "CHASE_FAIL", "alloc_id": alloc_id}))
        new, closed = self.queue.reconcile_run(("SETTLEMENT",), obs, now, as_of)
        return compared, len(obs), new, closed

    # ------------------------------------------------------ positions & cash
    def expected_external_bookings(self) -> list[Booking]:
        """Our books, adjusted by the known effect of every open break."""
        shadow = sorted({e.obs.client_order_id for e in self.queue.open_in(("TRADE", "FEE")) if e.obs.client_order_id})
        out: list[Booking] = []
        for oid, bks in self.cap.bookings_by_order.items():
            if oid not in shadow:
                out.extend(bks.values())
        for oid in shadow:
            o = self.cap.orders[oid]
            if o.trade_date not in self.cap.allocated_dates:
                continue
            ext = self.cap.order_ext_fills(oid)
            out.extend(allocate(o, [(t.quantity, t.price, t.fee) for t in ext], INSTRUMENTS[o.instrument_id],
                                SETTLEMENT_LOCATION[o.venue], self.cal))
        failing = {e.obs.fix["alloc_id"] for e in self.queue.open_in(("SETTLEMENT",))}
        return [replace(b, settle_date=None) if b.alloc_id in failing else b for b in out]

    def run_positions_cash(self, as_of: date, now: datetime) -> tuple[int, int, int, int]:
        stmts = {(l, a): s for (l, a, d), s in sorted(self.cap.statements.items()) if d == as_of}
        pos_obs: list[Observation] = []
        covered = set()
        for (loc, acct), s in stmts.items():
            if s.as_of < as_of:
                pos_obs.append(Observation(
                    B.STALE_POSITION, f"STALE|{loc}|{acct}|{as_of}", {f"stmt:{loc}:{acct}:{as_of}"},
                    f"statement as of {as_of}", f"statement as of {s.as_of}", Decimal((as_of - s.as_of).days),
                    Decimal(0), as_of=as_of, location=loc, account=acct,
                    source_records=[{"statement_id": s.statement_id, "as_of": s.as_of, "received_at": s.received_at}],
                    fix={"action": "RESEND", "location": loc, "account": acct, "business_date": as_of}))
            else:
                covered.add((loc, acct))

        exp_bookings = self.expected_external_bookings()
        exp_pos = ledger.positions(exp_bookings, self.holdings, as_of, "SD")
        ext_pos = {(loc, acct, p.instrument_id): p.quantity for (loc, acct), s in stmts.items()
                   if (loc, acct) in covered for p in s.positions}
        residual = {}
        for k in sorted(set(exp_pos) | set(ext_pos)):
            if (k[0], k[1]) in covered:
                diff = ext_pos.get(k, Decimal(0)) - exp_pos.get(k, Decimal(0))
                if diff != 0:
                    residual[k] = diff

        moves: dict[str, str] = {}
        by_li: dict[tuple[str, str], list[tuple[str, Decimal]]] = {}
        for (loc, acct, iid), diff in residual.items():
            by_li.setdefault((loc, iid), []).append((acct, diff))
        for (loc, iid), items in sorted(by_li.items()):
            for i, (x, rx) in enumerate(items):
                for y, ry in items[i + 1:]:
                    if (loc, x, iid) not in residual or (loc, y, iid) not in residual or rx != -ry:
                        continue
                    b = self._misbooked(exp_bookings, loc, iid, x, rx, y, as_of)
                    if not b:
                        continue
                    other = y if b.account == x else x
                    moves[b.alloc_id] = other
                    del residual[(loc, x, iid)], residual[(loc, y, iid)]
                    inst = INSTRUMENTS[iid]
                    pos_obs.append(Observation(
                        B.WRONG_ACCOUNT_ALLOCATION, f"WRONGACCT|{loc}|{iid}|{b.account}|{other}",
                        {f"alloc:{b.alloc_id}", f"pos:{loc}:{x}:{iid}", f"pos:{loc}:{y}:{iid}"},
                        f"{b.quantity} in {b.account}", f"booked to {other}", b.quantity,
                        _usd(b.quantity * self.marks[(iid, as_of)] * inst.price_factor, inst.currency), as_of=as_of,
                        location=loc, account=b.account, instrument_id=iid, client_order_id=b.client_order_id,
                        source_records=[{"account": x, "diff": rx}, {"account": y, "diff": ry},
                                        {"alloc_id": b.alloc_id}],
                        fix={"action": "JOURNAL", "location": loc, "instrument_id": iid, "from_account": other,
                             "to_account": b.account, "quantity": b.quantity}))
        for (loc, acct, iid), diff in residual.items():
            inst = INSTRUMENTS[iid]
            impact = _usd(diff * self.marks[(iid, as_of)] * inst.price_factor, inst.currency)
            common = dict(as_of=as_of, location=loc, account=acct, instrument_id=iid,
                          source_records=[{"internal_expected": exp_pos.get((loc, acct, iid), Decimal(0)),
                                           "custodian": ext_pos.get((loc, acct, iid), Decimal(0))}])
            if (loc, acct, iid) not in self.cap.ever_booked and (loc, acct, iid) not in self.opening_keys:
                pos_obs.append(Observation(
                    B.UNEXPECTED_POSITION, f"UNEXP|{loc}|{acct}|{iid}", {f"pos:{loc}:{acct}:{iid}"}, "no position",
                    f"custodian holds {ext_pos.get((loc, acct, iid))}", diff, impact,
                    fix={"action": "INVESTIGATE", "location": loc, "account": acct, "instrument_id": iid,
                         "quantity": diff}, **common))
            else:
                pos_obs.append(Observation(
                    B.POSITION_MISMATCH, f"POS|{loc}|{acct}|{iid}", {f"pos:{loc}:{acct}:{iid}"},
                    f"{exp_pos.get((loc, acct, iid), Decimal(0))}", f"{ext_pos.get((loc, acct, iid), Decimal(0))}",
                    diff, impact, fix={"action": "ESCALATE"}, **common))
        blind = {(loc, acct) for (loc, acct) in stmts if (loc, acct) not in covered}
        n1, c1 = self.queue.reconcile_run(("POSITION",), pos_obs, now, as_of, blind)

        cash_bookings = [replace(b, account=moves[b.alloc_id]) if b.alloc_id in moves else b for b in exp_bookings]
        exp_cash = ledger.cash(cash_bookings, self.opening_cash, as_of, self.marks, "SD")
        ext_cash = {(loc, acct, ccy): bal for (loc, acct), s in stmts.items() if (loc, acct) in covered
                    for ccy, bal in s.cash}
        cash_obs = []
        for k in sorted(set(exp_cash) | set(ext_cash)):
            if (k[0], k[1]) not in covered:
                continue
            diff = ext_cash.get(k, Decimal(0)) - exp_cash.get(k, Decimal(0))
            if abs(diff) >= Decimal("0.01"):
                loc, acct, ccy = k
                cash_obs.append(Observation(
                    B.CASH_MISMATCH, f"CASH|{loc}|{acct}|{ccy}", {f"cash:{loc}:{acct}:{ccy}"},
                    str(exp_cash.get(k)), str(ext_cash.get(k)), diff, _usd(diff, ccy), as_of=as_of, location=loc,
                    account=acct, fix={"action": "ESCALATE"}))
        n2, c2 = self.queue.reconcile_run(("CASH",), cash_obs, now, as_of, blind)
        compared = len(set(exp_pos) | set(ext_pos)) + len(set(exp_cash) | set(ext_cash))
        return compared, len(pos_obs) + len(cash_obs), n1 + n2, c1 + c2

    @staticmethod
    def _misbooked(bookings: list[Booking], loc: str, iid: str, x: str, rx: Decimal, y: str, as_of: date) -> Booking | None:
        """A settled booking whose move between x and y explains the pair of residuals."""
        cands = []
        for b in bookings:
            if b.location != loc or b.instrument_id != iid or b.settle_date is None or b.settle_date > as_of:
                continue
            q = b.side * b.quantity
            if (b.account == x and rx == -q) or (b.account == y and rx == q):
                cands.append(b)
        return max(cands, key=lambda b: (b.settle_date, b.alloc_id)) if cands else None

    # -------------------------------------------------------- confirmations
    def run_confirm(self, as_of: date, now: datetime) -> tuple[int, int, int, int]:
        obs, compared = [], 0
        for alloc_id in sorted(self.cap.bookings):
            b = self.cap.bookings[alloc_id]
            if b.trade_date > as_of or ny(b.trade_date, self.confirm_cutoff) > now:
                continue
            compared += 1
            if alloc_id in self.cap.confirms or self._under_trade_break(b.client_order_id):
                continue
            inst = INSTRUMENTS[b.instrument_id]
            obs.append(Observation(
                B.LATE_CONFIRMATION, f"CONF|{alloc_id}", {f"alloc:{alloc_id}", f"order:{b.client_order_id}"},
                f"confirmation by {self.confirm_cutoff} on {b.trade_date}", "not received", b.quantity,
                _usd(b.gross, b.currency), as_of=as_of, location=b.location, account=b.account,
                instrument_id=b.instrument_id, client_order_id=b.client_order_id,
                source_records=[{"alloc_id": alloc_id, "qty": b.quantity, "side": Side.name(b.side),
                                 "avg_price": b.avg_price / inst.price_factor}],
                fix={"action": "CHASE_CONFIRM", "alloc_id": alloc_id}))
        new, closed = self.queue.reconcile_run(("CONFIRM",), obs, now, as_of)
        return compared, len(obs), new, closed


def schedule_recon(bus: InProcessBus, trading_days: list[date], all_days: list[date]) -> None:
    for d in all_days:
        if d in trading_days:
            t = ny(d, "09:45")
            while t <= ny(d, "16:45"):
                bus.publish("recon.tick", {"kind": "TRADE"}, t)
                t += timedelta(minutes=15)
            bus.publish("recon.tick", {"kind": "TRADE"}, ny(d, "17:45"))
        else:
            bus.publish("recon.tick", {"kind": "TRADE"}, ny(d, "10:00"))
        bus.publish("recon.tick", {"kind": "EOD", "as_of": d}, ny(d, "19:30"))
        bus.publish("recon.tick", {"kind": "CONFIRM", "as_of": d}, ny(d, "21:05"))
