# State machines

Every state that persists is listed here with its legal transitions, the trigger
that causes them, the guard that must hold first, and the event that is written.
Anything not in these tables is a bug, not an edge case.

Notation: `A ──trigger[guard]──▶ B  (event, written row)`.

---

## 1. Signal — write-once, no states

A signal is **created fully formed and never modified**. There is no CREATED →
PENDING → … for signals; the "state" of a signal is derived from what
references it (risk decision, order, execution).

Creation guard (all enforced by code *and* by CHECK constraints):

| Guard | Where |
|---|---|
| `action ∈ {BUY, SELL, HOLD}` | `ck_signals_action_valid` |
| `0 ≤ confidence ≤ 1` (scaled: `≤ 100000000`) | `ck_signals_confidence_range` |
| `signal_candle_close_time > signal_candle_open_time` | `ck_signals_close_after_open` |
| `target_execution_open_time > signal_candle_open_time` | `ck_signals_target_after_candle` |
| `signal_id` unique | primary key (deterministic) |
| strategy triple (`name`, `version`, `config_hash`) recorded | columns |

Derived status (read-only views, not stored):

```
signal ──risk decision?──▶ UNDECIDED | APPROVED | REJECTED
       ──order?──────────▶ NO_ORDER  | PENDING | EXECUTED | TARGET_MISSED | CANCELLED
```

A `HOLD` signal is persisted (it is evidence that the strategy ran) but produces
no order.

---

## 2. RiskDecision — single-shot per signal

`risk_decisions.signal_id` **is** the primary key: exactly one verdict per
signal, ever. Re-running risk on the same signal is an idempotent no-op, and a
rejection can never be "re-risked" into an approval.

```
(no row) ──evaluate[signal exists]──▶ APPROVED   (sizing present, event RISK_APPROVED)
         └─evaluate[cap breached]──▶ REJECTED   (reason code, event RISK_REJECTED)
```

No transition between APPROVED and REJECTED, in either direction.

---

## 3. Order

```
                     ┌────────────────────────────────────────────┐
                     │                                            │
   create            │  queue                                    │ target candle
 signal ──▶ CREATED ─┴─▶ PENDING ─────────────────────────────▶ TARGET_REACHED
                            │                                        │
                            │                                        │ fill
                            │                                        ▼
                            │                                    EXECUTED  (terminal)
                            │
                            ├── gap/reorder detected ──▶ TARGET_MISSED
                            │                                 │
                            │                                 └─▶ CANCELLED (terminal)
                            │
                            └── explicit cancel ──▶ CANCELLED (terminal)
```

| From | To | Trigger | Guard | Event |
|---|---|---|---|---|
| — | `CREATED` | order engine builds the order | risk APPROVED, signal exists | `ORDER_CREATED` |
| `CREATED` | `PENDING` | same transaction as creation (atomic) | target is in the future | `ORDER_CREATED` payload includes state |
| `PENDING` | `TARGET_REACHED` | candle arrives with `open_time == target_execution_open_time` | candle `is_complete = True`, `candle_id == target_execution_candle_id` | `ORDER_EXECUTION_TARGET_REACHED` |
| `TARGET_REACHED` | `EXECUTED` | fill written in the same transaction | exactly one execution row, price > 0, provider = paper | `ORDER_EXECUTED` |
| `PENDING` | `TARGET_MISSED` | a candle with `open_time > target` arrives first, or the target candle is absent from a gap report | — | `ORDER_TARGET_MISSED` + `INTEGRITY_FAILURE` |
| `TARGET_MISSED` | `CANCELLED` | reconciliation (phase 7) closes the books on the missed target | — | `ORDER_CANCELLED` |
| `PENDING` | `CANCELLED` | explicit operator/engine cancel (e.g. position already open for the symbol) | — | `ORDER_CANCELLED` |

Forbidden transitions (all rejected in code *and* by
`ck_orders_state_valid`):

```
EXECUTED     → anything          (terminal)
CANCELLED    → anything          (terminal)
PENDING      → EXECUTED          (execution must pass through TARGET_REACHED)
TARGET_MISSED→ TARGET_REACHED    (a missed target is never "un-missed")
```

Row-level rules: `state_reason` records *why*; `state_updated_at` is set on every
transition; `order_id` and `signal_id` never change.

---

## 4. Execution — write-once

One row per order (`uq_executions_order_id`), written inside the same
transaction as the state transition `TARGET_REACHED → EXECUTED`. It is never
updated; a wrong fill is corrected by a *new* compensating flow (phase 7), never
by an UPDATE.

The hard invariant, evaluated immediately before insert:

```
execution.execution_candle_open_time  ==  order.target_execution_open_time
execution.execution_candle_id         ==  order.target_execution_candle_id
candle.is_complete                    is True
```

If any part fails → no row, `INTEGRITY_FAILURE`
(`EXECUTION_TARGET_MISMATCH` / `LATE_EXECUTION_ATTEMPT` /
`INCOMPLETE_CANDLE_USED`), and trading halts pending review.

---

## 5. Position

```
 create ──▶ OPEN ──exhaust (SL/TP/manual)──▶ CLOSING ──fill written──▶ CLOSED
              │                                 │
              └──────────── recovery (ph.7) ────┘   (CLOSING is crash-recoverable)
```

| From | To | Trigger | Guard | Event |
|---|---|---|---|---|
| — | `OPEN` | entry execution committed | no other OPEN position for `(symbol, timeframe)` — enforced by the partial unique index `uq_positions_one_open_per_sym_tf` | `POSITION_OPENED` |
| `OPEN` | `CLOSING` | intrabar SL or TP touched, or manual exit requested | exit price/quantity determined deterministically | `STOP_LOSS_HIT` / `TAKE_PROFIT_HIT` |
| `CLOSING` | `CLOSED` | exit execution + trade row committed in one transaction | trade round-trip reconciles with entry | `POSITION_CLOSED`, `TRADE_RECORDED` |

**Intrabar SL/TP policy (must be fixed now, implemented in phase 5):** when a
single candle touches both SL and TP, the engine assumes **SL executed first**
(stop-before-target). This is the conservative assumption: it never manufactures
a win the data cannot justify. The alternative (TP first) can only be chosen
with sub-candle data, which the baseline does not have. Whichever is chosen, it
is a property of the config — a run that changes it produces a different result
set and must be a different `strategy_config_hash`.

Close reason vocabulary is closed: `SL_HIT | TP_HIT | MANUAL | LIQUIDATION`
(`ck_positions_close_reason_valid`, `ck_trades_exit_reason_valid`).

---

## 6. PortfolioSnapshot

Not a state machine: one immutable point per processed candle,
`(symbol, timeframe, at_candle_open_time)` unique. `equity = cash + reserved +
open_position_value` is asserted by `ck_snapshots_equity_identity`, so a broken
accounting identity fails at INSERT rather than in a report.

---

## 7. Crash recovery (phase 7 contract)

The state machines are designed so that "what was in flight?" is always
answerable from the database alone:

| Observed on restart | Interpretation | Recovery |
|---|---|---|
| `PENDING` → target candle exists & complete | normal path simply did not run | process the target candle (idempotent by id) |
| `PENDING` → target candle absent (gap) | missed target | `TARGET_MISSED` + integrity event, then `CANCELLED` |
| `TARGET_REACHED` with no execution row | crashed between transition and fill | complete the fill atomically; the execution id is deterministic, so a partial retry cannot duplicate |
| `CLOSING` with no trade row | crashed mid-close | re-derive the close and commit, or reconcile to `OPEN` if the exit candle is provably absent |
| Unrecognised combination | contract violation | `UNKNOWN_STATE_ON_RECOVERY` (CRITICAL) + halt |

`STATE_RECOVERED` is written for every recovery action, so a restart is visible
in the audit trail rather than invisible.

---

## 8. Legal-transition enforcement

| Layer | Mechanism |
|---|---|
| Database | `CHECK (state IN (...))` on orders and positions; terminal states are simply absent from the allowed transition graph in code |
| Code | `app/common/errors.IllegalStateTransitionError` raised by the phase-5 order/position state modules |
| Tests | state-machine unit tests (phase 5) assert that every forbidden transition raises and writes an `ORDER_STATE_ILLEGAL_TRANSITION` / `POSITION_STATE_ILLEGAL_TRANSITION` integrity row |

Illegal transitions are **never** silently ignored and never auto-corrected.
