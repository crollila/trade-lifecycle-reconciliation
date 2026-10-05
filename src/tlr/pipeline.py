"""Wires the components onto one bus and runs a simulation end to end."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import ledger
from .bus import Event, InProcessBus
from .calendar import BusinessCalendar, ny
from .capture import Capture
from .config import SimConfig
from .exceptions import ExceptionQueue
from .ops import OpsDesk
from .recon import ReconEngine, schedule_recon
from .refdata import ACCOUNT_XREF, FX_TO_USD, INSTRUMENTS, SYMBOL_XREF
from .store import Store
from .world import World


@dataclass
class RunResult:
    cfg: SimConfig
    world: World
    capture: Capture
    queue: ExceptionQueue
    store: Store
    bus: InProcessBus
    snapshots: list[dict] = field(default_factory=list)
    cash_snapshots: list[dict] = field(default_factory=list)
    pnl: list[dict] = field(default_factory=list)


class Harness:
    """Measurement only: compares internal and external books with ground truth each evening."""

    def __init__(self, result: RunResult):
        self.r = result
        result.bus.subscribe("harness.snapshot", self.on_snapshot)
        self.nav0 = None

    def on_snapshot(self, ev: Event) -> None:
        r, d = self.r, ev.payload
        w, cap, mode = r.world, r.capture, r.cfg.label()
        internal = list(cap.bookings.values())
        expected = w.truth_bookings
        if self.nav0 is None:
            self.nav0 = ledger.nav_usd([], w.holdings, w.opening_cash, w.day0, w.marks)
        i_td = ledger.positions(internal, w.holdings, d, "TD")
        i_sd = ledger.positions(internal, w.holdings, d, "SD")
        e_td = ledger.positions(expected, w.holdings, d, "TD")
        e_sd = ledger.positions(expected, w.holdings, d, "SD")
        ext = {}
        for (loc, acct, bd), s in cap.statements.items():
            if bd == d:
                for p in s.positions:
                    ext[(loc, acct, p.instrument_id)] = p.quantity
        for k in sorted(set(i_td) | set(i_sd) | set(e_td) | set(e_sd) | set(ext)):
            row = dict(run_mode=mode, as_of=d, location=k[0], account=k[1], instrument_id=k[2],
                       internal_td=i_td.get(k, Decimal(0)), internal_sd=i_sd.get(k, Decimal(0)),
                       external_sd=ext.get(k, Decimal(0)), expected_td=e_td.get(k, Decimal(0)),
                       expected_sd=e_sd.get(k, Decimal(0)), mark=w.marks[(k[2], d)])
            r.snapshots.append(row)
            r.store.add("position_snapshots", **row)
        ic_sd = ledger.cash(internal, w.opening_cash, d, w.marks, "SD")
        ec_sd = ledger.cash(expected, w.opening_cash, d, w.marks, "SD")
        ic_td = ledger.cash(internal, w.opening_cash, d, w.marks, "TD")
        ec_td = ledger.cash(expected, w.opening_cash, d, w.marks, "TD")
        xc = {}
        for (loc, acct, bd), s in cap.statements.items():
            if bd == d:
                for ccy, bal in s.cash:
                    xc[(loc, acct, ccy)] = bal
        for k in sorted(set(ic_sd) | set(ec_sd) | set(xc)):
            row = dict(run_mode=mode, as_of=d, location=k[0], account=k[1], currency=k[2],
                       internal_sd=ic_sd.get(k, Decimal(0)), external_sd=xc.get(k), expected_sd=ec_sd.get(k, Decimal(0)),
                       internal_td=ic_td.get(k, Decimal(0)), expected_td=ec_td.get(k, Decimal(0)))
            r.cash_snapshots.append(row)
            r.store.add("cash_snapshots", **row)
        inav = ledger.nav_usd(internal, w.holdings, w.opening_cash, d, w.marks)
        enav = ledger.nav_usd(expected, w.holdings, w.opening_cash, d, w.marks)
        for acct in sorted(enav):
            row = dict(run_mode=mode, as_of=d, account=acct, internal_nav_usd=inav[acct], expected_nav_usd=enav[acct],
                       internal_pnl_usd=inav[acct] - self.nav0[acct], expected_pnl_usd=enav[acct] - self.nav0[acct])
            r.pnl.append(row)
            r.store.add("pnl_daily", **row)


def run_simulation(cfg: SimConfig, db_path: str | Path | None = None) -> RunResult:
    cal = BusinessCalendar()
    store = Store(db_path)
    bus = InProcessBus(ny(cal.prev_business_day(cfg.start), "00:00"))
    world = World(cfg, cal, bus)
    capture = Capture(bus, store, cal)
    queue = ExceptionQueue()
    result = RunResult(cfg, world, capture, queue, store, bus)
    if cfg.recon_enabled:
        ReconEngine(bus, capture, store, queue, cal, world.marks, world.holdings, world.opening_cash, cfg.confirm_cutoff)
        OpsDesk(bus, queue, cal, cfg.seed)
        schedule_recon(bus, world.trading_days, world.all_days)
    Harness(result)
    world.generate()
    _persist_reference(result)
    for d in world.trading_days:
        bus.publish("alloc.run", d, ny(d, "17:00"))
    for d in world.all_days:
        bus.publish("harness.snapshot", d, ny(d, "22:00"))
    end = ny(world.all_days[-1], "23:59")
    bus.run(until=end)
    store.set_clock(end)
    exc_rows, audit_rows = queue.to_rows()
    store.replace_table("exceptions", exc_rows)
    store.replace_table("exception_audit", audit_rows)
    return result


def _persist_reference(r: RunResult) -> None:
    s, w = r.store, r.world
    for iid, i in INSTRUMENTS.items():
        s.add("instruments", instrument_id=iid, asset_class=i.asset_class, currency=i.currency, tick=i.tick,
              price_factor=i.price_factor, price_tolerance=i.price_tolerance, description=i.description,
              dormant=i.dormant)
    for src, m in SYMBOL_XREF.items():
        for code, iid in m.items():
            s.add("instrument_xref", source=src, external_code=code, instrument_id=iid)
    for src, m in ACCOUNT_XREF.items():
        for code, acct in m.items():
            s.add("account_xref", source=src, external_code=code, account=acct)
    for (iid, d), px in sorted(w.marks.items()):
        s.add("marks", instrument_id=iid, mark_date=d, price=px)
    for h in w.holdings:
        s.add("opening_positions", location=h.location, account=h.account, instrument_id=h.instrument_id,
              quantity=h.quantity)
    for (loc, acct, ccy), bal in sorted(w.opening_cash.items()):
        s.add("opening_cash", location=loc, account=acct, currency=ccy, balance=bal)
    s.flush()


def usd_value(iid: str, qty: Decimal, mark: Decimal) -> Decimal:
    inst = INSTRUMENTS[iid]
    mult = inst.price_factor
    return abs(qty) * mark * mult * FX_TO_USD[inst.currency]

