# Architecture — ETHUSDT Paper Trading Engine

Status: **phase 1** (foundation). Phases 2-14 add engines on top of this
skeleton without restructuring it.

---

## 1. What this system is

A deterministic, single-instrument (ETHUSDT), paper-only trading research
engine. Its output is an auditable ledger of decisions and simulated fills plus
a reproducible research surface (config hash → signal → order → execution →
position → trade).

The governing question behind every design choice here is:

> *If this run and next month's run disagree, can I prove exactly why?*

Four properties follow from it, and they are non-negotiable:

| Property | Mechanism |
|---|---|
| **Deterministic identity** | Every entity id is a pure function of its semantic inputs (`app/common/ids.py`). No UUIDs on any financial row. |
| **Explicit execution target** | A signal names the exact candle that may fill it (`target_execution_open_time` + `target_execution_candle_id`); nothing else can. |
| **Append-only history** | Signals, executions, trades and events have no update path. Corrections are new rows. |
| **Paper-only by construction** | Three independent layers (§5); "live" cannot be represented. |

---

## 2. Component flow (as implemented)

```
┌───────────────────────────────────────────────────────────────────────────┐
│  EXTERNAL DATA SOURCES (phase 2)                                          │
│  Binance REST/WS · CSV archives · Mock provider (tests/replay)            │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │ raw klines
                                ▼
                 ┌─────────────────────────────────┐
                 │  MarketDataProvider (ABC, ph.2) │
                 │  fetch_closed_candles(since)    │
                 └───────────────┬─────────────────┘
                                 │ ONLY CLOSED candles leave the provider
                                 ▼
                 ┌─────────────────────────────────┐
                 │  CandleValidator (ph.2)         │
                 │  OHLC invariants · alignment ·  │
                 │  completion (now ≥ close_time) ·│
                 │  gap detection vs stored range  │
                 └───────────────┬─────────────────┘
                                 │ VALIDATED (or CANDLE_REJECTED)
                                 ▼
                 ┌─────────────────────────────────┐
                 │  DataIngestor (ph.2)            │
                 │  idempotent upsert by           │
                 │  (symbol, timeframe, open_time) │
                 │  strict chronological order     │
                 └───────────────┬─────────────────┘
                                 │
                                 ▼
      ╔══════════════════════════════════════════════════════════════╗
      ║  ORDERED CANDLE STREAM  = the engine's clock                 ║
      ║  one closed candle → one full pipeline pass, in order        ║
      ╚══════════════╦═══════════════════════════════════════════════╝
                     │
     ┌───────────────┼────────────────┬───────────────────────┐
     ▼               ▼                ▼                       ▼
┌──────────┐  ┌────────────┐  ┌───────────────┐   ┌────────────────────┐
│ Strategy │  │ RiskEngine │  │ OrderEngine   │   │ IntegrityMonitor   │
│ (ph.3)   │  │ (ph.4)     │  │ (ph.5)        │   │ (ph.7)             │
│ EMA200 + │  │ sizing +   │  │ state machine │   │ invariants, gaps,  │
│ ATR14    │  │ caps       │  │ + target lock │   │ crash recovery     │
└────┬─────┘  └─────┬──────┘  └───────┬───────┘   └────────────────────┘
     │ Signal       │ RiskDecision     │ Order(PENDING, target=T)
     │ (immutable)  │ (one per signal) │
     │              │                  ▼
     │              │        ┌──────────────────────────────┐
     │              │        │ PaperExecutionProvider (ph.5)│
     │              │        │ HARD INVARIANT:             │
     │              │        │   candle.open_time == T      │
     │              │        │ else → NO FILL, integrity    │
     │              │        │        event, trading STOPS  │
     │              │        └────────────┬─────────────────┘
     │              │                     │ Execution (1 per order)
     │              │                     ▼
     │              │        ┌──────────────────────────────┐
     │              │        │ PositionEngine + SL/TP (ph.5)│
     │              │        │ Portfolio + immutable ledger │
     │              │        │ (ph.6)  — one atomic tx      │
     │              │        └────────────┬─────────────────┘
     ▼              ▼                     ▼
┌───────────────────────────────────────────────────────────────────┐
│  system_events · integrity_events · ai_observations (ph.10)        │
│  append-only evidence; EventBus delivers in-process (app/events)   │
└───────────────────────────────────────────────────────────────────┘
                     │
                     ▼
     ┌──────────────────────────────────────────────────────┐
     │ FastAPI (ph.1): /health /ready /version /metrics      │
     │ + read-only /api/v1/* + /dashboard (static, ph.1)     │
     └──────────────────────────────────────────────────────┘

Scheduler (phase 8, ASYNCHRONOUS, internal to the process):
  wakes on a timer → asks the provider for closed candles → feeds the
  validator/ingestor in strict chronological order.
  It NEVER triggers execution. Execution happens only when a candle
  arrives whose open_time equals an order's target_execution_open_time.
```

**Correction to the original sketch:** the earlier diagram had the bus feeding
the execution engine. Execution is not a bus subscriber — it is triggered by a
*specific candle matching a specific target*. Making that explicit is what
prevents "execute on whatever candle is current", which is the failure mode the
whole design exists to avoid.

---

## 3. Layer rules

```
app/common        pure functions, no I/O, no imports from other app packages
app/config        settings + frozen strategy config (imports common only)
app/database      engine, session, custom types, Base
app/models        SQLAlchemy ORM (imports database only)
app/integrity     enums/codes; imported by everyone, imports nobody
app/events        in-process bus (imports common + integrity)
app/repositories  the ONLY place that writes rows
app/{market_data,candles,strategy,risk,execution,portfolio,ledger,scheduler}
                  engines: read/write through repositories, emit events
app/api           HTTP read-only surface over repositories/models
app/ai            isolated observer; never imported by the engines
```

Dependency direction is one-way: `api → repositories → models → database`, and
`engines → repositories`. No engine imports another engine's internals; they
communicate through persisted state and the event bus.

`tests/unit/test_ai_isolation.py` enforces the AI rule structurally, so it
cannot rot.

---

## 4. The canonical execution lifecycle

```
t0  Candle C0 closes at C0.close_time (= C0.open_time + timeframe)
    StrategyEngine evaluates *closed* candles only and emits a Signal:
        signal_candle_open_time    = C0.open_time
        signal_candle_close_time   = C0.close_time
        target_execution_open_time = C0.open_time + tf_duration   ← explicit
        target_execution_candle_id = candle_id(symbol, tf, target)
    (DB CHECK: target_execution_open_time > signal_candle_open_time)

t1  RiskEngine writes exactly one RiskDecision for the signal
    (APPROVED with sizing, or REJECTED with a reason code)

t2  OrderEngine creates one Order (PENDING) copying the target verbatim
    order_id = det_id("ORD", signal_id)   → one order per signal, forever

t3  The next candle C1 arrives:
        C1.open_time == target           → TARGET_REACHED, then EXECUTED at
                                           C1.open (+ slippage, + fee)
        C1.open_time != target           → TARGET_MISSED, INTEGRITY_FAILURE
                                           (EXECUTION_TARGET_MISSED), NO FILL,
                                           trading halts pending review
```

Why the divergence from the original plan, which said "MARK_MISSED, continue":
a gap or reorder means the engine is **not looking at the series it thinks it
is**. Continuing would silently produce a different strategy than the one whose
config hash is stamped on the signal. Halting is the only response that keeps
"config hash → result" a truthful claim. Phase 5 implements exactly this; the
policy switch (halt vs per-order cancel) is recorded in
[`DECISIONS.md`](DECISIONS.md) (ADR-012).

---

## 5. The paper-only guarantee (three independent layers)

1. **Type level** — `TradingMode` and `ExecutionProvider` have no `LIVE` member.
   `TRADING_MODE=live` fails pydantic *parsing*; it never becomes a `Settings`
   object.
2. **Validation/startup level** — `Settings._safety_guard` and
   `enforce_paper_only()` re-check; `app.main._startup_guard()` raises
   `SystemExit(2)`, and module import itself is guarded, so
   `uvicorn app.main:app` cannot boot into an unknown mode.
   Proven by subprocess tests (`tests/unit/test_safety_guard.py`).
3. **Database level** — `ck_orders_paper_only` and `ck_executions_paper_only`
   CHECK constraints make a non-paper row unrepresentable, even by hand-written
   SQL or a compromised process.

Tests cover all three, including "doctored settings object that bypassed
pydantic".

---

## 6. Data conventions

### 6.1 Identity
Deterministic ids everywhere on the financial path:

| Entity | Formula | Consequence |
|---|---|---|
| candle | `CDL::SYMBOL::TF::openTimeZ` | re-ingest = same row, upsert is a no-op |
| signal | `SIG::sha256(symbol,tf,candleOpen,strategy,version,configHash)[:32]` | same decision ⇒ same row; changed config ⇒ different row |
| order | `ORD::sha256(signal_id)[:32]` | one order per signal, even across restarts |
| execution | `EXE::sha256(order_id,target)[:32]` | a missed target can never be "retried" under the same id |
| position / trade | derived from entry order / position | replay reproduces the ledger exactly |
| event | `EVT::sha256(type,entity,ts,seq)[:32]` | identical event log for identical runs |

Autoincrement PKs are used **only** on pure bookkeeping tables
(`strategy_versions`, `configuration_versions`, `data_sources`,
`scheduler_runs`, `health_checks`) where identity is not semantic.

### 6.2 Numbers
* Money/price/quantity are stored as **scaled integers** (`FixedPoint`), not
  floats and not SQLite REAL: an 8-decimal price round-trips exactly, and SQL
  comparisons in CHECK constraints are integer comparisons.
  Scales: price 8, quantity 10, money 8, ratio 8 (`app/common/rounding.py`).
* All Python-side quantization goes through `app/common/rounding.py`
  (ROUND_HALF_EVEN, explicit at the call site).
* **Consequence to remember:** a CHECK constraint that bounds a scaled column
  must be written in scaled units — `confidence <= 100000000`, not `<= 1`.
  This bug was caught by the constraint tests; see ADR-003.

### 6.3 Time
* Every persisted datetime is timezone-aware UTC; the `UTCDateTime` type
  rejects naive input at the driver boundary and re-attaches UTC on read (SQLite
  would otherwise silently return naive datetimes).
* `open_time` inclusive, `close_time` exclusive and equal to the next boundary.
* A candle is usable only when `now >= close_time`.

### 6.4 Simulation-only columns
Tables carry fields the current phase does not populate yet
(`positions.entry_order_id`, `trades.exit_order_id`, `portfolio_snapshots.*`,
`ai_observations.*`). They are there because adding them later means a data
migration on live history; leaving them null-able now costs nothing.

---

## 7. Startup and failure policy

```
import app.main
  └─ create_app()             (config parsed here; invalid → SystemExit(2))
       └─ lifespan startup
            ├─ enforce_paper_only()          → fail closed → SystemExit(2)
            ├─ register strategy version      (idempotent upsert)
            ├─ register configuration version (idempotent, secrets stripped)
            └─ append STARTUP_GUARD_PASSED    (seq-ordered event)
```

| Situation | Behaviour |
|---|---|
| Unsafe mode / flag | process exits with code 2, `FATAL:` on stderr. No "warn and continue". |
| Invalid config (pydantic) | same path, before any I/O |
| DB not initialised | process starts; `/health` OK, `/ready` not-ready, `/api/v1/*` → 503 with the exact fix command; startup bookkeeping is skipped with a WARNING log |
| Observer handler raises | contained by the EventBus, reported to the publisher, persisted as an event; the pipeline continues |
| Integrity violation (phase 2+) | persisted to `integrity_events`; CRITICAL codes set the kill-switch flag for the scheduler |

---

## 8. Observability

| Surface | Purpose |
|---|---|
| `/health` | liveness, no DB access — works before `init_db` |
| `/ready` | safety + database + schema checks, each with a detail string |
| `/version`, `/api/v1/config` | software/strategy identity, config hash, coverage |
| `/metrics` (JSON) | row counts, open position, last trade, last event, critical integrity count |
| `/metrics/prometheus` | same data as gauges; generated directly, no client library |
| `/dashboard` | read-only operational view (same origin, no build step) |
| `/docs` | OpenAPI, read-only surface only |
| logs | one JSON object per line, `component`, `ts`, arbitrary `extra` fields |

**Definition of "healthy" for this engine** (phase 1): `/ready` all-green and
`integrity_critical_count == 0`. Everything else (equity, win rate) is research
output, not health.

---

## 9. Deliberate exclusions in phase 1

No ingestion, no indicators, no risk logic, no fills, no ledger, no scheduler,
no AI calls, no backtest. Their packages exist with documented scope so that the
"empty" state is a statement rather than an omission. Phase 1 is done when its
guarantees are *tested*, not when it is feature-complete — the guarantees are
the part that cannot be retrofitted.

See also: [`STATE_MACHINE.md`](STATE_MACHINE.md), [`EVENTS.md`](EVENTS.md),
[`DECISIONS.md`](DECISIONS.md).
