# Architecture decision record

Format: context → decision → consequences → alternatives rejected.
Entries marked **[FIX]** correct an error in the phase-1 draft this codebase was
built from; entries marked **[NEW]** are additions the draft did not specify.

---

## ADR-001 [FIX] Migrations via autogenerate + normaliser, not `create_all`

**Context.** The draft's `0001_initial_schema.py` called
`Base.metadata.create_all(bind=op.get_bind())`. Two consequences: the migration
is not real DDL (a later `--autogenerate` diffs models against the DB with no
revision describing the difference, so the next migration is unpredictable), and
it imports application code, so the migration stops working the moment the models
are refactored.

**Decision.** Generate `0001` with `alembic revision --autogenerate`, then run
`scripts/normalize_migration.py` to replace the custom types with plain
SQLAlchemy types (`UTCDateTime → sa.DateTime(timezone=True)`,
`FixedPoint → sa.BigInteger`). The revision is self-contained DDL.

**Consequences.** `alembic check` reports *no drift* (asserted by a test);
`downgrade base` genuinely reverses the schema; the migration survives model
refactors. Cost: regenerating `0001` must be followed by the normaliser (one
command, documented in the file header).

**Rejected.** Keeping `create_all` "temporarily"; importing `app.*` inside
migrations (couples history to present code).

---

## ADR-002 [NEW] Fixed-point integer storage for money

**Context.** SQLite has no DECIMAL: `Numeric(20, 8)` is REAL and SQLAlchemy warns
that Decimal→float→Decimal is lossy. A ledger whose values change when you read
them back is not a ledger.

**Decision.** `FixedPoint(scale)` stores exact scaled integers (price 8, qty 10,
money 8, ratio 8). Scales live in `app/common/rounding.py`; quantization is
explicit at the call site with ROUND_HALF_EVEN.

**Consequences.** Exact round-trips (`2500.12345678` in = out), exact SQL
comparisons, no float drift over thousands of trades. Cost: CHECK constraints on
scaled columns must use scaled bounds (see ADR-003), and the physical type is
INTEGER rather than NUMERIC — documented in the migration.

**Rejected.** REAL/float (non-deterministic); TEXT decimals (lexicographic
comparison traps); a separate price table (premature).

---

## ADR-003 [FIX] Scaled CHECK bounds; caught by a failing test

**Context.** With fixed-point storage, `CHECK (confidence >= 0 AND confidence <= 1)`
compares the *stored* integer (0.75 → `75000000`) against `1` — so **every**
signal insert would fail. The draft's schema had exactly this bug, and it was
only visible once the constraint was exercised against a real database.

**Decision.** Ratio bounds are written in scaled units, derived from the scale
constant: `f"confidence <= {10**RATIO_SCALE}"`. A test asserts the generated DDL
contains the scaled bound, and behavioural tests insert a valid signal (0.75) and
reject 1.5.

**Consequences.** Range checks cannot be "obvious" one-liners; any future bounded
scaled column needs the same treatment. Two tests pin it.

**Rejected.** Dropping the range CHECK (loses a real invariant); storing ratios as
plain integers 0-100 (loses precision and makes the scale implicit).

---

## ADR-004 [FIX] `UTCDateTime`: naive datetimes are refused

**Context.** SQLite's DATETIME silently drops tzinfo on write and returns naive
datetimes on read. Naive/aware confusion is the standard mechanism behind
look-ahead and "off by the server's offset" bugs. The draft's `is_aligned` was
also malformed (`ts.utcoffset().class(0)`), and the draft had no read-path
protection at all.

**Decision.** A `UTCDateTime` TypeDecorator rejects naive input at bind time
(loud `ValueError`, not a silent UTC assumption) and re-attaches UTC on read. All
datetime columns use it; a test asserts no column bypasses it.

**Consequences.** Every persisted timestamp is aware UTC, including across
SQLite/Postgres. Cost: external data must be explicitly normalized
(`to_utc`, `ensure_aware_utc`), which is a feature — it forces the decision to be
made in one place.

**Rejected.** Relying on `DateTime(timezone=True)` (SQLite ignores it);
converting inside every query (easy to forget one).

---

## ADR-005 [FIX] Canonical decimal strings in the config hash

**Context.** The draft stored Decimals as `str(Decimal(...))`, which preserves
trailing zeros: `Decimal("1.50") != str(Decimal("1.5"))`, so two numerically
identical configs would hash differently — breaking the config-hash identity
that every signal/order/trade is stamped with.

**Decision.** `canonical_decimal()` normalizes (`"1.50" → "1.5"`, `"200" → "200"`)
and `StrategyConfig.__post_init__` applies it to every decimal field, so the hash
depends on *value* regardless of construction path. The baseline hash is pinned
in a test.

**Consequences.** Equal configs always hash equally; a real parameter change still
changes the hash, and the pinned test forces a conscious version bump.

**Rejected.** Hashing `float(value)` (reintroduces float noise); trusting callers
to normalize.

---

## ADR-006 [NEW] Injectable `Clock` in phase 1, not phase 5

**Context.** The draft deferred clock injection to phase 5 as a "trivial
refactor". It is trivial only *before* engines exist; afterwards it means
touching every engine and every test.

**Decision.** `app/common/clock.py` ships `Clock`, `SystemClock`, `FrozenClock`
(with `auto_advance`), `ShiftedClock` and a process-wide `get_clock()/set_clock()`.

**Consequences.** Replay/backtest determinism is available from phase 3 onward;
"does the engine behave correctly when it wakes up late?" becomes a test
(`ShiftedClock`) rather than a thought experiment.

---

## ADR-007 [FIX] The process fails closed at import, not just at startup

**Context.** The draft relied on a `create_app()`-time guard while also doing
`app = create_app()` at module import. With `TRADING_MODE=live`, pydantic raises
*before* the guard, so the process dies with a validation traceback rather than a
clear FATAL message and a defined exit code — and the draft's own test reloaded
`app.main` to check `_startup_guard()`.

**Decision.** `_build_app_or_exit()` wraps module-level app construction: any
failure prints `FATAL: refusing to start with an invalid configuration: …` to
stderr and exits with code 2. `_startup_guard()` maps *any* invalid configuration
to the same exit code. Verified by subprocess tests that import `app.main` with a
hostile environment and assert exit code 2 + `FATAL` on stderr.

**Consequences.** One failure mode, one message, one exit code, provable from the
outside. Cost: two subprocess tests (~1s each).

**Rejected.** Warning and continuing in paper mode "for convenience" (that is how
live trading gets enabled by accident).

---

## ADR-008 [FIX] Deterministic event ordering via sequence

**Context.** Startup writes several events in one transaction with the same
timestamp; "latest event" queries then return an arbitrary row. A test caught
this (`last_event_type` was `STRATEGY_VERSION_REGISTERED` rather than
`STARTUP_GUARD_PASSED`).

**Decision.** Events carry a `sequence`, startup assigns increasing values, and
every "latest" query orders by `(timestamp DESC, sequence DESC)`.

**Consequences.** Event history is totally ordered and replay-stable; the audit
trail is readable. Cost: one extra column.

---

## ADR-009 [FIX] `set_engine(None)` must not rebuild

**Context.** The draft's `reset_engine()` called `set_engine(None)` which called
`get_engine()` — eagerly rebuilding an engine from *current* configuration. Test
teardown then failed with a validation error when the environment was still
dirty.

**Decision.** `set_engine(engine)` installs/clears and returns; `None` means
"forget", with the rebuild left lazy to `get_engine()`.

**Consequences.** Reset works regardless of environment state; no surprise I/O on
teardown.

---

## ADR-010 [NEW] Cross-field configuration invariants

**Context.** The draft validated fields individually. Two dangerous
combinations passed: `TP ≤ SL` (a strategy whose reward is below its risk cannot
pay its fees) and `WARMUP_CANDLES < max(EMA, ATR)` (indicators emit NaN into a
live decision).

**Decision.** `Settings` enforces `tp > sl`, `warmup ≥ max(ema, atr)`, AI
coherence (shadow requires a provider; gemini requires a key), a valid log level,
a valid DB scheme, and a plausible symbol. Each has a test.

**Consequences.** A misconfigured strategy cannot start. Cost: adding a strategy
parameter means deciding which invariant it participates in.

---

## ADR-011 [NEW] Read-only HTTP surface + same-origin dashboard

**Context.** The draft had `/health`, `/ready`, `/metrics`. That is not enough to
operate or to review an engine, and the phase-9 Streamlit/Next stack is a large
dependency to add to a foundation phase.

**Decision.** A `/api/v1/*` read-only surface (config, status, candles, signals,
orders, positions, trades, portfolio, events, integrity), Prometheus text at
`/metrics/prometheus`, and a dependency-free single-file dashboard served at
`/dashboard` which the browser-facing root (`/`) redirects to via content
negotiation.

**Consequences.** The API is auditable by inspection: **no write endpoint
exists**, so mounting it cannot change trading state. Everything degrades
gracefully before `init_db` (503 with the fix command). Cost: the dashboard is
deliberately not the final one; phase 9 replaces it.

**Rejected.** Adding Next.js/Streamlit now (runtime + build pipeline in a phase
whose deliverable is a guarantee).

---

## ADR-012 [FIX] Policy when an execution target is missed: halt

**Context.** The draft said: `ORDER_TARGET_MISSED` → continue with
`EXECUTION_TARGET_MISSED` logged. But a missing/reordered target means the engine
is not looking at the series it believes it is; continuing produces results that
do not correspond to the stamped `strategy_config_hash`.

**Decision.** No fill, `TARGET_MISSED`, `INTEGRITY_FAILURE` with severity
CRITICAL, and the scheduler's kill-switch trips; a human (or phase-7
reconciliation) decides. A gap is a data-integrity event, not a trading event.

**Consequences.** The invariant "config hash → results" stays truthful. Cost:
an automatic run stops on a data gap and needs attention — the correct tradeoff
for a research engine whose whole value is trustworthy history.

**Rejected.** Silently executing on the next available candle (fabricates fills);
silently skipping (hides the gap).

---

## ADR-013 [NEW] `risk_decisions.reasons` is a list

**Context.** The draft declared `reasons: dict` with a default of `dict`, while
the (broken) validator implied a list.

**Decision.** `reasons` is `JSON NOT NULL DEFAULT '[]'`, a list of structured
reason objects; `reason_code` carries the single machine-readable code.

**Consequences.** Uniform shape for both outcomes; filters/queries by
`reason_code` remain simple.

---

## ADR-014 [FIX] Dependency hygiene

**Context.** The draft pinned `pandas`/`numpy` as core dependencies; the engine
itself needs neither (it is exact-decimal arithmetic and SQL). It also declared
`requires-python >= 3.12`.

**Decision.** Core deps are what the engine actually imports; `pandas`/`numpy`
moved to the optional `research` extra. Wheel packaging declared (`hatchling`).

**Consequences.** `pip install -e .` installs a small, auditable dependency set;
research tooling is opt-in. Verified on Python 3.13 (SQLAlchemy 2.1, FastAPI
0.141, Pydantic 2.13).

---

## ADR-015 [NEW] Wrong-plan corrections applied to the schema

Small but real defects found while implementing the draft's schema, all fixed and
covered by tests:

| Draft | Problem | Fix |
|---|---|---|
| `ck_signal_target_after_candle` (name) | referenced a table name that did not exist; constraint was also truncated mid-line | `ck_signals_target_after_candle`, enforced by test + migration |
| `low = 0` in the candle volume CHECK | a truncated edit of `low <= open` / `volume >= 0`; would break every candle insert | split into `ck_candles_low_le_open`, `ck_candles_low_le_close`, `ck_candles_volume_nonneg` |
| `_iso()` in `ids.py` | contained a dead `if False else` chain and an unreachable branch; would emit local-offset strings | single `_iso_utc()` that refuses non-UTC |
| `is_aligned()` | expression `ts.utcoffset().class(0)` is not valid logic | `ensure_aware_utc` + epoch modulo |
| `UniqueConstraint("position_id")` on positions | a unique constraint on the primary key: dead weight | replaced by the partial unique index on `(symbol, timeframe) WHERE state='OPEN'` (which is the actual invariant the comment claimed) |
| `api/*/init.py`, `app/init.py`, `models/init.py` | files named `init.py` (no underscores) and `from future import` — not importable as packages | real `__init__.py` files with real exports |
| `%(message)r` in the log format | emits Python reprs (`'text'`), producing invalid JSON | `JsonFormatter` renders via `json.dumps`; regression test asserts the message is a JSON string |
| `.env.example` "Instrument" line | stray uncommented text after `timeframe=4h` | every non-setting line commented |
| `Settings` error path | `-4` comparison / truncated validator | full validators + tests |
| `scheduler_runs` / `health_checks` uniqueness | "uniqueness" on `(started_at, name)` / `(checked_at, component)` would be intended for heartbeats, but real heartbeats must *repeat*; the draft's `UniqueConstraint("position_id")`-style entries were inconsistent with their own purpose | dropped as constraints; both are append-only logs with covering indexes |
| `AI_SHADOW` config | draft allowed `enable_ai_shadow=true` with `provider=disabled` | validation error |

---

## ADR-016 [NEW] No mutable cash balance

**Context.** The obvious implementation keeps a `cash` column and mutates it on
every fill. Then the balance can drift from the ledger, and recovery has to
repair it — the classic source of "the numbers do not add up".

**Decision.** There is no stored balance. Cash, reserved cash, equity, peak and
drawdown are **recomputed from immutable rows** (`accounting.compute_state`):

    cash   = starting_balance + Σ net_pnl of closed trades      (the ledger)
    equity = cash + reserved + open_position_value

`cash_flow_residual()` reports the (bounded) difference between this and the
cash-flow view (`exit notional − entry notional − fees`): quantisation happens
per trade in one and per execution in the other, so they may differ by at most
one unit of the money scale per round trip — a checked quantity, not a hidden
one.

**Consequences.** The invariant `cash == starting + realised PnL` holds by
construction; replaying history reproduces the equity curve exactly; recovery has
nothing to repair because there is nothing cached to corrupt.

---

## ADR-017 [NEW] Declared data holes: void, do not halt

**Context.** The archive is not perfect: Binance's own 4h history has 9 holes
(17 missing candles, e.g. 2018-02-08 ×7, 2019-05-15 ×2). An entry signal whose
execution target falls inside one of them can **never** be filled — no candle
will ever carry that open time. Under ADR-012 that is a `TARGET_MISSED`, a
CRITICAL integrity event and a halt, which is right when the *series* is
unexpected but wrong for a hole the exchange published that way: the engine would
halt forever and no historical replay could ever finish.

**Decision.** The engine distinguishes two cases, and both keep the invariant
"never fill at a substitute candle":

| Case | Detection | Order | Integrity | Run |
|---|---|---|---|---|
| target candle missing, not declared | target time passed, not in the declared set | `TARGET_MISSED` → `CANCELLED` | `EXECUTION_TARGET_MISSED`, **CRITICAL** | halts (default) |
| target inside a declared hole | target time ∈ `find_missing_open_times(series)` | `CANCELLED`, reason `TARGET_UNREACHABLE_DATA_GAP` | `EXECUTION_TARGET_UNREACHABLE`, WARNING | continues |

The declared set is computed **from the series itself, before the run**, by the
same gap detector the ingestor uses, so the engine and the ingest-time gap report
can never disagree. If an exit is ever voided this way, the position is re-opened
(`CLOSING → OPEN`): an exit level is a price condition, not a time target, so it
survives the hole and can be re-armed on the next candle that exists.

**Consequences.** A full 2017→2026 replay completes (19,794 candles, 366 trades,
1 voided target) instead of stopping in 2019, and the data holes stay visible in
`data_sources`, the gap report, the dashboard and `integrity_events` — nothing is
papered over. A genuine "the series is not what I think it is" still halts.

---

## ADR-018 [NEW] Revisions must be offline-inspectable, except where SQLite forbids it

**Context.** Migrations should be reviewable as SQL without a database. Revision
`0003` narrows a UNIQUE constraint on `orders` (one order per signal *and*
purpose), and SQLite cannot drop a constraint in place: alembic must rebuild the
table in batch mode, which needs a live connection to reflect it.

**Decision.** Keep the property where it holds and name the exception: revisions
`0001`–`0002` render with `alembic upgrade --sql`; `0003` is verified against a
real database by the downgrade→upgrade and drift (`alembic check`) tests. The
test states the reason, so nobody "fixes" it later by weakening the assertion.

---

## ADR-019 [NEW] A plain-Arabic dashboard over the real ledger

**Context.** The operator is not a developer: JSON, table names and English
reason codes are not an answer to "did we make money, and when is the next trade".

**Decision.** `/dashboard` renders a self-contained, right-to-left Arabic panel
answering four questions — ما اشتريناه، وقت التنفيذ، الربح/الخسارة، وموعد الصفقة
القادمة — from the same immutable rows the engine writes. `/api/v1/dashboard/summary`
exposes the identical numbers as JSON, and `scripts/render_dashboard.py` writes the
same page to `dashboard/dashboard_ar.html` for offline viewing.

* Times are shown in **Cairo** time; amounts in USD with explicit signs.
* Reason codes are translated (`app/dashboard/summary.py: REASON_MEANINGS`);
  an unknown code degrades to a neutral Arabic sentence, never to the raw code.
* The page has **no external resources and no scripts**, so it renders identically
  in a browser, in a sandboxed preview and from disk.
* With no data it says so, and prints the two commands that fix that.

---

## ADR-020 [NEW] No CI, no hosting, no external service in the loop

**Context.** The original brief said the engine must not depend on GitHub Actions
or any hosted CI: tests, replay and (later) the scheduler must all run on the
machine that owns the database.

**Decision.** Running, testing and scheduling use nothing but the local checkout:

* `pytest` and `ruff` run locally; the suite is fully offline (no network, no
  Docker, no external service) and finishes in ~20 s;
* market data comes from a **static archive** that is fetched once and then read
  from disk;
* the future scheduler is an in-process component (ADR: scheduler phase), not a
  cron job on a build server;
* Git is optional and only ever holds a copy of the *code*. The database
  (`data/`), the virtualenv and the archive are deliberately not tracked.

**Consequences.** The system is auditable and reproducible on a laptop with no
account anywhere. Nothing about the engine's behaviour can change because a
third party changed a runner image, a token expired, or a repository moved.
