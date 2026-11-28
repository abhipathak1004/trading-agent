"""Research Lab repositories: lifecycle, versioning and the FTS fallback."""

import sqlite3
from pathlib import Path

import pytest

from bq.infra.database import migrate
from bq.research_lab.store import (
    ResearchArtifactStore,
    ResearchLabError,
    ResearchRunStore,
    ResearchSessionStore,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    return path


def test_session_and_run_lifecycle(tmp_path: Path) -> None:
    path = _database(tmp_path)
    sessions = ResearchSessionStore(path)
    session = sessions.create(title="Momentum study")
    assert sessions.get(session.session_id).title == "Momentum study"
    assert [item.session_id for item in sessions.list()] == [session.session_id]

    runs = ResearchRunStore(path)
    parent = runs.create(
        session_id=session.session_id,
        prompt="bench nifty100 momentum",
        model_config={"planner": "reasoning"},
        params={"product": "MIS"},
    )
    child = runs.create(
        session_id=session.session_id,
        prompt="replay with 20 bps fees",
        model_config={},
        params={},
        parent_run_id=parent.run_id,
    )
    assert parent.status == "queued"
    assert child.parent_run_id == parent.run_id
    assert len(runs.list(session_id=session.session_id)) == 2

    completed = runs.update_status(
        parent.run_id, status="completed", metrics={"sharpe": 1.25}, completed=True
    )
    assert completed.status == "completed"
    assert completed.metrics == {"sharpe": 1.25}
    assert completed.completed_at is not None
    assert len(runs.list(status="completed")) == 1


def test_run_status_and_digest_are_validated(tmp_path: Path) -> None:
    path = _database(tmp_path)
    session = ResearchSessionStore(path).create(title="s")
    runs = ResearchRunStore(path)
    with pytest.raises(ResearchLabError):
        runs.create(
            session_id=session.session_id,
            prompt="p",
            model_config={},
            params={},
            status="exploded",
        )
    with pytest.raises(ResearchLabError):
        runs.create(
            session_id=session.session_id,
            prompt="p",
            model_config={},
            params={},
            data_manifest_sha256="too-short",
        )
    with pytest.raises(ResearchLabError):
        runs.get("run_missing")


def test_runs_cannot_be_deleted(tmp_path: Path) -> None:
    path = _database(tmp_path)
    session = ResearchSessionStore(path).create(title="s")
    run = ResearchRunStore(path).create(
        session_id=session.session_id, prompt="p", model_config={}, params={}
    )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM research_runs WHERE run_id = ?", (run.run_id,)
            )


def test_artifact_revisions_link_to_their_parent(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ResearchArtifactStore(path)
    first = store.save(slug="momentum-nifty100", title="v1", body_md="IC 0.03 at 5d")
    second = store.save(slug="momentum-nifty100", title="v2", body_md="IC 0.04 at 5d")
    assert (first.revision, first.parent_revision) == (1, None)
    assert (second.revision, second.parent_revision) == (2, 1)
    assert [item.revision for item in store.revisions("momentum-nifty100")] == [1, 2]
    assert store.latest("momentum-nifty100").artifact_id == second.artifact_id
    assert store.revisions("nope") == ()


def test_artifact_revisions_are_immutable(tmp_path: Path) -> None:
    path = _database(tmp_path)
    artifact = ResearchArtifactStore(path).save(slug="s", title="t", body_md="b")
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE research_artifacts SET title = 'edited' WHERE artifact_id = ?",
                (artifact.artifact_id,),
            )


def test_search_matches_fts_and_fallback_identically(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ResearchArtifactStore(path)
    store.save(slug="a", title="Momentum", body_md="rank IC is stable after costs")
    store.save(slug="b", title="Value", body_md="no momentum jargon here")
    assert store.fts_available is True

    fallback = ResearchArtifactStore(path, fts=False)
    assert fallback.fts_available is False

    for query in ("costs", "momentum jargon", "stable"):
        indexed = [(item.slug, item.revision) for item in store.search(query)]
        scanned = [(item.slug, item.revision) for item in fallback.search(query)]
        assert indexed == scanned
    assert store.search("absentword") == ()
    with pytest.raises(ResearchLabError):
        store.search("!!!")


def test_revision_diff_reports_both_sides(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ResearchArtifactStore(path)
    store.save(slug="a", title="v1", body_md="line one\nline two")
    store.save(slug="a", title="v2", body_md="line one\nline three")
    diff = store.diff("a", 1, 2)
    assert diff["slug"] == "a"
    assert diff["title_changed"] is True
    assert "line three" in str(diff["unified"])
    with pytest.raises(ResearchLabError):
        store.diff("a", 1, 9)


def test_session_touch_advances_updated_at(tmp_path: Path) -> None:
    path = _database(tmp_path)
    sessions = ResearchSessionStore(path)
    session = sessions.create(title="s")
    touched = sessions.touch(session.session_id)
    assert touched.updated_at >= session.updated_at
