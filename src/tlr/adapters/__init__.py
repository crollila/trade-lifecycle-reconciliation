"""Venue and custodian adapters.

Each module owns one counterparty's wire format, deliberately different:

=============  ==========================================================
ALPHAX         FIX 4.4 tag=value execution reports; UTC ``YYYYMMDD-HH:MM:SS.sss``
BONDHUB        CSV; *dealer*-perspective side; face in thousands; New York local time
MERIDIAN       JSON; signed quantity; epoch-millisecond timestamps; nested fees
NORTHBRIDGE    SWIFT-MT-inspired tag blocks (535/548/950 style); comma decimals
=============  ==========================================================

``render_*`` functions are the *counterparty side* (used only by the
simulator to produce raw messages). ``parse_*`` functions are our adapter and
return normalized model objects. Nothing downstream of ``parse_*`` sees a
venue-specific field name, code, identifier, unit, side convention or time zone.
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_EVEN, Decimal


def content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def to_tick(x: Decimal, tick: Decimal) -> Decimal:
    """Snap a parsed price onto the instrument's tick grid (removes float artifacts)."""
    return ((x / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick).quantize(tick)


def dec(s: str | float | int) -> Decimal:
    return Decimal(str(s))
