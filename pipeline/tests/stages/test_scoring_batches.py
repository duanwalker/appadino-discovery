"""Stage 3 at national scale: Haiku/Sonnet passes split into Message Batches under the
API's per-batch limits, a global Haiku-to-Sonnet cut across all chunks, batch ids
persisted to runs.counts after every submit, and `--resume-run` collecting recorded
batches instead of paying for them again.

Fake Anthropic client and fake `runs` table only: no API calls, no database. The
fake DB can "kill" the process (a BaseException, like SIGKILL: no `except Exception`
handler or `finally` write gets through) right after the Nth counts write, which is
how a job dying mid-run looks from the database's side.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self

import pytest

from discovery import cli
from discovery.stages import run_scoring as run_scoring_module
from discovery.stages.run_scoring import ResumeRunError, UnfinishedScoringRunError, run_scoring
from discovery.stages.score import (
    HAIKU_MODEL,
    build_haiku_request,
    chunk_requests,
    estimate_request_bytes,
)

CLIENT_ID = 2


class Killed(BaseException):
    """The job process dying: nothing after this point runs, including `finally`."""


# --- Fake Anthropic Message Batches -------------------------------------------------


class FakeAnthropicWorld:
    """Batches survive across "processes" (clients), like the real API's 29-day retention."""

    def __init__(self, pre_scores: dict[str, int]) -> None:
        self.pre_scores = pre_scores
        self.batches: dict[str, list[dict[str, Any]]] = {}
        self.create_calls: list[list[str]] = []
        self.retrieve_calls: list[str] = []

    def client(self) -> SimpleNamespace:
        return SimpleNamespace(messages=SimpleNamespace(batches=_FakeBatches(self)))

    def submitted(self, model: str) -> list[str]:
        return [
            r["custom_id"]
            for requests in self.batches.values()
            for r in requests
            if r["params"]["model"] == model
        ]


class _FakeBatches:
    def __init__(self, world: FakeAnthropicWorld) -> None:
        self.world = world

    def create(self, requests: list[dict[str, Any]]) -> SimpleNamespace:
        batch_id = f"batch-{len(self.world.batches) + 1}"
        self.world.batches[batch_id] = list(requests)
        self.world.create_calls.append([r["custom_id"] for r in requests])
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self.world.retrieve_calls.append(batch_id)
        n = len(self.world.batches[batch_id])
        return SimpleNamespace(
            id=batch_id, processing_status="ended", request_counts=SimpleNamespace(succeeded=n, errored=0)
        )

    def results(self, batch_id: str) -> list[SimpleNamespace]:
        out = []
        for request in self.world.batches[batch_id]:
            ein = request["custom_id"]
            if request["params"]["model"] == HAIKU_MODEL:
                body = {"pre_score": self.world.pre_scores[ein], "obvious_disqualifier": False, "disqualifier_reason": None}
            else:
                body = {"values_signals": {}, "alignment_criteria": {}, "disqualified": False, "dq_reason": None}
            message = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))])
            out.append(SimpleNamespace(custom_id=ein, result=SimpleNamespace(type="succeeded", message=message)))
        return out


# --- Fake runs table ----------------------------------------------------------------


class FakeDB:
    def __init__(self, kill_after_counts_writes: int | None = None) -> None:
        self.runs: dict[int, dict[str, Any]] = {}
        self.counts_writes: list[dict[str, Any]] = []
        self.kill_after_counts_writes = kill_after_counts_writes
        self.kill_on_sonnet_insert = False
        self.dead = False
        self.scores: list[dict[str, Any]] = []

    def revive(self) -> None:
        """A new job execution: same database, fresh process."""
        self.dead = False
        self.kill_after_counts_writes = None
        self.kill_on_sonnet_insert = False


class _Cursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._one: tuple[Any, ...] | None = None
        self._all: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        db = self.db
        if db.dead:
            raise Killed
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO runs"):
            run_id = len(db.runs) + 1
            db.runs[run_id] = {
                "client_id": params[0],
                "stage": "scoring",
                "status": "running",
                "started_at": params[1],
                "counts": None,
            }
            self._one = (run_id,)
        elif s.startswith("SELECT version FROM icp_configs"):
            self._one = (1,)
        elif s.startswith("SELECT id, status, started_at FROM runs"):
            self._all = [
                (run_id, r["status"], r["started_at"])
                for run_id, r in db.runs.items()
                if r["client_id"] == params[0]
                and r["status"] in ("running", "failed")
                and r["counts"]
                and "batches" in r["counts"]
            ]
        elif s.startswith("SELECT client_id, stage, status, counts FROM runs"):
            r = db.runs.get(params[0])
            self._one = None if r is None else (r["client_id"], r["stage"], r["status"], copy.deepcopy(r["counts"]))
        elif s.startswith("UPDATE runs SET counts ="):
            stored = json.loads(json.dumps(params[0].obj))
            db.runs[params[1]]["counts"] = stored
            db.counts_writes.append(stored)
            if db.kill_after_counts_writes is not None and len(db.counts_writes) >= db.kill_after_counts_writes:
                db.dead = True
                raise Killed
        elif s.startswith("UPDATE runs SET status = 'running'"):
            db.runs[params[0]]["status"] = "running"
        elif s.startswith("UPDATE runs SET finished_at"):
            _finished_at, status, counts, error, run_id = params
            db.runs[run_id].update(status=status, counts=json.loads(json.dumps(counts.obj)), error=error)
        else:
            raise AssertionError(f"unexpected SQL: {s[:80]}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._all

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def cursor(self) -> _Cursor:
        return _Cursor(self.db)

    def commit(self) -> None:
        if self.db.dead:
            raise Killed

    def __enter__(self) -> Self:
        if self.db.dead:
            raise Killed
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# --- Wiring -------------------------------------------------------------------------


def _org(ein: str) -> dict[str, Any]:
    return {"name": f"Org {ein}", "city": "Portland", "state": "ME", "mission_text": "Help.", "gov_funding_pct": 0.1}


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    db: FakeDB,
    world: FakeAnthropicWorld,
    survivors: list[str] | None = None,
    chunk_size: int = 2,
) -> None:
    monkeypatch.setattr("psycopg.connect", lambda url: _Conn(db))
    monkeypatch.setattr("anthropic.Anthropic", world.client)
    monkeypatch.setattr(run_scoring_module, "BATCH_MAX_REQUESTS", chunk_size)
    monkeypatch.setattr(run_scoring_module, "get_active_icp_config", lambda conn, client_id: {})
    monkeypatch.setattr(run_scoring_module, "_load_scoreable_orgs", lambda conn, eins: {e: _org(e) for e in eins})
    monkeypatch.setattr(
        run_scoring_module, "enforce_hard_rules", lambda vs, criteria, org_context: (vs, criteria, [])
    )

    def select_survivors(conn: object, client_id: int) -> list[str]:
        if survivors is None:
            raise AssertionError("an --eins run must never re-select survivors")
        return list(survivors)

    monkeypatch.setattr(run_scoring_module, "select_survivor_eins", select_survivors)

    def insert_score(conn: object, row: dict[str, Any]) -> None:
        if db.kill_on_sonnet_insert and row["stage"] == "sonnet":
            db.dead = True
            raise Killed
        db.scores.append(row)

    monkeypatch.setattr(run_scoring_module, "_insert_score", insert_score)


EINS = [f"{i:09d}" for i in range(1, 7)]  # 6 orgs -> 3 Haiku batches of 2
# Winners deliberately sit in the first and last chunk.
PRE_SCORES = {EINS[0]: 95, EINS[1]: 10, EINS[2]: 20, EINS[3]: 30, EINS[4]: 40, EINS[5]: 90}


def _score(**kwargs: Any) -> dict[str, Any]:
    return run_scoring(CLIENT_ID, database_url="postgresql://fake", poll_interval=0, **kwargs)


# --- Chunking -----------------------------------------------------------------------


def _haiku(ein: str, mission: str = "Help.") -> Any:
    return build_haiku_request(ein, {**_org(ein), "mission_text": mission}, ["youth"])


def test_chunk_requests_caps_request_count() -> None:
    requests = [_haiku(e) for e in EINS[:5]]
    chunks = chunk_requests(requests, max_requests=2, max_bytes=10**9)
    assert [[r["custom_id"] for r in c] for c in chunks] == [EINS[0:2], EINS[2:4], EINS[4:5]]


def test_chunk_requests_caps_estimated_bytes_whichever_comes_first() -> None:
    requests = [_haiku(e) for e in EINS[:4]]
    size = max(estimate_request_bytes(r) for r in requests)
    chunks = chunk_requests(requests, max_requests=100, max_bytes=size * 2 + 1)
    assert [len(c) for c in chunks] == [2, 2]
    assert all(sum(estimate_request_bytes(r) for r in c) <= size * 2 + 1 for c in chunks)


def test_chunk_requests_rejects_a_single_request_over_the_byte_cap() -> None:
    with pytest.raises(ValueError, match="over the"):
        chunk_requests([_haiku(EINS[0], mission="x" * 5000)], max_requests=10, max_bytes=1000)


def test_limits_are_well_under_the_api_caps() -> None:
    from discovery.stages import score

    api_max_requests, api_max_bytes = 100_000, 256 * 1024 * 1024
    assert score.BATCH_MAX_REQUESTS == 20_000
    assert score.BATCH_MAX_BYTES == 100 * 1024 * 1024
    assert score.BATCH_MAX_REQUESTS < api_max_requests and score.BATCH_MAX_BYTES < api_max_bytes


# --- Multi-chunk run + global cut ---------------------------------------------------


def test_haiku_is_split_into_chunks_and_the_cut_is_global(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)

    counts = _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)

    assert world.create_calls[:3] == [EINS[0:2], EINS[2:4], EINS[4:6]]  # three Haiku batches
    # Ranked across all chunks: the winners come from the first and the last chunk.
    assert world.create_calls[3] == [EINS[0], EINS[5]]
    assert len(world.create_calls) == 4
    assert counts["sonnet_candidates"] == 2
    assert counts["batches"] == {"haiku": ["batch-1", "batch-2", "batch-3"], "sonnet": ["batch-4"]}
    assert db.runs[1]["status"] == "success"
    assert db.runs[1]["counts"]["batches"] == counts["batches"]


def test_each_batch_id_is_persisted_right_after_its_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)

    _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)

    recorded = [w["batches"] for w in db.counts_writes]
    assert recorded == [
        {"haiku": ["batch-1"]},
        {"haiku": ["batch-1", "batch-2"]},
        {"haiku": ["batch-1", "batch-2", "batch-3"]},
        {"haiku": ["batch-1", "batch-2", "batch-3"], "sonnet": ["batch-4"]},
    ]
    # The resume inputs are in the very first write, before any later batch exists.
    assert db.counts_writes[0]["icp_version"] == 1
    assert db.counts_writes[0]["haiku_cut_n"] == 2
    assert db.counts_writes[0]["eins"] == EINS


# --- Kill after the second submit, then resume ---------------------------------------


def test_kill_after_second_submit_then_resume_never_resubmits(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(kill_after_counts_writes=2), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)

    with pytest.raises(Killed):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)

    assert world.create_calls == [EINS[0:2], EINS[2:4]]  # two batches paid for
    assert db.runs[1]["status"] == "running"  # the process died; nothing marked it
    assert db.runs[1]["counts"]["batches"] == {"haiku": ["batch-1", "batch-2"]}

    db.revive()
    counts = _score(eins=None, resume_run_id=1)

    # Only the third Haiku chunk and the Sonnet pass are new; batches 1-2 were collected.
    assert world.create_calls[2:] == [EINS[4:6], [EINS[0], EINS[5]]]
    haiku_submitted = world.submitted(HAIKU_MODEL)
    assert sorted(haiku_submitted) == EINS and len(haiku_submitted) == len(set(haiku_submitted))
    assert {"batch-1", "batch-2"} <= set(world.retrieve_calls)
    assert counts["batches"] == {"haiku": ["batch-1", "batch-2", "batch-3"], "sonnet": ["batch-4"]}
    assert counts["resumed_times"] == 1
    assert db.runs[1]["status"] == "success"
    assert len(db.runs) == 1  # resumed on the same runs row


def test_resume_with_every_batch_recorded_submits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(), FakeAnthropicWorld(PRE_SCORES)
    db.kill_on_sonnet_insert = True  # dies after both passes were submitted and recorded
    _wire(monkeypatch, db, world)

    with pytest.raises(Killed):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)
    creates_before = len(world.create_calls)
    assert creates_before == 4

    db.revive()
    counts = _score(eins=None, resume_run_id=1)

    assert len(world.create_calls) == creates_before  # nothing new paid for
    assert counts["sonnet_succeeded"] == 2
    assert db.runs[1]["status"] == "success"


def test_resume_of_an_ordinary_run_reselects_survivors(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(kill_after_counts_writes=1), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world, survivors=EINS)

    with pytest.raises(Killed):
        _score(eins=EINS, haiku_cut_n=2)
    assert "eins" not in db.runs[1]["counts"]  # ordinary runs don't store the survivor list

    db.revive()
    counts = _score(eins=None, resume_run_id=1)

    assert sorted(world.submitted(HAIKU_MODEL)) == EINS
    assert counts["sonnet_candidates"] == 2


# --- A plain rerun must not pay twice -----------------------------------------------


def test_plain_score_refuses_while_an_unfinished_run_has_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(kill_after_counts_writes=2), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)
    with pytest.raises(Killed):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)
    db.revive()
    creates_before = len(world.create_calls)

    with pytest.raises(UnfinishedScoringRunError, match="--resume-run 1"):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)

    assert len(world.create_calls) == creates_before
    assert len(db.runs) == 1  # no second runs row


def test_cli_prints_the_resume_command_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db, world = FakeDB(kill_after_counts_writes=1), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)
    with pytest.raises(Killed):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)
    db.revive()
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

    exit_code = cli.main(["score", str(CLIENT_ID), "--eins", ",".join(EINS)])

    assert exit_code == 2
    assert f"discovery score {CLIENT_ID} --resume-run 1" in capsys.readouterr().err
    assert len(world.create_calls) == 1


def test_a_failed_run_with_no_batches_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(), FakeAnthropicWorld(PRE_SCORES)
    db.runs[1] = {"client_id": CLIENT_ID, "stage": "scoring", "status": "failed", "started_at": datetime.now(UTC), "counts": {"eins_requested": 6}}
    _wire(monkeypatch, db, world)

    _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)

    assert db.runs[2]["status"] == "success"


# --- Resume validation ---------------------------------------------------------------


def test_resume_refuses_a_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)
    _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)
    creates_before = len(world.create_calls)

    with pytest.raises(ResumeRunError, match="already finished"):
        _score(eins=None, resume_run_id=1)
    assert len(world.create_calls) == creates_before


def test_resume_refuses_another_clients_run(monkeypatch: pytest.MonkeyPatch) -> None:
    db, world = FakeDB(kill_after_counts_writes=1), FakeAnthropicWorld(PRE_SCORES)
    _wire(monkeypatch, db, world)
    with pytest.raises(Killed):
        _score(eins=EINS, eins_overridden=True, haiku_cut_n=2)
    db.revive()

    with pytest.raises(ResumeRunError, match="not scoring for client_id=3"):
        run_scoring(3, None, database_url="postgresql://fake", resume_run_id=1, poll_interval=0)


@pytest.mark.parametrize("extra", [["--eins", "000000001"], ["--haiku-cut-n", "5"]])
def test_cli_rejects_resume_with_eins_or_cut(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["score", "2", "--resume-run", "1", *extra])


def test_cli_passes_resume_run_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run_scoring(client_id: int, eins: list[str] | None, **kwargs: Any) -> dict[str, Any]:
        seen.update(client_id=client_id, eins=eins, kwargs=kwargs)
        return {}

    monkeypatch.setattr(run_scoring_module, "run_scoring", fake_run_scoring)
    monkeypatch.setattr("psycopg.connect", lambda url: (_ for _ in ()).throw(AssertionError("no DB from the CLI")))

    assert cli.main(["score", "2", "--resume-run", "17"]) == 0
    assert seen == {"client_id": 2, "eins": None, "kwargs": {"resume_run_id": 17}}
