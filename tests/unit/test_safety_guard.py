"""The safety guard must fail closed in every direction.

Covers three independent layers:
1. ``Settings`` refuses to *exist* in an unsafe state (pydantic),
2. ``enforce_paper_only`` re-checks a built settings object,
3. ``app.main`` exits with code 2 at import/startup (verified in a **real
   subprocess**, which is the only way to prove the process-level contract).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.common.errors import SafetyViolationError, StartupGuardError
from app.common.safety import assert_paper_provider, enforce_paper_only
from app.config.settings import Settings, clear_settings_cache, get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD_EXIT_CODE = 2


def test_enforce_paper_only_passes_for_real_settings() -> None:
    report = enforce_paper_only(get_settings())
    assert report.ok is True
    assert {c["name"] for c in report.to_dict()["checks"]} >= {
        "trading_mode_is_paper",
        "live_flag_disabled",
        "live_trading_mode_absent_from_enum",
        "live_provider_absent_from_enum",
    }


def test_enforce_paper_only_raises_on_doctored_mode() -> None:
    """Even if someone bypasses pydantic, the guard refuses."""
    fake = SimpleNamespace(trading_mode="live", enable_live_trading=False)
    with pytest.raises(StartupGuardError):
        enforce_paper_only(fake)


def test_enforce_paper_only_raises_on_doctored_flag() -> None:
    fake = SimpleNamespace(trading_mode="paper", enable_live_trading=True)
    with pytest.raises(StartupGuardError):
        enforce_paper_only(fake)


def test_assert_paper_provider() -> None:
    assert_paper_provider("paper")
    with pytest.raises(SafetyViolationError):
        assert_paper_provider("live")


def test_settings_themselves_refuse_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        Settings()


def test_startup_guard_refuses_invalid_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard turns *any* invalid configuration into ``SystemExit(2)``."""
    from app import main as main_mod

    monkeypatch.setenv("TRADING_MODE", "live")
    clear_settings_cache()
    with pytest.raises(SystemExit) as exc:
        main_mod._startup_guard()
    assert exc.value.code == main_mod.GUARD_FAILURE_EXIT_CODE

    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    clear_settings_cache()
    with pytest.raises(SystemExit) as exc:
        main_mod._startup_guard()
    assert exc.value.code == main_mod.GUARD_FAILURE_EXIT_CODE


def test_startup_guard_passes_paper() -> None:
    from app import main as main_mod

    report = main_mod._startup_guard()
    assert report.ok is True


def _run_in_subprocess(env_overrides: dict[str, str], code: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": str(PROJECT_ROOT),
        "TRADING_MODE": "paper",
        "ENABLE_LIVE_TRADING": "false",
        "DATABASE_URL": f"sqlite:///{(PROJECT_ROOT / 'data' / 'subprocess_check.db').as_posix()}",
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_process_exits_with_code_2_when_mode_is_live() -> None:
    """`uvicorn app.main:app` imports the module: it must not boot into 'live'."""
    result = _run_in_subprocess({"TRADING_MODE": "live"}, "import app.main")
    assert result.returncode == GUARD_EXIT_CODE, result.stderr
    assert "FATAL" in result.stderr


def test_process_exits_with_code_2_when_live_flag_is_set() -> None:
    result = _run_in_subprocess({"ENABLE_LIVE_TRADING": "true"}, "import app.main")
    assert result.returncode == GUARD_EXIT_CODE, result.stderr
    assert "FATAL" in result.stderr


def test_process_boots_normally_in_paper_mode() -> None:
    result = _run_in_subprocess({}, "import app.main; print('BOOTED')")
    assert result.returncode == 0, result.stderr
    assert "BOOTED" in result.stdout
