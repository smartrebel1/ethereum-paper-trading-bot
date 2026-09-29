# Always-on live-paper deployment

Use an always-on Linux host. GitHub Actions is intentionally not the real-time
engine because it is a job runner, not a persistent WebSocket process.

Example on a VM:

    git clone https://github.com/smartrebel1/ethereum-paper-trading-bot.git
    cd ethereum-paper-trading-bot
    python3.12 -m venv .venv
    .venv/bin/pip install .
    cp .env.example .env
    .venv/bin/python scripts/init_db.py

Run the paper engine manually:

    .venv/bin/python scripts/run_live_paper.py

Install the systemd unit:

    sudo cp deploy/eth-paper-live.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now eth-paper-live
    sudo systemctl status eth-paper-live

The state is persisted at data/live_paper_state.json by default.

The FastAPI monitor is read-only and exposes:

    /live-paper
    /api/v1/live-paper/status

Do not add Binance API keys for this paper-only mode.
