# Resume bullets

Pick the version that fits the role. Every figure comes from the deterministic run in
`results/results.json` and describes a **synthetic simulation**. Don't present it as
production experience or as reconciliation of real accounts.

## Trading systems / trading platform engineering

- Built an event-driven trade lifecycle and reconciliation system in Python and DuckDB that
  normalizes three deliberately different venue protocols (FIX 4.4, CSV, JSON) plus a
  SWIFT-style custodian feed into one Decimal-exact trade model, and processes 3,200 orders
  and 8,003 partial fills from order through allocation, settlement, positions, cash and P&L.
- Designed idempotent ingestion keyed on venue message ID and content hash, with versioned
  append-only fills and allocations. 150 replayed messages were absorbed with zero
  double-counting, and corrections re-allocate blocks with exact-to-the-cent conservation.
- Built a deterministic discrete-event message bus behind a two-method publish/subscribe
  interface (Kafka/NATS-swappable). The whole pipeline reproduces byte-identically from one
  command and is verified in CI.

## Trade support / middle office / operations technology

- Implemented T+0 trade, fee, confirmation, settlement, end-of-day position and cash
  reconciliations across three venues and two custodians, with an exception queue tracking
  severity, owner, age, expected vs actual, resolution action and a full audit trail.
- Detected all 88 injected breaks across 10 break types with zero false positives in the
  baseline scenario, and still caught 100% of 258 overlapping incidents at 94% precision in a
  stress scenario. Documented the overlap cases where rule-based classification fails.
- Showed that one 10x fat-finger on a bond fill, left unreconciled, became a 45M-face
  position error and a $45.5M settled-cash misstatement. With reconciliation enabled it was
  flagged CRITICAL 23 minutes after execution and corrected before allocation.

## Quant / quant developer

- Modelled settlement cycles (T+0/T+1/T+2 with business-day calendars), 30/360 and ACT/ACT
  accrued interest, futures variation margin and USD P&L as pure functions over versioned
  bookings, so any historical date can be recomputed after a correction.
- Built a measurement harness that scores the reconciliation engine against injected ground
  truth (precision, recall, detection latency, unresolved breaks, book error before and after
  remediation). The engine itself is kept blind to ground truth, enforced by a test.

## One-liner

Deterministic multi-venue trade lifecycle and reconciliation simulator (Python, DuckDB,
FIX/CSV/JSON adapters): 3,200 orders, 10 break types, idempotent capture, an exception queue
with audit trail, and 100% recall on injected breaks with the failure modes documented.
