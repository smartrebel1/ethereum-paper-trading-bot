# Event catalog

Two append-only tables carry evidence:

* `system_events` — everything that happened (`event_type` from
  `app.integrity.enums.EventType`).
* `integrity_events` — everything that went *wrong* (`code` from `IntegrityCode`),
  with a severity and the operator action it demands.

Both are write-once. The only way to "change" history is to append a new row
(`STATE_RECOVERED`, a follow-up observation), which is exactly why a post-mortem
can trust them.

---

## 1. Envelope

```jsonc
{
  "event_id": "EVT::<sha256(type|entity|ts|seq)[:32]>",  // deterministic, PK
  "event_type": "ORDER_EXECUTED",
  "timestamp": "2026-01-01T04:00:00.123456Z",            // UTC, aware
  "sequence": 0,                                         // tiebreaker within one timestamp
  "symbol": "ETHUSDT",
  "timeframe": "4h",
  "entity_id": "ORD::9f2c…",                             // what it is about
  "payload": { },
  "software_version": "0.1.0",                           // which build wrote it
  "strategy_version": "1.0.0"                            // if strategy-driven
}
```

Ordering rule: `(timestamp ASC, sequence ASC)` — never timestamp alone, since a
startup batch can share one microsecond. `system_events.sequence` exists for
exactly this reason.

Deduplication rule: `event_id` is a pure function of the content, so replaying a
run rewrites the same ids and the insert is an idempotent no-op
(`repositories.system_events.record_event`).

---

## 2. Event types

### Data

| Event | Emitted when | entity_id | payload keys |
|---|---|---|---|
| `CANDLE_RECEIVED` | a raw candle arrives from a provider | `candle_id` | `source`, `is_complete`, `open_time`, `close_time` |
| `CANDLE_VALIDATED` | it passes every validator | `candle_id` | `checks[]`, `gap_filled` |
| `CANDLE_REJECTED` | it fails validation | `candle_id` (or `raw`) | `reason`, `violated`, `raw_excerpt` |
| `DATA_GAP_DETECTED` | expected candles are missing between stored ranges | range string | `expected`, `missing[]`, `first_missing`, `last_missing` |
| `DATA_SOURCE_REGISTERED` | a provider/dataset is registered | `dataset_hash` | `name`, `kind`, `candle_count`, `start`, `end` |

### Strategy / risk / order

| Event | Emitted when | entity_id | payload keys |
|---|---|---|---|
| `SIGNAL_CREATED` | strategy emits a signal from a closed candle | `signal_id` | `action`, `confidence`, `signal_candle_open_time`, `target_execution_open_time`, `target_execution_candle_id`, `strategy_config_hash`, `indicator_context` |
| `SIGNAL_SUPPRESSED` | a would-be signal is dropped by design | `signal_id` | `reason` (e.g. `warmup`, `min_confidence`) |
| `RISK_APPROVED` | risk accepts the signal | `signal_id` | `approved_quantity`, `approved_notional`, `stop_loss`, `take_profit` |
| `RISK_REJECTED` | risk rejects the signal | `signal_id` | `reason_code`, `reasons[]` |
| `ORDER_CREATED` | order row committed | `order_id` | `signal_id`, `side`, `intended_quantity`, `target_execution_open_time`, `state` |
| `ORDER_EXECUTION_TARGET_REACHED` | a candle arrived whose open_time equals the target | `order_id` | `candle_id`, `candle_open_time` |
| `ORDER_EXECUTED` | fill committed | `execution_id` | `order_id`, `price`, `quantity`, `fee`, `slippage_bps` |
| `ORDER_TARGET_MISSED` | the series moved past the target without it | `order_id` | `target_execution_open_time`, `saw_open_time`, `gap` |
| `ORDER_CANCELLED` | order is closed without execution | `order_id` | `reason`, `previous_state` |

### Position / portfolio / ledger

| Event | Emitted when | entity_id | payload keys |
|---|---|---|---|
| `POSITION_OPENED` | entry fill committed | `position_id` | `side`, `quantity`, `entry_price`, `stop_loss`, `take_profit`, `strategy_config_hash` |
| `STOP_LOSS_HIT` | intrabar SL touched | `position_id` | `candle_id`, `low`/`high`, `sl_price` |
| `TAKE_PROFIT_HIT` | intrabar TP touched | `position_id` | `candle_id`, `high`/`low`, `tp_price` |
| `POSITION_CLOSED` | exit fill committed | `position_id` | `exit_price`, `exit_reason`, `realized_pnl`, `holding_seconds` |
| `TRADE_RECORDED` | immutable round-trip row written | `trade_id` | `net_pnl`, `gross_pnl`, `fees`, `exit_reason` |
| `PORTFOLIO_SNAPSHOT_TAKEN` | snapshot row written for a candle | `snapshot_id` | `equity`, `cash`, `drawdown_pct` |

### System

| Event | Emitted when | entity_id | payload keys |
|---|---|---|---|
| `STARTUP_GUARD_PASSED` | process passed every safety check | `-` | `mode`, `software_version`, `strategy_config_hash` |
| `STARTUP_GUARD_FAILED` | process refused to start | `-` | `reason` (also written to stderr and exit code 2) |
| `STRATEGY_VERSION_REGISTERED` | a strategy build is seen for the first time | `config_hash` | `name`, `version`, `config_hash` |
| `CONFIGURATION_REGISTERED` | a configuration snapshot is recorded | `-` | `scope` |
| `STATE_RECOVERED` | a restart repaired in-flight state | the entity repaired | `action`, `observed_state`, `resolution` |
| `INTEGRITY_FAILURE` | any integrity code was recorded | the entity | `code`, `severity`, `details` |
| `SCHEDULER_TICK` | the scheduler completed a pass | run id | `candles_processed`, `duration_ms`, `next_run` |
| `SCHEDULER_MISSED` | a scheduled pass did not complete in time | run id | `missed_by_ms`, `last_successful_tick` |

### AI (isolated)

| Event | Emitted when | entity_id | payload keys |
|---|---|---|---|
| `AI_OBSERVATION_CREATED` | an observation was stored | `observation_id` | `provider`, `model`, `latency_ms` |
| `AI_ERROR` | provider call failed | `-` | `provider`, `error`, `attempt` |
| `AI_DISABLED` | shadow disabled but a run expected it | `-` | `provider` |

**Contamination rule:** no AI event may be consumed by strategy/risk/execution.
`tests/unit/test_ai_isolation.py` fails if a baseline module ever imports the AI
model or package.

---

## 3. Integrity codes and severity

Severity is derived from the code, never chosen ad hoc:

| Code | Severity | Trigger | Required action |
|---|---|---|---|
| `EXECUTION_TARGET_MISSED` | CRITICAL | target candle never arrived | halt, inspect series |
| `EXECUTION_TARGET_MISMATCH` | CRITICAL | a fill was attempted against the wrong candle | halt (should be impossible: guarded twice) |
| `LATE_EXECUTION_ATTEMPT` | CRITICAL | execution attempted after the target candle passed | halt |
| `EARLY_EXECUTION_ATTEMPT` | CRITICAL | execution attempted before the target candle closed | halt |
| `DUPLICATE_EXECUTION` | ERROR | a second execution for one order | investigate (unique index should prevent it) |
| `LOOKAHEAD_DETECTED` | CRITICAL | an input timestamped after the decision point was used | halt |
| `INCOMPLETE_CANDLE_USED` | CRITICAL | the in-progress candle reached the strategy | halt |
| `DUPLICATE_CANDLE` | ERROR | same identity ingested twice | benign if identical; inspect if not |
| `OUT_OF_ORDER_CANDLE` | ERROR | ingest order violated | inspect upstream ordering |
| `MISALIGNED_CANDLE` | ERROR | `open_time` off the timeframe grid | quarantine the candle |
| `OHLC_INVARIANT_BROKEN` | ERROR | high < low, etc. | quarantine (also blocked by CHECK) |
| `NEGATIVE_VOLUME` / `NON_POSITIVE_PRICE` | ERROR | impossible value | quarantine |
| `DATA_GAP` | WARNING | missing candles in a range | report; gap policy applies |
| `ORDER_STATE_ILLEGAL_TRANSITION` | ERROR | forbidden transition attempted | bug; must be fixed before running again |
| `POSITION_STATE_ILLEGAL_TRANSITION` | ERROR | same, for positions | bug |
| `BALANCE_INVARIANT_BROKEN` | CRITICAL | cash/equity identity broken | halt, rebuild from ledger |
| `LEDGER_MISMATCH` | CRITICAL | ledger ≠ recomputed PnL | halt, rebuild from ledger |
| `DUPLICATE_LEDGER_ENTRY` | ERROR | repeated ledger write | investigate idempotency |
| `STARTUP_MODE_INVALID` | CRITICAL | non-paper mode observed | process must not run |
| `CRASH_DURING_TRANSACTION` | ERROR | interrupted unit of work found on restart | replay via recovery contract |
| `UNKNOWN_STATE_ON_RECOVERY` | CRITICAL | state combination outside the contract | halt, human review |

`FATAL_INTEGRITY_CODES` (in `app/integrity/enums.py`) is the set the phase-8
scheduler uses to trip its kill-switch.

---

## 4. What must never be added

* An event that implies **live** execution (`LIVE_ORDER_SENT`, `BROKER_ACK`, …).
  There is no such code path; a test asserts the catalog does not contain it.
* An event written *after* the fact to paper over a failure ("RETRY_OK"). The
  fix for a bad event is a new, honest event.
* Any event whose payload contains an API key, secret, or full environment dump.
  `ConfigurationVersion` strips secrets for the same reason.
* Deleting or updating rows in either table. Resolution is a new row.

---

## 5. Retention (phase 9+)

`system_events` grows by roughly one row per processed candle per stage. At 4h
that is negligible; at 15m it is ~96 candles/day × ~8 events ≈ 800 rows/day.
Retention policy (archiving to Parquet, with the archive hash recorded in
`data_sources`) is a phase-13 concern and is out of scope now — the schema is
already append-only, which is the property that makes such a policy safe to add
later.
