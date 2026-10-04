"""CLI argument-parsing tests for the `filter` subcommand's --eins override (G1.3
Stage 2 review follow-up). `run_filter_and_signals` is monkeypatched so this only
exercises cli.py's own argparse wiring and comma-list parsing, not the orchestrator
itself (covered separately in test_run_filter_and_signals.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from discovery import cli
from discovery.stages import run_filter_and_signals


def _capture_run_filter_and_signals(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run_filter_and_signals(client_id: int, **kwargs: Any) -> dict[str, Any]:
        captured["client_id"] = client_id
        captured["kwargs"] = kwargs
        return {}

    monkeypatch.setattr(run_filter_and_signals, "run_filter_and_signals", fake_run_filter_and_signals)
    return captured


def test_eins_flag_parses_comma_separated_list_and_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_filter_and_signals(monkeypatch)

    exit_code = cli.main(["filter", "2", "--eins", " 111111111, 222222222 ,333333333"])

    assert exit_code == 0
    assert captured["client_id"] == 2
    assert captured["kwargs"]["override_eins"] == ["111111111", "222222222", "333333333"]


def test_eins_flag_drops_empty_segments_from_stray_commas(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_filter_and_signals(monkeypatch)

    cli.main(["filter", "2", "--eins", "111111111,,222222222,"])

    assert captured["kwargs"]["override_eins"] == ["111111111", "222222222"]


def test_without_eins_flag_override_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_filter_and_signals(monkeypatch)

    cli.main(["filter", "2"])

    assert captured["kwargs"]["override_eins"] is None


def test_limit_eins_and_force_refresh_flags_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_filter_and_signals(monkeypatch)

    cli.main(["filter", "2", "--limit-eins", "5000", "--force-refresh-signals"])

    assert captured["kwargs"]["limit_eins"] == 5000
    assert captured["kwargs"]["force_refresh_signals"] is True
