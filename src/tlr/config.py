from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_INCIDENTS = {
    "MISSING_TRADE": 12,
    "DUPLICATE_FILL": 10,
    "QUANTITY_MISMATCH": 10,  # includes the one material 10x bond fat-finger
    "PRICE_MISMATCH": 10,
    "FEE_MISMATCH": 12,
    "WRONG_ACCOUNT_ALLOCATION": 6,
    "STALE_POSITION": 5,
    "FAILED_SETTLEMENT": 8,
    "LATE_CONFIRMATION": 10,
    "UNEXPECTED_POSITION": 5,
}


@dataclass
class SimConfig:
    seed: int = 20260302
    start: date = date(2026, 3, 2)  # window spans the US DST change on 2026-03-08
    trading_days: int = 10
    settle_days: int = 2  # business days after the last trading day (settlement / ops only)
    orders_per_day: int = 320
    incidents: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_INCIDENTS))
    material_incident: bool = True
    exact_replays: int = 150  # benign: identical venue messages delivered twice
    float_noise: bool = True  # benign: binary-float price artefacts on some feeds
    recon_enabled: bool = True
    confirm_cutoff: str = "21:00"  # local NY time on trade date
    # When True, incidents are placed so no two root causes overlap on the same
    # location/instrument or account/day. The stress scenario turns this off.
    isolate_incidents: bool = True

    @classmethod
    def stress(cls, **kw) -> "SimConfig":
        """Three times the incidents, allowed to overlap: probes the classifier's limits."""
        return cls(incidents={k: v * 3 for k, v in DEFAULT_INCIDENTS.items()}, isolate_incidents=False, **kw)

    def label(self) -> str:
        return "recon_on" if self.recon_enabled else "recon_off"
