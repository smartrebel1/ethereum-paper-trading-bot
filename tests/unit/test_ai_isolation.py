"""AI contamination isolation (structural, forward-looking).

The AI shadow observer is allowed to *read* market context and *write* rows to
``ai_observations``. It must never be able to influence a trading decision.

Because the trading engines do not exist yet (phases 3-8), this test guards the
*future*: any import of the AI model from a trading-path module fails here.
"""

from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Modules that form the baseline (non-AI) decision path.
BASELINE_PACKAGES = (
    "strategy",
    "risk",
    "execution",
    "portfolio",
    "ledger",
    "scheduler",
    "candles",
)

#: Modules allowed to know about AI observations at all.
AI_AWARE = ("ai", "api", "models", "repositories", "main.py")


def _python_files(*relative: str) -> list[Path]:
    files: list[Path] = []
    for rel in relative:
        path = APP_ROOT / rel
        if path.is_dir():
            files.extend(p for p in path.rglob("*.py"))
        elif path.is_file():
            files.append(path)
    return files


def test_baseline_packages_never_import_ai_models() -> None:
    offenders: list[str] = []
    for path in _python_files(*BASELINE_PACKAGES):
        text = path.read_text(encoding="utf-8")
        if "AIObservation" in text or "ai_observation" in text:
            offenders.append(str(path.relative_to(APP_ROOT)))
    assert offenders == [], "the baseline trading path must not import AI artifacts; found: " + ", ".join(
        offenders
    )


def test_ai_package_is_not_imported_by_the_baseline_path() -> None:
    offenders: list[str] = []
    for path in _python_files(*BASELINE_PACKAGES):
        text = path.read_text(encoding="utf-8")
        if "from app.ai" in text or "import app.ai" in text:
            offenders.append(str(path.relative_to(APP_ROOT)))
    assert offenders == [], f"baseline path imports app.ai: {offenders}"


def test_ai_allowed_modules_are_the_only_readers() -> None:
    """Sanity check that the allow-list above is not stale."""
    ai_files = _python_files("ai")
    assert ai_files, "app/ai must exist as a package (phase 10 placeholder)"
    for name in AI_AWARE:
        assert (APP_ROOT / name).exists(), f"allow-listed path missing: {name}"


def test_ai_model_has_no_trading_side_effects() -> None:
    """AIObservation holds context + response only: no order/position references."""
    from app.models.ai_observation import AIObservation

    column_names = set(AIObservation.__table__.columns.keys())
    forbidden = {"order_id", "position_id", "trade_id", "signal_id", "execution_id"}
    assert column_names & forbidden == set()
