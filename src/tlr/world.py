"""The simulated outside world: venues, counterparties and custodians.

``World`` generates a deterministic trading history (orders, acknowledgements,
partial fills, cancels, rejects), computes the *true* allocations, injects a
controlled set of incidents, and then plays every counterparty's feeds onto the
bus in each counterparty's own wire format:

* real-time execution reports (where internal-feed incidents are injected);
* drop-copy / trade-activity files (the venue's own record, incl. billed fees);
* allocation confirmations; settlement status messages;
* end-of-day custodian statements, computed *at statement time* from the true
  state plus any custodian-side incident that operations has not yet fixed.

It also reacts to operations requests (fee disputes, custody journals,
statement re-sends, position investigations) the way a counterparty would.

Nothing in the internal pipeline or the reconciliation engine may read this
module's ground truth; only the scoring harness does.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from . import ledger
from .adapters import alphax, bondhub, meridian, northbridge
from .allocation import allocate
from .bus import Event, InProcessBus
from .calendar import BusinessCalendar, ny, ny_date, settlement_date
from .config import SimConfig
from .model import (
    AssetClass,
    Booking,
    BreakType,
    Confirmation,
    ExecEvent,
    ExecType,
    ExtTrade,
    Holding,
    Incident,
    Order,
    OrderStatus,
    SettlementStatus,
    Side,
    Statement,
    StatementPosition,
    money,
)
from .refdata import (
    ACCOUNTS,
    ALPHAX,
    BLOCK_ACCOUNT,
    BONDHUB,
    INSTRUMENTS,
    LOCATIONS,
    MERIDIAN,
    NORTHBRIDGE,
    SETTLEMENT_LOCATION,
    START_PRICE,
    expected_fee,
    opening_cash,
    opening_holdings,
    tradable_on,
)

D = Decimal
VOL = {AssetClass.EQUITY: D("0.02"), AssetClass.BOND: D("0.004"), AssetClass.FUTURE: D("0.015")}
DROP_COPY_SLOTS = {
    ALPHAX: ([f"{h:02d}:{m:02d}" for h in range(9, 17) for m in (0, 15, 30, 45)
              if "09:45" <= f"{h:02d}:{m:02d}" <= "16:15"] + ["17:00"], 2),
    BONDHUB: ([f"{h:02d}:00" for h in range(10, 17)] + ["17:00"], 5),
    MERIDIAN: (["12:00", "17:00"], 30),
}
STATEMENT_TIME = {MERIDIAN: "18:30", NORTHBRIDGE: "19:00"}
WEIGHT_SETS = [((D("0.5"), D("0.3"), D("0.2")), 3), ((D("0.6"), D("0.4")), 2), ((D(1),), 1)]


@dataclass
class TruthFill:
    fill_id: str
    qty: Decimal
    price: Decimal
    ts: datetime


@dataclass
class OrderScript:
    order: Order
    venue_order_id: str
    ack_ts: datetime
    rejected: bool = False
    fills: list[TruthFill] = field(default_factory=list)
    cancel_ts: datetime | None = None

    @property
    def location(self) -> str:
        return SETTLEMENT_LOCATION[self.order.venue]


class World:
    def __init__(self, cfg: SimConfig, cal: BusinessCalendar, bus: InProcessBus):
        self.cfg, self.cal, self.bus = cfg, cal, bus
        self.rng = random.Random(cfg.seed)
        self.trading_days = cal.business_days(cfg.start, cfg.trading_days)
        self.all_days = self.trading_days + [cal.add_business_days(self.trading_days[-1], i + 1)
                                             for i in range(cfg.settle_days)]
        self.day0 = cal.prev_business_day(cfg.start)
        self.holdings: list[Holding] = opening_holdings()
        self.opening_cash = opening_cash()
        self.marks: dict[tuple[str, date], Decimal] = {}
        self.scripts: dict[str, OrderScript] = {}
        self.truth_fills: dict[tuple[str, str], ExtTrade] = {}
        self.fill_order: dict[tuple[str, str], str] = {}
        self.truth_bookings: list[Booking] = []
        self.bookings_by_order: dict[str, list[Booking]] = defaultdict(list)
        self.incidents: list[Incident] = []
        # incident state
        self.internal_feed: dict[tuple[str, str], tuple[str, dict]] = {}  # fill -> (kind, params)
        self.billed_fee: dict[tuple[str, str], Decimal] = {}
        self.refunded_at: dict[tuple[str, str], datetime] = {}
        self.billed_bookings: dict[str, list[Booking]] = {}
        self.failed: dict[str, date] = {}
        self.wrong_acct: dict[str, dict] = {}
        self.unexpected: list[dict] = []
        self.stale: set[tuple[str, str, date]] = set()
        self.late_confirm: dict[str, datetime] = {}
        self.last_coverage: dict[str, datetime] = {}
        self._seq = defaultdict(int)
        self._claims: dict[tuple, set[int]] = defaultdict(set)
        bus.subscribe("ops.request", self.on_ops_request)
        bus.subscribe("world.statement", self._on_statement_time)
        bus.subscribe("world.refund", self._on_refund)
        bus.subscribe("world.resend", self._on_resend)

    # ================================================================ generate
    def generate(self) -> None:
        self._gen_marks()
        for d in self.trading_days:
            for n in range(self.cfg.orders_per_day):
                self._gen_order(d, n)
        for s in self.scripts.values():
            if not s.rejected and s.fills:
                inst = INSTRUMENTS[s.order.instrument_id]
                fills = [(f.qty, f.price, self.truth_fills[(s.order.venue, f.fill_id)].fee) for f in s.fills]
                bks = allocate(s.order, fills, inst, s.location, self.cal)
                self.truth_bookings += bks
                self.bookings_by_order[s.order.client_order_id] = bks
        self._plan_incidents()
        self._schedule()

    def _next_id(self, prefix: str) -> str:
        self._seq[prefix] += 1
        return f"{prefix}{self._seq[prefix]:07d}"

    def _gen_marks(self) -> None:
        for iid, inst in INSTRUMENTS.items():
            px = START_PRICE[iid]
            self.marks[(iid, self.day0)] = px
            k = max(1, int(px * VOL[inst.asset_class] / inst.tick))
            for d in self.all_days:
                px = max(inst.tick * 40, px + self.rng.randint(-k, k) * inst.tick)
                self.marks[(iid, d)] = px

    def _intraday_price(self, iid: str, d: date, ts: datetime) -> Decimal:
        inst = INSTRUMENTS[iid]
        p0 = self.marks[(iid, self.cal.prev_business_day(d))]
        p1 = self.marks[(iid, d)]
        frac = D(max(0, min(390, int((ts - ny(d, "09:30")).total_seconds() // 60)))) / D(390)
        raw = p0 + (p1 - p0) * frac + self.rng.randint(-4, 4) * inst.tick
        n = (raw / inst.tick).to_integral_value()
        return max(inst.tick * 40, (n * inst.tick).quantize(inst.tick))

    def _gen_order(self, d: date, n: int) -> None:
        rng = self.rng
        r = rng.random()
        if r < 0.55:
            iid = rng.choice([i for i in tradable_on(ALPHAX)])
            inst = INSTRUMENTS[iid]
            venue = ALPHAX if inst.currency == "EUR" or rng.random() < 0.6 else MERIDIAN
            units, unit, kmax = rng.randint(5, 200), D(100), 5
        elif r < 0.80:
            iid, venue = rng.choice(tradable_on(BONDHUB)), BONDHUB
            units, unit, kmax = rng.randint(1, 20), D(250_000), 2
        else:
            iid, venue = rng.choice([i for i in tradable_on(MERIDIAN) if INSTRUMENTS[i].asset_class == AssetClass.FUTURE]), MERIDIAN
            units, unit, kmax = rng.randint(5, 150), D(1), 4
        inst = INSTRUMENTS[iid]
        weights, nacct = rng.choice(WEIGHT_SETS)
        accts = sorted(rng.sample(ACCOUNTS, nacct))
        whole_seconds = venue == BONDHUB  # BondHub's CSV carries no milliseconds
        created = ny(d, "09:30", seconds=rng.randint(300, 6 * 3600), millis=0 if whole_seconds else rng.randint(0, 999))
        side = rng.choice((Side.BUY, Side.SELL))
        order = Order(f"O{d:%m%d}-{n:04d}", d, iid, side, unit * units, START_PRICE[iid], venue,
                      tuple(zip(accts, weights)), created)
        prefix = {ALPHAX: "AXO", BONDHUB: "BHO", MERIDIAN: "MRO"}[venue]
        ack_ts = created + timedelta(milliseconds=0 if whole_seconds else rng.randint(40, 400),
                                     seconds=1 if whole_seconds else 0)
        s = OrderScript(order, self._next_id(prefix), ack_ts)
        self.scripts[order.client_order_id] = s
        roll = rng.random()
        if roll < 0.01:
            s.rejected = True
            return
        exec_units = units if roll >= 0.07 else max(1, units * rng.randint(30, 80) // 100)
        k = min(rng.randint(1, kmax), exec_units)
        cuts = sorted(rng.sample(range(1, exec_units), k - 1)) if k > 1 else []
        parts = [b - a for a, b in zip([0] + cuts, cuts + [exec_units])]
        t = ack_ts
        cap = ny(d, "15:59", seconds=rng.randint(0, 29))
        fprefix = {ALPHAX: "AXF", BONDHUB: "BHT", MERIDIAN: "MRX"}[venue]
        for p in parts:
            t = min(cap, t + timedelta(seconds=rng.randint(1, 600), milliseconds=0 if whole_seconds else rng.randint(0, 999)))
            fill = TruthFill(self._next_id(fprefix), unit * p, self._intraday_price(iid, d, t), t)
            s.fills.append(fill)
            key = (venue, fill.fill_id)
            self.fill_order[key] = order.client_order_id
            self.truth_fills[key] = ExtTrade(venue, fill.fill_id, s.venue_order_id, order.client_order_id, iid, side,
                                             fill.qty, fill.price, expected_fee(venue, inst, fill.qty, fill.price),
                                             fill.ts, inst.currency, BLOCK_ACCOUNT)
        if exec_units < units:
            s.cancel_ts = min(cap + timedelta(seconds=20), t + timedelta(seconds=rng.randint(30, 600)))

    # ============================================================== incidents
    def _free(self, keys: list[tuple], d0: int, d1: int) -> bool:
        if not self.cfg.isolate_incidents:
            return True
        days = set(range(d0, d1 + 1))
        return not any(self._claims[k] & days for k in keys)

    def _claim(self, keys: list[tuple], d0: int, d1: int) -> bool:
        """Reserve (key, day) slots so incidents never overlap in ways that blur root cause."""
        days = set(range(d0, d1 + 1))
        if not self._free(keys, d0, d1):
            return False
        for k in keys:
            self._claims[k] |= days
        return True

    def _add_incident(self, kind: str, expected: str | None, at: datetime, keys: set[str], desc: str,
                      material: bool = False, **details) -> None:
        self.incidents.append(Incident(f"INC-{len(self.incidents) + 1:04d}", kind, expected, at, keys, desc,
                                       material, details))

    def _plan_incidents(self) -> None:
        rng, cnt = self.rng, self.cfg.incidents
        used_orders: set[str] = set()
        all_fills = sorted(self.truth_fills)
        rng.shuffle(all_fills)

        def take_fill(pred=lambda key: True):
            for key in all_fills:
                oid = self.fill_order[key]
                if oid not in used_orders and pred(key):
                    used_orders.add(oid)
                    return key
            raise RuntimeError("no candidate fill left")

        if self.cfg.material_incident:
            day = self.trading_days[min(2, len(self.trading_days) - 1)]
            cands = [k for k in sorted(self.truth_fills) if k[0] == BONDHUB and ny_date(self.truth_fills[k].trade_ts) == day]
            key = max(cands, key=lambda k: (self.truth_fills[k].quantity, k))
            used_orders.add(self.fill_order[key])
            t = self.truth_fills[key]
            self.internal_feed[key] = ("QTY", {"qty": t.quantity * 10})
            self._add_incident("QUANTITY_MISMATCH", BreakType.QUANTITY_MISMATCH, t.trade_ts, {f"fill:{key[0]}:{key[1]}"},
                               f"MATERIAL: fat-finger 10x face on {t.instrument_id} ({t.quantity:,.0f} booked as "
                               f"{t.quantity * 10:,.0f})", material=True, fill=key[1], venue=key[0])

        for kind, n in cnt.items():
            for _ in range(max(0, n - (1 if kind == BreakType.QUANTITY_MISMATCH and self.cfg.material_incident else 0))):
                if kind in (BreakType.MISSING_TRADE, BreakType.DUPLICATE_FILL, BreakType.QUANTITY_MISMATCH,
                            BreakType.PRICE_MISMATCH, BreakType.FEE_MISMATCH):
                    self._plan_trade_incident(kind, take_fill())
        # position-level incidents use orders untouched by trade incidents
        clean = [b for b in self.truth_bookings if b.client_order_id not in used_orders]
        day_ix = {d: i for i, d in enumerate(self.all_days)}
        last_ok = len(self.all_days) - 3
        for _ in range(cnt.get(BreakType.FAILED_SETTLEMENT, 0)):
            self._plan_failed(clean, day_ix, last_ok, used_orders)
        for _ in range(cnt.get(BreakType.WRONG_ACCOUNT_ALLOCATION, 0)):
            self._plan_wrong_account(clean, day_ix, last_ok, used_orders)
        for _ in range(cnt.get(BreakType.UNEXPECTED_POSITION, 0)):
            self._plan_unexpected(day_ix)
        for _ in range(cnt.get(BreakType.STALE_POSITION, 0)):
            self._plan_stale(day_ix)
        for _ in range(cnt.get(BreakType.LATE_CONFIRMATION, 0)):
            self._plan_late_confirm(clean, used_orders)

    def _plan_trade_incident(self, kind: str, key: tuple[str, str]) -> None:
        rng = self.rng
        t = self.truth_fills[key]
        inst = INSTRUMENTS[t.instrument_id]
        fkey = f"fill:{key[0]}:{key[1]}"
        if kind == BreakType.MISSING_TRADE:
            self.internal_feed[key] = ("DROP", {})
            desc = "real-time execution report dropped by the session"
        elif kind == BreakType.DUPLICATE_FILL:
            dup = key[1] + "-R"
            self.internal_feed[key] = ("DUP", {"dup_id": dup, "delay": rng.randint(2, 40)})
            fkey_dup = f"fill:{key[0]}:{dup}"
            self._add_incident(kind, BreakType.DUPLICATE_FILL, t.trade_ts, {fkey, fkey_dup},
                               "venue re-sent an execution under a new exec id after a session reset",
                               fill=key[1], venue=key[0])
            return
        elif kind == BreakType.QUANTITY_MISMATCH:
            unit = {AssetClass.EQUITY: 100, AssetClass.BOND: 250_000, AssetClass.FUTURE: 1}[inst.asset_class]
            delta = D(unit * rng.randint(1, 8))
            new_q = t.quantity - delta if t.quantity > delta and rng.random() < 0.5 else t.quantity + delta
            self.internal_feed[key] = ("QTY", {"qty": new_q})
            desc = f"real-time quantity {new_q} vs venue {t.quantity}"
        elif kind == BreakType.PRICE_MISMATCH:
            ticks = rng.randint(5, 40) * rng.choice((-1, 1))
            new_p = max(inst.tick, t.price + ticks * inst.tick)
            self.internal_feed[key] = ("PX", {"price": new_p})
            desc = f"real-time price {new_p} vs venue {t.price}"
        else:  # FEE_MISMATCH: venue bills off-schedule
            billed = t.fee + max(D("0.25"), money(t.fee * D("0.6")))
            self.billed_fee[key] = billed
            desc = f"venue billed {billed} vs schedule {t.fee}"
        self._add_incident(kind, kind, t.trade_ts, {fkey}, desc, fill=key[1], venue=key[0])

    def _status_time(self, d: date, alloc_id: str) -> datetime:
        return ny(d, "16:00", seconds=random.Random(f"{alloc_id}{d}").randint(0, 600))

    def _plan_failed(self, clean, day_ix, last_ok, used_orders) -> None:
        cands = [b for b in clean if INSTRUMENTS[b.instrument_id].settles and b.settle_date in day_ix
                 and day_ix[b.settle_date] <= last_ok and b.client_order_id not in used_orders]
        self.rng.shuffle(cands)
        for b in cands:
            lag = self.rng.randint(1, 2)
            i0 = day_ix[b.settle_date]
            if (self._claim([("LI", b.location, b.instrument_id)], i0, i0 + lag + 1)
                    and self._claim([("START", b.location, b.account)], i0, i0)):
                used_orders.add(b.client_order_id)
                self.failed[b.alloc_id] = self.all_days[i0 + lag]
                self._add_incident(BreakType.FAILED_SETTLEMENT, BreakType.FAILED_SETTLEMENT,
                                   self._status_time(b.settle_date, b.alloc_id), {f"alloc:{b.alloc_id}"},
                                   f"counterparty failed to deliver (LACK); settles {lag} day(s) late",
                                   alloc=b.alloc_id)
                return

    def _plan_wrong_account(self, clean, day_ix, last_ok, used_orders) -> None:
        cands = [b for b in clean if INSTRUMENTS[b.instrument_id].settles and b.settle_date in day_ix
                 and day_ix[b.settle_date] <= last_ok and b.client_order_id not in used_orders]
        self.rng.shuffle(cands)
        for b in cands:
            other = self.rng.choice([a for a in ACCOUNTS if a != b.account])
            i0 = day_ix[b.settle_date]
            if (self._free([("START", b.location, b.account), ("START", b.location, other)], i0, i0)
                    and self._claim([("LI", b.location, b.instrument_id)], i0, i0 + 2)):
                self._claim([("START", b.location, b.account), ("START", b.location, other)], i0, i0)
                used_orders.add(b.client_order_id)
                self.wrong_acct[b.alloc_id] = {"to": other, "journal_at": None, "booking": b}
                self._add_incident(BreakType.WRONG_ACCOUNT_ALLOCATION, BreakType.WRONG_ACCOUNT_ALLOCATION,
                                   ny(b.settle_date, "16:00"),
                                   {f"alloc:{b.alloc_id}", f"pos:{b.location}:{b.account}:{b.instrument_id}",
                                    f"pos:{b.location}:{other}:{b.instrument_id}"},
                                   f"custodian settled {b.account}'s allocation into {other}", alloc=b.alloc_id)
                return

    def _plan_unexpected(self, day_ix) -> None:
        combos = [(NORTHBRIDGE, "QUIX"), (NORTHBRIDGE, "OLDC-3.00-2028"), (MERIDIAN, "QUIX")]
        for _ in range(200):
            loc, iid = self.rng.choice(combos)
            acct = self.rng.choice(ACCOUNTS)
            i0 = self.rng.randint(1, len(self.trading_days) - 1)
            if any(u["loc"] == loc and u["acct"] == acct and u["iid"] == iid for u in self.unexpected):
                continue
            if self._claim([("START", loc, acct)], i0, i0):
                qty = D(25_000) if INSTRUMENTS[iid].asset_class == AssetClass.EQUITY else D(2_000_000)
                d = self.all_days[i0]
                self.unexpected.append({"loc": loc, "acct": acct, "iid": iid, "qty": qty, "start": d, "reversed_at": None})
                self._add_incident(BreakType.UNEXPECTED_POSITION, BreakType.UNEXPECTED_POSITION,
                                   ny(d, STATEMENT_TIME[loc]), {f"pos:{loc}:{acct}:{iid}"},
                                   f"custodian booked {qty:,.0f} {iid} free-of-payment into {acct} in error",
                                   location=loc, account=acct)
                return

    def _plan_stale(self, day_ix) -> None:
        for _ in range(200):
            loc, acct = self.rng.choice(LOCATIONS), self.rng.choice(ACCOUNTS)
            i0 = self.rng.randint(1, len(self.trading_days) - 1)
            if self._claim([("START", loc, acct)], i0, i0):
                d = self.all_days[i0]
                self.stale.add((loc, acct, d))
                self._add_incident(BreakType.STALE_POSITION, BreakType.STALE_POSITION, ny(d, STATEMENT_TIME[loc]),
                                   {f"stmt:{loc}:{acct}:{d}"}, "custodian statement carried the prior day's balances",
                                   location=loc, account=acct)
                return

    def _plan_late_confirm(self, clean, used_orders) -> None:
        cands = [b for b in clean if b.client_order_id not in used_orders and b.alloc_id not in self.late_confirm]
        b = self.rng.choice(cands)
        used_orders.add(b.client_order_id)
        days_late = self.rng.choice((1, 1, 2))
        arrive = ny(self.cal.add_business_days(b.trade_date, days_late), "10:00", seconds=self.rng.randint(0, 5 * 3600))
        self.late_confirm[b.alloc_id] = arrive
        self._add_incident(BreakType.LATE_CONFIRMATION, BreakType.LATE_CONFIRMATION,
                           ny(b.trade_date, self.cfg.confirm_cutoff), {f"alloc:{b.alloc_id}"},
                           f"confirmation arrived {days_late} business day(s) after trade date", alloc=b.alloc_id)

    # =============================================================== schedule
    def _exec_event(self, s: OrderScript, exec_type: str, status: str, ts: datetime, msg_id: str,
                    fill: TruthFill | None = None, qty: Decimal | None = None, price: Decimal | None = None) -> ExecEvent:
        o = s.order
        inst = INSTRUMENTS[o.instrument_id]
        return ExecEvent(
            venue=o.venue, exec_type=exec_type, message_id=msg_id, content_hash="", client_order_id=o.client_order_id,
            venue_order_id=s.venue_order_id, account=BLOCK_ACCOUNT, instrument_id=o.instrument_id, side=o.side,
            status=status, trade_ts=ts, currency=inst.currency, venue_fill_id=fill.fill_id if fill else None,
            quantity=qty if qty is not None else (fill.qty if fill else D(0)),
            price=price if price is not None else (fill.price if fill else D(0)),
            settlement_date=settlement_date(o.trade_date, inst.asset_class, inst.currency, self.cal))

    def _render(self, e: ExecEvent) -> str:
        if e.venue == ALPHAX:
            self._seq["fixseq"] += 1
            px = None
            if self.cfg.float_noise and e.exec_type == ExecType.FILL and self.rng.random() < 0.3:
                px = repr(float(e.price) + 2e-10)  # binary-float artefact from the venue's engine
            return alphax.render_exec(e, self._seq["fixseq"], px)
        if e.venue == BONDHUB:
            return bondhub.render_exec(e)
        return meridian.render_exec(e)

    def _publish_exec(self, venue: str, raw: str, ts: datetime) -> None:
        self.bus.publish("raw.exec", (venue, raw), ts + timedelta(milliseconds=self.rng.randint(5, 300)),
                         key=venue)

    def _msg_id(self, venue: str, base: str, suffix: str) -> str:
        return {ALPHAX: f"{base}{suffix}", BONDHUB: f"{suffix[1:]}{base}", MERIDIAN: f"EV-{base}{suffix}"}[venue]

    def _schedule(self) -> None:
        bus, rng = self.bus, self.rng
        replay_pool: list[tuple[str, str, datetime, str]] = []
        for s in self.scripts.values():
            o = s.order
            bus.publish("oms.order", o, o.created_at, key=o.client_order_id)
            if s.rejected:
                e = self._exec_event(s, ExecType.REJECT, OrderStatus.REJECTED, s.ack_ts,
                                     self._msg_id(o.venue, s.venue_order_id, "-REJ"))
                self._publish_exec(o.venue, self._render(e), s.ack_ts)
                continue
            e = self._exec_event(s, ExecType.ACK, OrderStatus.ACKNOWLEDGED, s.ack_ts,
                                 self._msg_id(o.venue, s.venue_order_id, "-ACK"))
            self._publish_exec(o.venue, self._render(e), s.ack_ts)
            for i, f in enumerate(s.fills):
                last = i == len(s.fills) - 1 and s.cancel_ts is None
                status = OrderStatus.FILLED if last else OrderStatus.PARTIALLY_FILLED
                msg_id = f.fill_id if o.venue != MERIDIAN else f"EV-{f.fill_id}"
                key = (o.venue, f.fill_id)
                kind, params = self.internal_feed.get(key, (None, {}))
                if kind == "DROP":
                    continue
                e = self._exec_event(s, ExecType.FILL, status, f.ts, msg_id, f, qty=params.get("qty"),
                                     price=params.get("price"))
                raw = self._render(e)
                self._publish_exec(o.venue, raw, f.ts)
                if kind == "DUP":
                    dup_fill = TruthFill(params["dup_id"], f.qty, f.price, f.ts)
                    dup_msg = dup_fill.fill_id if o.venue != MERIDIAN else f"EV-{dup_fill.fill_id}"
                    e2 = self._exec_event(s, ExecType.FILL, status, f.ts, dup_msg, dup_fill)
                    self._publish_exec(o.venue, self._render(e2), f.ts + timedelta(seconds=params["delay"]))
                elif kind is None:
                    replay_pool.append((o.venue, raw, f.ts, f.fill_id))
            if s.cancel_ts:
                e = self._exec_event(s, ExecType.CANCEL, OrderStatus.CANCELED, s.cancel_ts,
                                     self._msg_id(o.venue, s.venue_order_id, "-CXL"))
                self._publish_exec(o.venue, self._render(e), s.cancel_ts)
        # benign noise: exact replays of already-delivered messages
        for venue, raw, ts, fid in rng.sample(replay_pool, min(self.cfg.exact_replays, len(replay_pool))):
            at = ts + timedelta(seconds=rng.randint(1, 900))
            self.bus.publish("raw.exec", (venue, raw), at, key=venue)
            self._add_incident("EXACT_REPLAY", None, at, {f"fill:{venue}:{fid}"},
                               "identical message delivered twice (must be absorbed idempotently)")
        self._schedule_drop_copies()
        self._schedule_confirms()
        self._schedule_settlements()
        for d in self.all_days:
            for loc in LOCATIONS:
                bus.publish("world.statement", (loc, d), ny(d, STATEMENT_TIME[loc]))

    def _ext_record(self, key: tuple[str, str]) -> ExtTrade:
        t = self.truth_fills[key]
        if key in self.billed_fee:
            t = replace(t, fee=self.billed_fee[key])
        return t

    def _schedule_drop_copies(self) -> None:
        by_venue_day: dict[tuple[str, date], list[tuple[str, str]]] = defaultdict(list)
        for key, t in self.truth_fills.items():
            by_venue_day[(key[0], ny_date(t.trade_ts))].append(key)
        for d in self.trading_days:
            for venue, (slots, lag) in DROP_COPY_SLOTS.items():
                prev = ny(d, "09:30")
                day_keys = sorted(by_venue_day.get((venue, d), []), key=lambda k: (self.truth_fills[k].trade_ts, k))
                for slot in slots:
                    cov = ny(d, slot)
                    recs = [(self._ext_record(k), None) for k in day_keys if prev < self.truth_fills[k].trade_ts <= cov]
                    raw = self._render_drop_copy(venue, recs, cov)
                    self.bus.publish("raw.dropcopy", (venue, raw), cov + timedelta(minutes=lag), key=venue)
                    self.bus.publish("world.coverage", (venue, cov), cov + timedelta(minutes=lag))
                    prev = cov
        self.bus.subscribe("world.coverage", lambda ev: self.last_coverage.__setitem__(ev.payload[0], ev.payload[1]))

    def _render_drop_copy(self, venue: str, recs, cov: datetime) -> str:
        if venue == ALPHAX:
            return alphax.render_drop_copy(recs, cov)
        if venue == BONDHUB:
            return bondhub.render_drop_copy(recs, cov)
        return meridian.render_drop_copy(recs, cov, float_noise=self.cfg.float_noise)

    def _schedule_confirms(self) -> None:
        for b in self.truth_bookings:
            venue = self.scripts[b.client_order_id].order.venue
            inst = INSTRUMENTS[b.instrument_id]
            if b.alloc_id in self.late_confirm:
                at = self.late_confirm[b.alloc_id]
            else:
                at = ny(b.trade_date, "17:00", seconds=self.rng.randint(600, 150 * 60))
            avg = (b.gross / (b.quantity * inst.price_factor)).quantize(D("0.000001"))
            sd = settlement_date(b.trade_date, inst.asset_class, inst.currency, self.cal)
            c = Confirmation(venue, b.alloc_id, b.account, b.instrument_id, b.side, b.quantity, avg, sd, at)
            raw = {ALPHAX: alphax.render_confirm, BONDHUB: bondhub.render_confirm, MERIDIAN: meridian.render_confirm}[venue](c)
            self.bus.publish("raw.confirm", (venue, raw), at, key=venue)

    def _schedule_settlements(self) -> None:
        last = self.all_days[-1]
        for b in self.truth_bookings:
            if not INSTRUMENTS[b.instrument_id].settles or b.settle_date > last:
                continue
            actual = self.failed.get(b.alloc_id, b.settle_date)
            d = b.settle_date
            while d <= actual and d <= last:
                at = self._status_time(d, b.alloc_id)
                st = SettlementStatus(b.location, b.alloc_id, "SETTLED" if d == actual else "FAILED",
                                      "" if d == actual else "LACK", d, at)
                raw = northbridge.render_settlement(st) if b.location == NORTHBRIDGE else meridian.render_settlement(st)
                self.bus.publish("raw.settlement", (b.location, raw), at, key=b.location)
                d = self.cal.next_business_day(d)

    # ================================================= external (custodian) state
    def external_bookings(self, t: datetime) -> list[Booking]:
        out = []
        for oid, bks in self.bookings_by_order.items():
            fee_keys = [(self.scripts[oid].order.venue, f.fill_id) for f in self.scripts[oid].fills
                        if (self.scripts[oid].order.venue, f.fill_id) in self.billed_fee]
            if fee_keys and not all(k in self.refunded_at and self.refunded_at[k] <= t for k in fee_keys):
                bks = self._billed_bookings(oid)
            for b in bks:
                if b.alloc_id in self.failed:
                    b = replace(b, settle_date=self.failed[b.alloc_id])
                w = self.wrong_acct.get(b.alloc_id)
                if w and (w["journal_at"] is None or w["journal_at"] > t):
                    b = replace(b, account=w["to"])
                out.append(b)
        return out

    def _billed_bookings(self, oid: str) -> list[Booking]:
        if oid not in self.billed_bookings:
            s = self.scripts[oid]
            fills = [(f.qty, f.price, self._ext_record((s.order.venue, f.fill_id)).fee) for f in s.fills]
            self.billed_bookings[oid] = allocate(s.order, fills, INSTRUMENTS[s.order.instrument_id], s.location, self.cal)
        return self.billed_bookings[oid]

    def external_holdings(self, t: datetime, d: date) -> list[Holding]:
        extra = [Holding(u["loc"], u["acct"], u["iid"], u["qty"]) for u in self.unexpected
                 if u["start"] <= d and (u["reversed_at"] is None or u["reversed_at"] > t)]
        return self.holdings + extra

    def build_statements(self, loc: str, d: date, t: datetime, accounts=ACCOUNTS, fresh: bool = False) -> list[Statement]:
        bks = self.external_bookings(t)
        out = []
        cache: dict[date, tuple] = {}
        for acct in accounts:
            as_of = d if fresh or (loc, acct, d) not in self.stale else self.cal.prev_business_day(d)
            if as_of not in cache:
                cache[as_of] = (ledger.positions(bks, self.external_holdings(t, as_of), as_of, "SD"),
                                ledger.cash(bks, self.opening_cash, as_of, self.marks, "SD"))
            pos, cash = cache[as_of]
            positions = tuple(StatementPosition(iid, q) for (l, a, iid), q in sorted(pos.items())
                              if l == loc and a == acct and q != 0)
            cash_rows = tuple((ccy, bal) for (l, a, ccy), bal in sorted(cash.items()) if l == loc and a == acct)
            self._seq["stmt"] += 1
            out.append(Statement(loc, acct, d, as_of, positions, cash_rows, t.replace(microsecond=0),
                                 f"{loc[:2]}{d:%Y%m%d}{acct[-1]}{self._seq['stmt']:05d}"))
        return out

    def _publish_statements(self, stmts: list[Statement]) -> None:
        for s in stmts:
            raw = northbridge.render_statement(s) if s.location == NORTHBRIDGE else meridian.render_statement(s)
            self.bus.publish("raw.statement", (s.location, raw), s.received_at, key=s.location)

    def _on_statement_time(self, ev: Event) -> None:
        loc, d = ev.payload
        self._publish_statements(self.build_statements(loc, d, ev.ts))

    # ===================================================== operations requests
    def on_ops_request(self, ev: Event) -> None:
        req, now = ev.payload, ev.ts
        kind = req["kind"]
        if kind == "FEE_DISPUTE":
            key = (req["venue"], req["venue_fill_id"])
            if key in self.billed_fee and key not in self.refunded_at:
                days = random.Random(f"refund{key}").randint(2, 4)
                at = ny(self.cal.add_business_days(ny_date(now), days), "11:00",
                        seconds=random.Random(f"r{key}").randint(0, 7200))
                self.refunded_at[key] = at  # takes effect at ``at``
                self.bus.publish("world.refund", key, at)
        elif kind == "CUSTODY_JOURNAL":
            for alloc, w in self.wrong_acct.items():
                b = w["booking"]
                if (w["journal_at"] is None and b.location == req["location"] and b.instrument_id == req["instrument_id"]
                        and w["to"] == req["from_account"] and b.account == req["to_account"]):
                    w["journal_at"] = now + timedelta(minutes=random.Random(alloc).randint(60, 240))
        elif kind == "STATEMENT_RESEND":
            at = now + timedelta(minutes=random.Random(str(req)).randint(20, 60))
            self.bus.publish("world.resend", (req["location"], req["account"], req["business_date"]), at)
        elif kind == "POSITION_INVESTIGATION":
            for u in self.unexpected:
                if (u["reversed_at"] is None and u["loc"] == req["location"] and u["acct"] == req["account"]
                        and u["iid"] == req["instrument_id"]):
                    u["reversed_at"] = ny(self.cal.next_business_day(ny_date(now)), "12:00")

    def _on_refund(self, ev: Event) -> None:
        key = ev.payload
        t = self.truth_fills[key]  # corrected record carries the schedule fee
        raw = self._render_drop_copy(key[0], [(t, f"{key[1]}-C")], self.last_coverage.get(key[0], ev.ts))
        self.bus.publish("raw.dropcopy", (key[0], raw), ev.ts, key=key[0])

    def _on_resend(self, ev: Event) -> None:
        loc, acct, d = ev.payload
        self._publish_statements(self.build_statements(loc, d, ev.ts, accounts=(acct,), fresh=True))

    # ======================================================== harness helpers
    def expected_holdings(self) -> list[Holding]:
        return self.holdings
