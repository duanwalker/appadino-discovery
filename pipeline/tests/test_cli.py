"""CLI argument-parsing tests for the `filter` and `score` subcommands' --eins overrides
(`filter`: G1.3 Stage 2 review follow-up; `score`: bounded internal-QA tenant run).
`run_filter_and_signals` / `run_scoring` are monkeypatched so this only exercises
cli.py's own argparse wiring and EIN-list parsing, not the orchestrators themselves
(covered separately in test_run_filter_and_signals.py / test_run_scoring.py).
"""

from __future__ import annotations

from typing import Any, Self

import pytest

from discovery import cli
from discovery.stages import filter as filter_stage
from discovery.stages import run_filter_and_signals, run_scoring


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


# --- `score --eins` (bounded, known-cost scoring run; e.g. the internal-QA tenant) ---

def _capture_run_scoring(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run_scoring(client_id: int, eins: list[str], **kwargs: Any) -> dict[str, Any]:
        captured["client_id"] = client_id
        captured["eins"] = eins
        captured["kwargs"] = kwargs
        return {}

    monkeypatch.setattr(run_scoring, "run_scoring", fake_run_scoring)
    return captured


def _forbid_survivor_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("score --eins must not consult the DB or select_survivor_eins")

    monkeypatch.setattr(filter_stage, "select_survivor_eins", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)


def test_score_eins_flag_parses_strips_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_scoring(monkeypatch)
    _forbid_survivor_selection(monkeypatch)

    exit_code = cli.main(["score", "3", "--eins", " 010343943, 010322532 ,,010343943,"])

    assert exit_code == 0
    assert captured["client_id"] == 3
    assert captured["eins"] == ["010343943", "010322532"]
    assert captured["kwargs"]["eins_overridden"] is True


def test_score_eins_override_bypasses_select_survivor_eins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the flag: client 3's permissive ICP would otherwise send every
    signaled org through Haiku. With --eins, survivor selection never runs."""
    captured = _capture_run_scoring(monkeypatch)
    _forbid_survivor_selection(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)  # not even needed by the CLI itself

    cli.main(["score", "3", "--eins", "010343943", "--haiku-cut-n", "8"])

    assert captured["eins"] == ["010343943"]
    assert captured["kwargs"] == {"haiku_cut_n": 8, "eins_overridden": True}


@pytest.mark.parametrize(
    "raw",
    ["12345678", "1234567890", "01-0343943", "01034394a", "010343943,abc", "٠١٠٣٤٣٩٤٣", "010343943;010322532"],
    ids=["8-digits", "10-digits", "dash", "letter", "one-bad-of-two", "non-ascii-digits", "wrong-separator"],
)
def test_score_eins_rejects_malformed_ein(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    captured = _capture_run_scoring(monkeypatch)
    _forbid_survivor_selection(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["score", "3", "--eins", raw])

    assert excinfo.value.code == 2  # argparse usage error
    assert captured == {}  # run_scoring never called — nothing spent


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,"], ids=["empty", "space", "comma", "commas-and-spaces"])
def test_score_eins_rejects_empty_list(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    captured = _capture_run_scoring(monkeypatch)
    _forbid_survivor_selection(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["score", "3", "--eins", raw])

    assert excinfo.value.code == 2
    assert captured == {}


def test_score_without_eins_flag_uses_select_survivor_eins_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run_scoring(monkeypatch)
    seen: dict[str, Any] = {}

    class _Conn:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_connect(url: str) -> _Conn:
        seen["url"] = url
        return _Conn()

    def fake_select(conn: object, client_id: int) -> list[str]:
        seen["client_id"] = client_id
        return ["111111111", "222222222"]

    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    monkeypatch.setattr("psycopg.connect", fake_connect)
    monkeypatch.setattr(filter_stage, "select_survivor_eins", fake_select)

    cli.main(["score", "2"])

    assert seen == {"url": "postgresql://example/db", "client_id": 2}
    assert captured["eins"] == ["111111111", "222222222"]
    assert captured["kwargs"] == {"haiku_cut_n": None}  # same call shape as before the flag existed


def test_parse_ein_list_preserves_first_seen_order() -> None:
    assert cli.parse_ein_list("333333333,111111111,333333333,222222222") == ["333333333", "111111111", "222222222"]
