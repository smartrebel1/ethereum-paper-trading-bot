# Real-Time Paper Trading

This is the live-paper path. It is intentionally separate from the deterministic
historical replay engine.

## Behaviour

1. Connects to Binance public trade and 4h kline streams.
2. Waits for a genuinely new closed 4h candle.
3. Evaluates the existing EMA200 + ATR14 baseline on that closed candle.
4. If the baseline produces BUY and risk guards allow it, arms an entry for 120 seconds.
5. The next live trade fills the virtual position at the observed live price.
6. Stop-loss and take-profit are checked on every incoming live trade.
7. State survives process restarts in data/live_paper_state.json.
8. No Binance API key is read or required and no order endpoint exists in this path.

The 4h candle is the analysis/filter, while the WebSocket price is the execution
and position-monitoring clock.

Run:

    python scripts/run_live_paper.py

Reset the virtual account:

    python scripts/run_live_paper.py --reset

The service should run on a persistent machine such as an Oracle VM or another
always-on Linux host. GitHub Actions is not used as the real-time engine because
it is a job runner, not a persistent WebSocket process.

Binance documents real-time trade and kline streams and time-limited WebSocket
connections; the service reconnects with exponential backoff.
