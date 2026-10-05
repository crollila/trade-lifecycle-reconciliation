"""Synthetic reference data: security master, accounts, venues, cross-references,
fee schedules and opening balances.

Every identifier here is SYNTHETIC. Tickers, ISIN-like codes (``ZZ`` prefix,
a user-assigned range no country uses) and account numbers are invented for
the simulation and do not refer to any real security, firm or account.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .model import AssetClass, Holding, Instrument, money

D = Decimal

# ------------------------------------------------------------------ venues
ALPHAX = "ALPHAX"  # equity execution broker, FIX 4.4 tag=value
BONDHUB = "BONDHUB"  # bond RFQ platform, CSV with dealer-perspective sides
MERIDIAN = "MERIDIAN"  # prime broker + futures clearing (FCM), JSON
NORTHBRIDGE = "NORTHBRIDGE"  # global custodian, SWIFT-MT-inspired text

VENUES = (ALPHAX, BONDHUB, MERIDIAN)

# where each venue's trades settle and are held
SETTLEMENT_LOCATION = {ALPHAX: NORTHBRIDGE, BONDHUB: NORTHBRIDGE, MERIDIAN: MERIDIAN}
LOCATIONS = (NORTHBRIDGE, MERIDIAN)

ACCOUNTS = ("FUND-A", "FUND-B", "FUND-C")

# Venue / custodian account codes -> internal accounts.
BLOCK_ACCOUNT = "BLOCK"  # executions happen in a block account and are allocated later
ACCOUNT_XREF: dict[str, dict[str, str]] = {
    ALPHAX: {"AX-7741-BLK": BLOCK_ACCOUNT},
    BONDHUB: {"BH-CLIENT-0098": BLOCK_ACCOUNT},
    MERIDIAN: {"MRD-ACC-500": BLOCK_ACCOUNT, "MRD-ACC-501": "FUND-A", "MRD-ACC-502": "FUND-B", "MRD-ACC-503": "FUND-C"},
    NORTHBRIDGE: {"NB-102938": "FUND-A", "NB-102939": "FUND-B", "NB-102940": "FUND-C"},
}
ACCOUNT_CODE = {venue: {v: k for k, v in m.items()} for venue, m in ACCOUNT_XREF.items()}

EURUSD = D("1.08")  # fixed synthetic FX rate used only to report P&L in USD
FX_TO_USD = {"USD": D(1), "EUR": EURUSD}


def _eq(sym: str, ccy: str, desc: str, dormant: bool = False) -> Instrument:
    return Instrument(sym, AssetClass.EQUITY, ccy, D("0.01"), D(1), D(1), desc, dormant=dormant)


def _bond(iid: str, ccy: str, cpn: str, mat: date, freq: int, dc: str, desc: str, dormant: bool = False) -> Instrument:
    return Instrument(iid, AssetClass.BOND, ccy, D("0.0005"), D(1000), D("0.01"), desc,
                      coupon=D(cpn), maturity=mat, coupon_freq=freq, day_count=dc, dormant=dormant)


def _fut(iid: str, tick: str, mult: str, desc: str) -> Instrument:
    return Instrument(iid, AssetClass.FUTURE, "USD", D(tick), D(1), D(mult), desc)


INSTRUMENTS: dict[str, Instrument] = {i.instrument_id: i for i in [
    _eq("ACME", "USD", "Acme Industrials (synthetic)"),
    _eq("BOLT", "USD", "Bolt Logistics (synthetic)"),
    _eq("CRUX", "USD", "Crux Materials (synthetic)"),
    _eq("DYNA", "USD", "Dyna Semiconductors (synthetic)"),
    _eq("EMBR", "USD", "Ember Energy (synthetic)"),
    _eq("FLUX", "USD", "Flux Biotech (synthetic)"),
    _eq("NORD", "EUR", "Nord Maschinen (synthetic, EUR)"),
    _eq("VELA", "EUR", "Vela Retail (synthetic, EUR)"),
    _eq("QUIX", "USD", "Quix Holdings (synthetic, never traded)", dormant=True),
    _bond("SYNT-4.25-2031", "USD", "0.0425", date(2031, 5, 15), 2, "ACT/ACT", "Synthetic govt 4.25% 2031"),
    _bond("ACME-5.10-2030", "USD", "0.0510", date(2030, 6, 15), 2, "30/360", "Acme 5.10% 2030 (synthetic corp)"),
    _bond("SYNB-2.50-2034", "EUR", "0.0250", date(2034, 2, 15), 1, "ACT/ACT", "Synthetic EUR govt 2.50% 2034"),
    _bond("OLDC-3.00-2028", "USD", "0.0300", date(2028, 7, 1), 2, "30/360", "OldCo 3.00% 2028 (never traded)", dormant=True),
    _fut("SYX-H26", "0.25", "50", "Synthetic equity-index future, Mar-26, x50"),
    _fut("SYN10Y-H26", "0.015625", "1000", "Synthetic 10y note future, Mar-26, x1000"),
]}

# starting marks (prior close), SYNTHETIC
START_PRICE: dict[str, Decimal] = {
    "ACME": D("42.00"), "BOLT": D("118.50"), "CRUX": D("23.10"), "DYNA": D("305.20"),
    "EMBR": D("67.80"), "FLUX": D("12.45"), "NORD": D("54.30"), "VELA": D("88.90"), "QUIX": D("9.75"),
    "SYNT-4.25-2031": D("99.4250"), "ACME-5.10-2030": D("101.8000"), "SYNB-2.50-2034": D("96.1500"),
    "OLDC-3.00-2028": D("97.5000"), "SYX-H26": D("5012.25"), "SYN10Y-H26": D("112.515625"),
}

# Venue-specific identifiers -> internal instrument ids (the "security master xref").
SYMBOL_XREF: dict[str, dict[str, str]] = {
    ALPHAX: {"ACME.N": "ACME", "BOLT.N": "BOLT", "CRUX.N": "CRUX", "DYNA.OQ": "DYNA", "EMBR.N": "EMBR",
             "FLUX.OQ": "FLUX", "NORD.EU": "NORD", "VELA.EU": "VELA", "QUIX.N": "QUIX"},
    BONDHUB: {"ZZ00SYNT2031": "SYNT-4.25-2031", "ZZ00ACME2030": "ACME-5.10-2030",
              "ZZ00SYNB2034": "SYNB-2.50-2034", "ZZ00OLDC2028": "OLDC-3.00-2028"},
    MERIDIAN: {"MRD-100101": "ACME", "MRD-100102": "BOLT", "MRD-100103": "CRUX", "MRD-100104": "DYNA",
               "MRD-100105": "EMBR", "MRD-100106": "FLUX", "MRD-100109": "QUIX",
               "MRD-700001": "SYX-H26", "MRD-700002": "SYN10Y-H26"},
    # the custodian keys bonds by ISIN-like code and equities by ticker
    NORTHBRIDGE: {"ZZ00SYNT2031": "SYNT-4.25-2031", "ZZ00ACME2030": "ACME-5.10-2030",
                  "ZZ00SYNB2034": "SYNB-2.50-2034", "ZZ00OLDC2028": "OLDC-3.00-2028",
                  "ACME": "ACME", "BOLT": "BOLT", "CRUX": "CRUX", "DYNA": "DYNA", "EMBR": "EMBR",
                  "FLUX": "FLUX", "NORD": "NORD", "VELA": "VELA", "QUIX": "QUIX"},
}
SYMBOL_CODE = {venue: {v: k for k, v in m.items()} for venue, m in SYMBOL_XREF.items()}


def tradable_on(venue: str) -> list[str]:
    return sorted(iid for iid in SYMBOL_XREF[venue].values() if not INSTRUMENTS[iid].dormant)


# ------------------------------------------------------------- fee schedules
# SYNTHETIC negotiated rates. Every fee is computed per fill and rounded to cents.
FEE_SCHEDULE_TEXT = {
    ALPHAX: "USD equities $0.0035/share; EUR equities 3.0 bp of notional",
    BONDHUB: "USD bonds 0.50 bp of notional; EUR bonds 0.75 bp of notional",
    MERIDIAN: "USD equities $0.0040/share; futures $1.25 exchange + $0.40 clearing + $0.85 brokerage per contract",
}
BONDHUB_FEE_BPS = {"USD": D("0.50"), "EUR": D("0.75")}
MERIDIAN_FUT_FEES = {"exchange": D("1.25"), "clearing": D("0.40"), "brokerage": D("0.85")}


def fee_components(venue: str, inst: Instrument, qty: Decimal, price: Decimal) -> dict[str, Decimal]:
    """Expected fee for one fill, split into the components the venue bills."""
    notional = qty * price * inst.price_factor
    if venue == ALPHAX:
        if inst.currency == "USD":
            return {"commission": money(qty * D("0.0035"))}
        return {"commission": money(notional * D("0.0003"))}
    if venue == BONDHUB:
        return {"platform": money(notional * BONDHUB_FEE_BPS[inst.currency] / D(10000))}
    if venue == MERIDIAN:
        if inst.asset_class == AssetClass.FUTURE:
            return {k: money(qty * v) for k, v in MERIDIAN_FUT_FEES.items()}
        return {"exchange": D("0.00"), "clearing": D("0.00"), "brokerage": money(qty * D("0.0040"))}
    raise ValueError(venue)


def expected_fee(venue: str, inst: Instrument, qty: Decimal, price: Decimal) -> Decimal:
    return sum(fee_components(venue, inst, qty, price).values(), D("0.00"))


# --------------------------------------------------------- opening balances
def opening_holdings() -> list[Holding]:
    """SYNTHETIC opening positions so that sells reduce longs rather than create shorts."""
    out: list[Holding] = []
    for acct in ACCOUNTS:
        for iid, inst in INSTRUMENTS.items():
            if inst.dormant or inst.asset_class == AssetClass.FUTURE:
                continue
            qty = D(400_000) if inst.asset_class == AssetClass.EQUITY else D(80_000_000)
            out.append(Holding("NORTHBRIDGE", acct, iid, qty))
            if iid in SYMBOL_XREF[MERIDIAN].values():
                out.append(Holding("MERIDIAN", acct, iid, D(300_000)))
    return out


def opening_cash() -> dict[tuple[str, str, str], Decimal]:
    out: dict[tuple[str, str, str], Decimal] = {}
    for acct in ACCOUNTS:
        out[("NORTHBRIDGE", acct, "USD")] = D("750000000.00")
        out[("NORTHBRIDGE", acct, "EUR")] = D("300000000.00")
        out[("MERIDIAN", acct, "USD")] = D("400000000.00")
    return out
