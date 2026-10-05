"""Our side of the lifecycle: gateway, OMS, trade capture, allocation, SSIs.

Message flow (all over the bus)::

    raw.exec ──adapter──▶ ExecEvent ──▶ idempotency gate ──▶ order state + versioned fills
    alloc.run (17:00)  ──▶ block allocation ──▶ bookings (versioned) ──▶ settlement instructions
    ops.correction     ──▶ same idempotent path (late bookings, corrections, busts)
    raw.dropcopy / raw.confirm / raw.settlement / raw.statement ──▶ external records

Idempotency: every message carries a venue-unique ``message_id``. A repeat with
the same content hash is ignored (exact replay); a repeat with a different hash
is quarantined, never applied. Fills are keyed by (venue, venue_fill_id), so a
second FILL for the same id can never double-count.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from .adapters import alphax, bondhub, meridian, northbridge
from .allocation import allocate
from .bus import Event, InProcessBus
from .calendar import BusinessCalendar, ny_date, settlement_date
from .model import (
    Booking,
    Confirmation,
    ExecEvent,
    ExecType,
    ExtTrade,
    Fill,
    Order,
    OrderStatus,
    SettlementStatus,
    Side,
    Statement,
)
from .refdata import ALPHAX, BONDHUB, INSTRUMENTS, MERIDIAN, NORTHBRIDGE, SETTLEMENT_LOCATION, expected_fee
from .store import Store

EXEC_PARSERS = {ALPHAX: alphax.parse_exec, BONDHUB: bondhub.parse_exec, MERIDIAN: meridian.parse_exec}
DROP_PARSERS = {ALPHAX: alphax.parse_drop_copy, BONDHUB: bondhub.parse_drop_copy, MERIDIAN: meridian.parse_drop_copy}
CONFIRM_PARSERS = {ALPHAX: alphax.parse_confirm, BONDHUB: bondhub.parse_confirm, MERIDIAN: meridian.parse_confirm}
SETTLE_PARSERS = {NORTHBRIDGE: northbridge.parse_settlement, MERIDIAN: meridian.parse_settlement}
STMT_PARSERS = {NORTHBRIDGE: northbridge.parse_statement, MERIDIAN: meridian.parse_statement}


class Capture:
    def __init__(self, bus: InProcessBus, store: Store, cal: BusinessCalendar):
        self.bus, self.store, self.cal = bus, store, cal
        self.orders: dict[str, Order] = {}
        self.order_status: dict[str, str] = {}
        self.venue_order_id: dict[str, str] = {}
        self.seen: dict[str, str] = {}  # venue:message_id -> content hash
        self.replays_ignored = 0
        self.conflicts = 0
        self.fill_version: dict[tuple[str, str], int] = {}
        self.fills: dict[tuple[str, str], Fill] = {}  # current, non-busted
        self.fills_by_order: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.allocated_dates: set[date] = set()
        self.bookings: dict[str, Booking] = {}  # current, by alloc_id
        self.bookings_by_order: dict[str, dict[str, Booking]] = defaultdict(dict)
        self.ever_booked: set[tuple[str, str, str]] = set()  # (location, account, instrument)
        self.ext: dict[tuple[str, str], ExtTrade] = {}
        self.ext_by_order: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._ext_seq = 0
        self._file_seq: dict[str, int] = defaultdict(int)
        self.watermark: dict[str, datetime] = {}
        self.confirms: dict[str, Confirmation] = {}
        self.settlement_history: dict[str, list[SettlementStatus]] = defaultdict(list)
        self.statements: dict[tuple[str, str, date], Statement] = {}  # latest per (loc, acct, business date)
        self.counts: dict[str, int] = defaultdict(int)
        self.trade_data_version = 0  # bumps whenever fills or venue trade records change
        bus.subscribe("oms.order", self.on_order)
        bus.subscribe("raw.exec", self.on_raw_exec)
        bus.subscribe("ops.correction", lambda ev: self.on_exec(ev.payload, ev.ts))
        bus.subscribe("alloc.run", self.on_alloc_run)
        bus.subscribe("raw.dropcopy", self.on_drop_copy)
        bus.subscribe("raw.confirm", self.on_confirm)
        bus.subscribe("raw.settlement", self.on_settlement)
        bus.subscribe("raw.statement", self.on_statement)

    # ------------------------------------------------------------------ OMS
    def on_order(self, ev: Event) -> None:
        o: Order = ev.payload
        self.orders[o.client_order_id] = o
        self.order_status[o.client_order_id] = OrderStatus.NEW
        self.counts["orders"] += 1
        self.store.add("orders", client_order_id=o.client_order_id, trade_date=o.trade_date,
                       instrument_id=o.instrument_id, side=Side.name(o.side), quantity=o.quantity, venue=o.venue,
                       location=SETTLEMENT_LOCATION[o.venue],
                       allocation_rule=",".join(f"{a}:{w}" for a, w in o.allocations), created_at=o.created_at)

    # --------------------------------------------------------------- gateway
    def on_raw_exec(self, ev: Event) -> None:
        venue, raw = ev.payload
        self.counts["messages"] += 1
        self.on_exec(EXEC_PARSERS[venue](raw), ev.ts)

    def on_exec(self, e: ExecEvent, now: datetime) -> None:
        mkey = f"{e.venue}:{e.message_id}"
        if mkey in self.seen:
            outcome = "REPLAY_IGNORED" if self.seen[mkey] == e.content_hash else "CONFLICT_QUARANTINED"
            if outcome == "REPLAY_IGNORED":
                self.replays_ignored += 1
            else:
                self.conflicts += 1
            self.store.add("ingest_log", venue=e.venue, message_id=e.message_id, content_hash=e.content_hash,
                           exec_type=e.exec_type, received_at=now, outcome=outcome)
            return
        self.seen[mkey] = e.content_hash
        self.store.add("ingest_log", venue=e.venue, message_id=e.message_id, content_hash=e.content_hash,
                       exec_type=e.exec_type, received_at=now, outcome="ACCEPTED")
        oid = e.client_order_id
        self.venue_order_id.setdefault(oid, e.venue_order_id)
        self.store.add("order_events", venue=e.venue, client_order_id=oid, venue_order_id=e.venue_order_id,
                       exec_type=e.exec_type, status=e.status, event_ts=e.trade_ts, received_at=now, source=e.source)
        if e.exec_type in (ExecType.ACK, ExecType.CANCEL, ExecType.REJECT):
            if e.source == "VENUE":
                self.order_status[oid] = e.status
            return
        key = (e.venue, e.ref_fill_id or e.venue_fill_id) if e.exec_type != ExecType.FILL else (e.venue, e.venue_fill_id)
        if e.exec_type == ExecType.FILL and key in self.fill_version:
            self.conflicts += 1  # a second, different message claiming an existing fill id
            return
        if e.exec_type in (ExecType.CORRECT, ExecType.BUST) and key not in self.fill_version:
            self.conflicts += 1
            return
        self._apply_fill(e, key, now)
        if e.source == "VENUE":
            self.order_status[oid] = e.status
        o = self.orders[oid]
        if o.trade_date in self.allocated_dates:
            self.allocate_order(oid, now)  # post-allocation change -> amend bookings and SSIs

    def _apply_fill(self, e: ExecEvent, key: tuple[str, str], now: datetime) -> None:
        version = self.fill_version.get(key, 0) + 1
        self.fill_version[key] = version
        o = self.orders[e.client_order_id]
        inst = INSTRUMENTS[o.instrument_id]
        busted = e.exec_type == ExecType.BUST
        prev = self.fills.get(key)
        qty = prev.quantity if busted and prev else e.quantity
        price = prev.price if busted and prev else e.price
        sd = e.settlement_date or settlement_date(o.trade_date, inst.asset_class, inst.currency, self.cal)
        ts = prev.trade_ts if busted and prev else e.trade_ts
        voi = prev.venue_order_id if busted and prev else e.venue_order_id
        fill = Fill(e.venue, key[1], o.client_order_id, voi, o.instrument_id, o.side, qty, price,
                    expected_fee(e.venue, inst, qty, price), ts, ny_date(ts), sd, inst.currency, e.account)
        if busted:
            self.fills.pop(key, None)
            self.fills_by_order[o.client_order_id].discard(key)
        else:
            self.fills[key] = fill
            self.fills_by_order[o.client_order_id].add(key)
        self.counts["fill_versions"] += 1
        self.trade_data_version += 1
        self.store.add("fills", venue=e.venue, venue_fill_id=key[1], version=version, exec_type=e.exec_type,
                       source=e.source, client_order_id=o.client_order_id, venue_order_id=voi,
                       account=e.account, instrument_id=o.instrument_id, side=Side.name(o.side), quantity=qty,
                       price=price, currency=inst.currency, fee=fill.fee, trade_ts=ts,
                       trade_date=fill.trade_date, settlement_date=sd, status=e.status, recorded_at=now,
                       is_busted=busted)

    def order_fills(self, oid: str) -> list[Fill]:
        return [self.fills[k] for k in sorted(self.fills_by_order.get(oid, ()))]

    # ------------------------------------------------------------ allocation
    def on_alloc_run(self, ev: Event) -> None:
        d: date = ev.payload
        self.allocated_dates.add(d)
        for oid, o in self.orders.items():
            if o.trade_date == d:
                self.allocate_order(oid, ev.ts)

    def allocate_order(self, oid: str, now: datetime) -> None:
        o = self.orders[oid]
        inst = INSTRUMENTS[o.instrument_id]
        loc = SETTLEMENT_LOCATION[o.venue]
        new = {b.alloc_id: b for b in allocate(o, [(f.quantity, f.price, f.fee) for f in self.order_fills(oid)],
                                                inst, loc, self.cal)}
        old = self.bookings_by_order.get(oid, {})
        for alloc_id in sorted(set(old) | set(new)):
            ob, nb = old.get(alloc_id), new.get(alloc_id)
            if ob and nb and replace(ob, version=1) == replace(nb, version=1):
                continue
            if nb:
                nb = replace(nb, version=(ob.version + 1) if ob else 1)
                self.bookings[alloc_id] = nb
                self.bookings_by_order[oid][alloc_id] = nb
                self.ever_booked.add((nb.location, nb.account, nb.instrument_id))
                self._record_booking(nb, "AMEND" if ob else "NEW", now)
            else:
                cancelled = replace(ob, version=ob.version + 1)
                del self.bookings[alloc_id]
                del self.bookings_by_order[oid][alloc_id]
                self._record_booking(cancelled, "CANCEL", now)

    def _record_booking(self, b: Booking, action: str, now: datetime) -> None:
        self.counts["booking_versions"] += 1
        self.store.add("allocations", alloc_id=b.alloc_id, version=b.version, action=action,
                       client_order_id=b.client_order_id, account=b.account, location=b.location,
                       instrument_id=b.instrument_id, side=Side.name(b.side), quantity=b.quantity, gross=b.gross,
                       fee=b.fee, accrued=b.accrued, avg_price=b.avg_price.quantize(Decimal("0.0000000001")),
                       currency=b.currency, trade_date=b.trade_date, settle_date=b.settle_date, booked_at=now)
        if INSTRUMENTS[b.instrument_id].settles:  # settlement instruction to the custodian
            self.store.add("settlement_instructions", ssi_id=f"{b.alloc_id}-v{b.version}", alloc_id=b.alloc_id,
                           version=b.version, action=action, location=b.location, account=b.account,
                           instrument_id=b.instrument_id, direction="RVP" if b.side == Side.BUY else "DVP",
                           quantity=b.quantity, settlement_amount=b.gross + b.accrued, currency=b.currency,
                           settle_date=b.settle_date, sent_at=now)

    # ------------------------------------------------------ external records
    def on_drop_copy(self, ev: Event) -> None:
        venue, raw = ev.payload
        coverage_end, trades = DROP_PARSERS[venue](raw)
        self._file_seq[venue] += 1
        self.trade_data_version += 1
        self.watermark[venue] = max(self.watermark.get(venue, coverage_end), coverage_end)
        self.store.add("ext_feed_files", venue=venue, file_seq=self._file_seq[venue], coverage_end=coverage_end,
                       records=len(trades), received_at=ev.ts)
        for t in trades:
            self._ext_seq += 1
            key = (venue, t.venue_fill_id)
            self.ext[key] = t
            self.ext_by_order[t.client_order_id].add(key)
            self.store.add("ext_trades", venue=venue, venue_fill_id=t.venue_fill_id, ingest_seq=self._ext_seq,
                           venue_order_id=t.venue_order_id, client_order_id=t.client_order_id,
                           instrument_id=t.instrument_id, side=Side.name(t.side), quantity=t.quantity, price=t.price,
                           currency=t.currency, fee=t.fee, trade_ts=t.trade_ts, received_at=ev.ts)

    def order_ext_fills(self, oid: str) -> list[ExtTrade]:
        return [self.ext[k] for k in sorted(self.ext_by_order.get(oid, ()))]

    def on_confirm(self, ev: Event) -> None:
        venue, raw = ev.payload
        c = CONFIRM_PARSERS[venue](raw)
        self.confirms.setdefault(c.alloc_id, c)
        self.store.add("confirmations", venue=venue, alloc_id=c.alloc_id, account=c.account,
                       instrument_id=c.instrument_id, side=Side.name(c.side), quantity=c.quantity,
                       avg_price=c.avg_price, settle_date=c.settle_date, received_at=ev.ts)

    def on_settlement(self, ev: Event) -> None:
        loc, raw = ev.payload
        s = SETTLE_PARSERS[loc](raw)
        self.settlement_history[s.alloc_id].append(s)
        self.store.add("settlement_status", location=loc, alloc_id=s.alloc_id, status=s.status, reason=s.reason,
                       status_date=s.status_date, received_at=ev.ts)

    def settlement_as_of(self, alloc_id: str, d: date) -> SettlementStatus | None:
        hist = [s for s in self.settlement_history.get(alloc_id, ()) if s.status_date <= d]
        return hist[-1] if hist else None

    def on_statement(self, ev: Event) -> None:
        loc, raw = ev.payload
        s = STMT_PARSERS[loc](raw)
        self.statements[(s.location, s.account, s.business_date)] = s
        for p in s.positions:
            self.store.add("ext_positions", location=loc, account=s.account, business_date=s.business_date,
                           as_of=s.as_of, statement_id=s.statement_id, instrument_id=p.instrument_id,
                           quantity=p.quantity, received_at=ev.ts)
        for ccy, bal in s.cash:
            self.store.add("ext_cash", location=loc, account=s.account, business_date=s.business_date, as_of=s.as_of,
                           statement_id=s.statement_id, currency=ccy, balance=bal, received_at=ev.ts)
