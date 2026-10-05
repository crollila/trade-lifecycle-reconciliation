"""Exception queue: break observations in, managed exceptions with audit trail out.

Each recon run reports the breaks it *observes*. The queue de-duplicates them by
``break_key`` so an unresolved break seen on every 15-minute run stays a single
exception (first-seen time = detection time). A break that a later run of the
same family no longer observes is closed: ``RESOLVED`` if operations had
applied a fix, ``AUTO_CLEARED`` if it went away on its own (e.g. a late
confirmation finally arrived, a failing trade settled).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from .model import BreakType as B

FAMILY = {
    B.MISSING_TRADE: "TRADE", B.DUPLICATE_FILL: "TRADE", B.QUANTITY_MISMATCH: "TRADE", B.PRICE_MISMATCH: "TRADE",
    B.FEE_MISMATCH: "FEE", B.LATE_CONFIRMATION: "CONFIRM", B.FAILED_SETTLEMENT: "SETTLEMENT",
    B.STALE_POSITION: "POSITION", B.WRONG_ACCOUNT_ALLOCATION: "POSITION", B.UNEXPECTED_POSITION: "POSITION",
    B.POSITION_MISMATCH: "POSITION", B.CASH_MISMATCH: "CASH",
}
OWNER = {
    "TRADE": "Trade Support", "FEE": "Middle Office - Fees", "CONFIRM": "Settlements",
    "SETTLEMENT": "Settlements", "POSITION": "Position Control", "CASH": "Treasury Ops",
}
SEVERITY_FLOOR = {B.FAILED_SETTLEMENT: "MEDIUM", B.WRONG_ACCOUNT_ALLOCATION: "MEDIUM", B.UNEXPECTED_POSITION: "MEDIUM",
                  B.STALE_POSITION: "MEDIUM", B.POSITION_MISMATCH: "HIGH", B.CASH_MISMATCH: "HIGH"}
SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
OPEN_STATES = ("OPEN", "INVESTIGATING", "PENDING_EXTERNAL", "FIX_APPLIED", "ESCALATED")


def severity(break_type: str, impact_usd: Decimal) -> str:
    s = "CRITICAL" if impact_usd >= 1_000_000 else "HIGH" if impact_usd >= 100_000 else \
        "MEDIUM" if impact_usd >= 5_000 else "LOW"
    floor = SEVERITY_FLOOR.get(break_type, "LOW")
    return max(s, floor, key=SEVERITIES.index)


@dataclass
class Observation:
    break_type: str
    key: str
    related_keys: set[str]
    expected: str
    actual: str
    difference: Decimal
    impact_usd: Decimal
    as_of: date | None = None
    venue: str | None = None
    location: str | None = None
    account: str | None = None
    instrument_id: str | None = None
    client_order_id: str | None = None
    source_records: list[dict] = field(default_factory=list)
    fix: dict = field(default_factory=dict)  # data operations needs to act


@dataclass
class ReconException:
    exc_id: str
    obs: Observation
    family: str
    severity: str
    owner: str
    status: str
    opened_at: datetime
    last_seen_at: datetime
    last_observed_asof: date | None
    closed_at: datetime | None = None
    resolution_action: str | None = None
    audit: list[dict] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def log(self, ts: datetime, actor: str, action: str, to_status: str | None = None, note: str = "") -> None:
        frm = self.status
        if to_status:
            self.status = to_status
        self.audit.append({"event_ts": ts, "actor": actor, "action": action, "from_status": frm,
                           "to_status": self.status, "note": note})


class ExceptionQueue:
    def __init__(self) -> None:
        self.all: list[ReconException] = []
        self.open_by_key: dict[str, ReconException] = {}
        self.open_by_related: dict[str, set[str]] = defaultdict(set)
        self.on_created: list[Callable[[ReconException], None]] = []

    def _index(self, exc: ReconException, add: bool) -> None:
        for k in exc.obs.related_keys:
            (self.open_by_related[k].add if add else self.open_by_related[k].discard)(exc.obs.key)

    def open_for(self, related_key: str, families: tuple[str, ...]) -> list[ReconException]:
        return [self.open_by_key[k] for k in sorted(self.open_by_related.get(related_key, ()))
                if self.open_by_key[k].family in families]

    def open_in(self, families: tuple[str, ...]) -> list[ReconException]:
        return [e for e in self.open_by_key.values() if e.family in families]

    def reconcile_run(self, families: tuple[str, ...], observations: list[Observation], now: datetime,
                      as_of: date | None = None, blind: set[tuple[str, str]] | None = None) -> tuple[int, int]:
        """Upsert observed breaks; close unobserved ones in scope. Returns (new, closed).

        ``blind`` lists (location, account) pairs this run could not see (e.g. a stale
        statement); their open breaks are neither confirmed nor cleared."""
        new = closed = 0
        seen = set()
        for obs in observations:
            seen.add(obs.key)
            exc = self.open_by_key.get(obs.key)
            if exc:
                exc.last_seen_at = now
                if as_of and (exc.last_observed_asof is None or as_of > exc.last_observed_asof):
                    exc.last_observed_asof = as_of
                if obs.difference != exc.obs.difference:
                    exc.log(now, "RECON_ENGINE", "RE-OBSERVED", note=f"difference {exc.obs.difference} -> {obs.difference}")
                    obs.fix = {**exc.obs.fix, **obs.fix}
                    self._index(exc, False)
                    exc.obs = obs
                    self._index(exc, True)
                continue
            fam = FAMILY[obs.break_type]
            exc = ReconException(f"EXC-{len(self.all) + 1:05d}", obs, fam, severity(obs.break_type, obs.impact_usd),
                                 OWNER[fam], "OPEN", now, now, as_of)
            exc.log(now, "RECON_ENGINE", "CREATED", "OPEN", f"{obs.break_type}: expected {obs.expected}; actual {obs.actual}")
            self.all.append(exc)
            self.open_by_key[obs.key] = exc
            self._index(exc, True)
            new += 1
            for cb in self.on_created:
                cb(exc)
        for key, exc in list(self.open_by_key.items()):
            if exc.family not in families or key in seen:
                continue
            if blind and any(k.startswith(("pos:", "cash:")) and tuple(k.split(":")[1:3]) in blind
                             for k in exc.obs.related_keys):
                continue
            if as_of is not None and exc.last_observed_asof is not None and exc.last_observed_asof > as_of:
                continue  # a re-run for an earlier date cannot clear a later observation
            self.close(exc, now)
            closed += 1
        return new, closed

    def close(self, exc: ReconException, now: datetime) -> None:
        fixed = exc.status in ("FIX_APPLIED", "PENDING_EXTERNAL")
        final = "RESOLVED" if fixed else "AUTO_CLEARED"
        if not exc.resolution_action:
            exc.resolution_action = "Cleared without intervention: records now agree"
        exc.log(now, "RECON_ENGINE", "VERIFIED_CLEAR" if fixed else "AUTO_CLEARED", final,
                "re-run of the reconciliation no longer observes the break")
        exc.closed_at = now
        del self.open_by_key[exc.obs.key]
        self._index(exc, False)

    # ----------------------------------------------------------- persistence
    def to_rows(self) -> tuple[list[dict], list[dict]]:
        exc_rows, audit_rows = [], []
        for e in self.all:
            o = e.obs
            exc_rows.append({
                "exc_id": e.exc_id, "break_type": o.break_type, "family": e.family, "break_key": o.key,
                "severity": e.severity, "owner": e.owner, "status": e.status, "as_of": o.as_of, "venue": o.venue,
                "location": o.location, "account": o.account, "instrument_id": o.instrument_id,
                "client_order_id": o.client_order_id, "expected": o.expected, "actual": o.actual,
                "difference": o.difference, "impact_usd": o.impact_usd,
                "source_records": json.dumps(o.source_records, default=str),
                "related_keys": json.dumps(sorted(o.related_keys)), "resolution_action": e.resolution_action,
                "opened_at": e.opened_at, "last_seen_at": e.last_seen_at, "closed_at": e.closed_at})
            for i, a in enumerate(e.audit, 1):
                audit_rows.append({"exc_id": e.exc_id, "seq": i, **a})
        return exc_rows, audit_rows

    def status_counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.all:
            out[e.status] += 1
        return dict(out)
