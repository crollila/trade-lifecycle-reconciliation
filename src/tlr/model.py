"""Normalized domain model shared by every component.

Money, quantities and prices are ``Decimal`` end to end. Timestamps are
timezone-aware UTC ``datetime`` objects. Venue-specific representations never
leave the adapters: everything downstream sees only these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(x: Decimal) -> Decimal:
    """Round a monetary amount to cents, half-up (the convention used on confirms)."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


class AssetClass:
    EQUITY = "EQUITY"
    BOND = "BOND"
    FUTURE = "FUTURE"


class Side:
    BUY = 1
    SELL = -1

    @staticmethod
    def name(side: int) -> str:
        return "BUY" if side == Side.BUY else "SELL"


class ExecType:
    """Normalized execution-event types (a superset of what each venue speaks)."""

    ACK = "ACK"
    FILL = "FILL"
    CORRECT = "CORRECT"  # amend an existing fill (qty/price/fee)
    BUST = "BUST"  # cancel an existing fill
    CANCEL = "CANCEL"  # cancel the order's remaining quantity
    REJECT = "REJECT"


class OrderStatus:
    NEW = "NEW"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class BreakType:
    MISSING_TRADE = "MISSING_TRADE"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    FEE_MISMATCH = "FEE_MISMATCH"
    WRONG_ACCOUNT_ALLOCATION = "WRONG_ACCOUNT_ALLOCATION"
    STALE_POSITION = "STALE_POSITION"
    FAILED_SETTLEMENT = "FAILED_SETTLEMENT"
    LATE_CONFIRMATION = "LATE_CONFIRMATION"
    UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
    # Residual types: differences no rule could explain. In a healthy run these
    # stay at zero; any occurrence is either a real unknown or a false positive.
    POSITION_MISMATCH = "POSITION_MISMATCH"
    CASH_MISMATCH = "CASH_MISMATCH"

    INJECTABLE = (
        MISSING_TRADE,
        DUPLICATE_FILL,
        QUANTITY_MISMATCH,
        PRICE_MISMATCH,
        FEE_MISMATCH,
        WRONG_ACCOUNT_ALLOCATION,
        STALE_POSITION,
        FAILED_SETTLEMENT,
        LATE_CONFIRMATION,
        UNEXPECTED_POSITION,
    )
    ALL = INJECTABLE + (POSITION_MISMATCH, CASH_MISMATCH)


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    asset_class: str
    currency: str
    tick: Decimal
    lot: Decimal  # allocation granularity
    price_factor: Decimal  # notional = qty * price * price_factor
    description: str
    coupon: Decimal = ZERO  # bonds: annual coupon rate, e.g. 0.0425
    maturity: date | None = None
    coupon_freq: int = 0
    day_count: str = ""  # "30/360" or "ACT/ACT"
    dormant: bool = False  # in the security master but never traded by the strategy

    @property
    def settles(self) -> bool:
        """Futures have no delivery-versus-payment settlement; they margin daily."""
        return self.asset_class != AssetClass.FUTURE

    @property
    def price_tolerance(self) -> Decimal:
        """Reconciliation price tolerance: half a tick."""
        return self.tick / 2


@dataclass(frozen=True)
class Order:
    client_order_id: str
    trade_date: date
    instrument_id: str
    side: int
    quantity: Decimal
    limit_price: Decimal
    venue: str
    allocations: tuple[tuple[str, Decimal], ...]  # (internal account, weight)
    created_at: datetime


@dataclass(frozen=True)
class ExecEvent:
    """A venue execution report after normalization.

    ``message_id`` is the venue's own unique identifier for the *message*;
    ``content_hash`` fingerprints the raw payload. Together they make capture
    idempotent: the same message delivered twice is recognized and ignored.
    """

    venue: str
    exec_type: str
    message_id: str
    content_hash: str
    client_order_id: str
    venue_order_id: str
    account: str  # normalized internal account (block account for executions)
    instrument_id: str
    side: int
    status: str
    trade_ts: datetime
    currency: str
    venue_fill_id: str | None = None
    ref_fill_id: str | None = None  # for CORRECT/BUST
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    fee: Decimal | None = None  # fee reported on the message, if the venue sends one
    settlement_date: date | None = None
    source: str = "VENUE"  # VENUE | OPS (manual repair booked by operations)


@dataclass(frozen=True)
class Fill:
    venue: str
    venue_fill_id: str
    client_order_id: str
    venue_order_id: str
    instrument_id: str
    side: int
    quantity: Decimal
    price: Decimal
    fee: Decimal  # expected fee per our fee schedule
    trade_ts: datetime
    trade_date: date
    settlement_date: date
    currency: str
    account: str


@dataclass(frozen=True)
class ExtTrade:
    """A trade as the venue reports it on its drop copy / trade file."""

    venue: str
    venue_fill_id: str
    venue_order_id: str
    client_order_id: str
    instrument_id: str
    side: int
    quantity: Decimal
    price: Decimal
    fee: Decimal  # fee the venue actually billed
    trade_ts: datetime
    currency: str
    account: str


@dataclass(frozen=True)
class Booking:
    """One account's share of a block order: the unit that settles and is held.

    ``gross`` is |qty * price * price_factor| in cents; ``accrued`` is the
    bond accrued interest paid/received at settlement; ``fee`` is this
    account's share of the block's fees. ``settle_date`` is None while a
    settlement is known to be outstanding (used by expected-state models).
    """

    alloc_id: str
    client_order_id: str
    account: str
    location: str
    instrument_id: str
    side: int
    quantity: Decimal
    gross: Decimal
    fee: Decimal
    accrued: Decimal
    trade_date: date
    settle_date: date | None
    currency: str
    version: int = 1

    @property
    def avg_price(self) -> Decimal:
        return self.gross / self.quantity if self.quantity else ZERO


@dataclass(frozen=True)
class Confirmation:
    venue: str
    alloc_id: str
    account: str
    instrument_id: str
    side: int
    quantity: Decimal
    avg_price: Decimal
    settle_date: date
    received_at: datetime


@dataclass(frozen=True)
class SettlementStatus:
    location: str
    alloc_id: str
    status: str  # SETTLED | FAILED
    reason: str
    status_date: date
    received_at: datetime


@dataclass(frozen=True)
class StatementPosition:
    instrument_id: str
    quantity: Decimal


@dataclass(frozen=True)
class Statement:
    """An end-of-day custodian / prime-broker statement for one account."""

    location: str
    account: str
    business_date: date
    as_of: date
    positions: tuple[StatementPosition, ...]
    cash: tuple[tuple[str, Decimal], ...]  # (currency, settled balance)
    received_at: datetime
    statement_id: str = ""


@dataclass
class Holding:
    """Opening holding (or an externally injected holding) at a location."""

    location: str
    account: str
    instrument_id: str
    quantity: Decimal


@dataclass
class Incident:
    """Ground-truth record of a deliberately injected problem (scoring only)."""

    incident_id: str
    kind: str
    expected_break: str | None  # None for benign noise that must NOT break
    observable_at: datetime
    match_keys: set[str] = field(default_factory=set)
    description: str = ""
    material: bool = False
    details: dict = field(default_factory=dict)
