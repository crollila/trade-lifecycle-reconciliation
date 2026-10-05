"""Simulated operations desk: works exceptions inside business hours.

For every new exception the desk picks it up after a (deterministic, per
exception) delay, investigates, and applies the standard resolution for its
break type. Internal repairs go back through the same idempotent capture path
as venue messages; external problems become requests to the counterparty.
Closure is never declared by ops: the recon engine verifies it on a re-run.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from .bus import Event, InProcessBus
from .calendar import NEW_YORK, BusinessCalendar, ny
from .exceptions import ExceptionQueue, ReconException
from .model import BreakType as B
from .model import ExecEvent, ExecType, OrderStatus

OPEN_HOUR, CLOSE_HOUR = time(8, 0), time(18, 30)
# (pickup delay range, fix delay range) in minutes
TIMING = {
    "TRADE": ((5, 45), (10, 90)),
    "FEE": ((30, 180), (15, 60)),
    "DEFAULT": ((10, 60), (20, 120)),
}
ACTIONS = {
    B.MISSING_TRADE: "Booked missing fill from venue drop copy (late booking)",
    B.DUPLICATE_FILL: "Busted duplicate fill; venue record kept",
    B.QUANTITY_MISMATCH: "Corrected internal fill quantity to venue-confirmed quantity",
    B.PRICE_MISMATCH: "Corrected internal fill price to venue-confirmed price",
    B.FEE_MISMATCH: "Disputed off-schedule fee with venue; awaiting fee credit",
    B.WRONG_ACCOUNT_ALLOCATION: "Instructed custodian journal to the correct account",
    B.STALE_POSITION: "Requested statement re-send from custodian",
    B.UNEXPECTED_POSITION: "Raised investigation with custodian; requested reversal of misbooking",
    B.FAILED_SETTLEMENT: "Chased counterparty on fail; monitoring for settlement",
    B.LATE_CONFIRMATION: "Chased counterparty for outstanding confirmation",
    B.POSITION_MISMATCH: "Escalated: unexplained position difference",
    B.CASH_MISMATCH: "Escalated: unexplained cash difference",
}


class OpsDesk:
    def __init__(self, bus: InProcessBus, queue: ExceptionQueue, cal: BusinessCalendar, seed: int):
        self.bus, self.queue, self.cal, self.seed = bus, queue, cal, seed
        self.by_id: dict[str, ReconException] = {}
        queue.on_created.append(self.on_created)
        bus.subscribe("ops.pickup", self.on_pickup)
        bus.subscribe("ops.fix", self.on_fix)

    def business_time(self, t: datetime, rng: random.Random) -> datetime:
        local = t.astimezone(NEW_YORK)
        d = local.date()
        if self.cal.is_business_day(d) and OPEN_HOUR <= local.time() < CLOSE_HOUR:
            return t
        if not (self.cal.is_business_day(d) and local.time() < OPEN_HOUR):
            d = self.cal.next_business_day(d)
        return ny(d, "08:00", seconds=rng.randint(0, 45 * 60))

    def _rng(self, exc: ReconException, stage: str) -> random.Random:
        return random.Random(f"{self.seed}|{exc.obs.key}|{exc.exc_id}|{stage}")

    def on_created(self, exc: ReconException) -> None:
        self.by_id[exc.exc_id] = exc
        rng = self._rng(exc, "pickup")
        lo, hi = TIMING.get(exc.family, TIMING["DEFAULT"])[0]
        at = self.business_time(exc.opened_at + timedelta(minutes=rng.randint(lo, hi)), rng)
        self.bus.publish("ops.pickup", exc.exc_id, at)

    def on_pickup(self, ev: Event) -> None:
        exc = self.by_id[ev.payload]
        if not exc.is_open:
            return
        exc.log(ev.ts, f"OPS:{exc.owner}", "PICKED_UP", "INVESTIGATING", "assigned analyst began investigation")
        rng = self._rng(exc, "fix")
        lo, hi = TIMING.get(exc.family, TIMING["DEFAULT"])[1]
        self.bus.publish("ops.fix", exc.exc_id, self.business_time(ev.ts + timedelta(minutes=rng.randint(lo, hi)), rng))

    def on_fix(self, ev: Event) -> None:
        exc = self.by_id[ev.payload]
        if not exc.is_open:
            return
        now, fix, actor = ev.ts, exc.obs.fix, f"OPS:{exc.owner}"
        action = fix.get("action")
        exc.resolution_action = ACTIONS[exc.obs.break_type]
        if action in ("LATE_BOOK", "CORRECT", "BUST"):
            self.bus.publish("ops.correction", self._repair_event(exc, action), now)
            exc.log(now, actor, "FIX_APPLIED", "FIX_APPLIED", exc.resolution_action)
            self.bus.publish("recon.tick", {"kind": "TRADE", "rerun": True}, now + timedelta(minutes=5))
        elif action == "DISPUTE":
            self._request(now, kind="FEE_DISPUTE", venue=fix["venue"], venue_fill_id=fix["fill"])
            exc.log(now, actor, "REQUESTED_EXTERNAL_FIX", "PENDING_EXTERNAL", exc.resolution_action)
        elif action == "JOURNAL":
            self._request(now, kind="CUSTODY_JOURNAL", **{k: fix[k] for k in
                          ("location", "instrument_id", "from_account", "to_account", "quantity")})
            exc.log(now, actor, "REQUESTED_EXTERNAL_FIX", "PENDING_EXTERNAL", exc.resolution_action)
        elif action == "RESEND":
            self._request(now, kind="STATEMENT_RESEND", location=fix["location"], account=fix["account"],
                          business_date=fix["business_date"])
            exc.log(now, actor, "REQUESTED_EXTERNAL_FIX", "PENDING_EXTERNAL", exc.resolution_action)
            self.bus.publish("recon.tick", {"kind": "EOD", "as_of": fix["business_date"], "rerun": True},
                             now + timedelta(minutes=75))
        elif action == "INVESTIGATE":
            self._request(now, kind="POSITION_INVESTIGATION", location=fix["location"], account=fix["account"],
                          instrument_id=fix["instrument_id"], quantity=fix["quantity"])
            exc.log(now, actor, "REQUESTED_EXTERNAL_FIX", "PENDING_EXTERNAL", exc.resolution_action)
        elif action in ("CHASE_FAIL", "CHASE_CONFIRM"):
            exc.log(now, actor, "CHASED_COUNTERPARTY", "INVESTIGATING", exc.resolution_action)
        else:  # ESCALATE / ESCALATE_VENUE: nothing the desk can fix unilaterally
            exc.log(now, actor, "ESCALATED", "ESCALATED", exc.resolution_action)

    def _request(self, now: datetime, **req) -> None:
        self.bus.publish("ops.request", req, now)

    @staticmethod
    def _repair_event(exc: ReconException, action: str) -> ExecEvent:
        if action == "BUST":
            fid = exc.obs.fix["fill"]
            return ExecEvent(exc.obs.venue, ExecType.BUST, f"OPS-BUST-{fid}", "ops", exc.obs.client_order_id, "",
                             "BLOCK", exc.obs.instrument_id, 0, OrderStatus.PARTIALLY_FILLED,
                             exc.opened_at, "", ref_fill_id=fid, source="OPS")
        ext = exc.obs.fix["ext"]
        is_new = action == "LATE_BOOK"
        return ExecEvent(ext.venue, ExecType.FILL if is_new else ExecType.CORRECT,
                         f"OPS-{'LATE' if is_new else 'CORR'}-{ext.venue_fill_id}-{exc.exc_id}", "ops",
                         ext.client_order_id, ext.venue_order_id, ext.account, ext.instrument_id, ext.side,
                         OrderStatus.PARTIALLY_FILLED, ext.trade_ts, ext.currency,
                         venue_fill_id=ext.venue_fill_id, ref_fill_id=None if is_new else ext.venue_fill_id,
                         quantity=ext.quantity, price=ext.price, source="OPS")
