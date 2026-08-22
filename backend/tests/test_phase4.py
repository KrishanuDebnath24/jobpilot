"""Phase 4 tests — orchestrator plumbing.

Pure-unit tests (schedule parsing, graph routing, event/envelope shape, dsn) run
anywhere. The correlation/guard/grouping tests exercise the real repositories layer
against Postgres and skip cleanly when the DB is unreachable.
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jobpilot_core import events, models, repositories
from jobpilot_core.db import session_scope
from jobpilot_core.security import hash_password
from jobpilot_core.settings import get_settings
from sqlalchemy.exc import IntegrityError


def _db_reachable() -> bool:
    url = get_settings().database_url
    hostpart = url.split("@", 1)[-1].split("/", 1)[0]
    host, _, port = hostpart.partition(":")
    try:
        with socket.create_connection((host, int(port or "5432")), timeout=2):
            return True
    except OSError:
        return False


requires_db = pytest.mark.skipif(not _db_reachable(), reason="database not reachable")


# --------------------------------------------------------------------------- #
# Pure unit — no DB, no services
# --------------------------------------------------------------------------- #
def test_schedule_interval_parsing():
    from app.scheduler import schedule_interval

    assert schedule_interval("hourly") == 3600
    assert schedule_interval("daily") == 86400
    assert schedule_interval("15m") == 900
    assert schedule_interval("120") == 120  # plain seconds
    assert schedule_interval("30") == 60  # floored at 60s
    assert schedule_interval(None) is None
    assert schedule_interval("") is None
    assert schedule_interval("manual") is None  # explicit opt-out
    assert schedule_interval("gibberish") is None


def test_graph_routing_sends_failure_to_failed_node():
    from app.orchestrator import _route

    assert _route({"status": "running"}) == "ok"
    assert _route({}) == "ok"
    assert _route({"status": "failed"}) == "failed"


def test_psycopg_dsn_strips_asyncpg():
    from app.orchestrator import _psycopg_dsn

    class S:
        database_url = "postgresql+asyncpg://u:p@h:5432/db"

    assert _psycopg_dsn(S()) == "postgresql://u:p@h:5432/db"


def test_event_envelope_shape():
    ev = events.make_event("scout", events.COMPLETED, pipeline_id="pid-1", counts={"new": 3})
    assert ev["type"] == "scout.completed" and ev["stage"] == "scout"
    assert ev["kind"] == "completed" and ev["pipeline_id"] == "pid-1"
    assert ev["counts"]["new"] == 3 and "ts" in ev


def test_assemble_pipeline_groups_stages_newest_wins():
    pid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    def row(stage, status, mins, out=None):
        r = models.PipelineRun(user_id=uuid.uuid4(), pipeline_id=pid, stage=stage, status=status)
        r.id = uuid.uuid4()
        r.created_at = now + timedelta(minutes=mins)
        r.started_at = r.created_at
        r.finished_at = None
        r.output_ref = out
        r.error = None
        return r

    rows = [
        row("pipeline", "running", 0),
        row("scout", "completed", 1, out='{"new": 5}'),
        row("qualify", "running", 2),
    ]
    g = repositories._assemble_pipeline(pid, rows)
    assert g["status"] == "running"  # umbrella status
    assert set(g["stages"]) == {"scout", "qualify"}
    assert g["stages"]["scout"]["counts"]["new"] == 5


# --------------------------------------------------------------------------- #
# Repository layer against Postgres (skips without a DB)
# --------------------------------------------------------------------------- #
async def _mk_user() -> uuid.UUID:
    async with session_scope() as s:
        return (await repositories.create_user(s, f"p4_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id


@requires_db
async def test_pipeline_correlation_grouping_and_guard():
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        # umbrella + two correlated stage runs
        async with session_scope() as s:
            await repositories.open_pipeline(s, uid, pid)
            await repositories.create_pipeline_run(s, uid, "scout", pipeline_id=pid)
            await repositories.create_pipeline_run(s, uid, "qualify", pipeline_id=pid, status="queued")

        async with session_scope() as s:
            grp = await repositories.get_pipeline(s, uid, pid)
        assert grp["pipeline_id"] == str(pid)
        assert set(grp["stages"]) == {"scout", "qualify"}

        # hard concurrency guard: a 2nd active umbrella for the same user fails
        blocked = False
        try:
            async with session_scope() as s:
                await repositories.open_pipeline(s, uid, uuid.uuid4())
        except IntegrityError:
            blocked = True
        assert blocked

        # active detection + freeing after finish
        async with session_scope() as s:
            active = await repositories.get_active_pipeline(s, uid)
            assert active is not None and active.pipeline_id == pid
            await repositories.finish_pipeline_run(s, uid, active.id, "completed")
        async with session_scope() as s:  # guard now free
            await repositories.open_pipeline(s, uid, uuid.uuid4())
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


@requires_db
async def test_get_stage_status_reconciliation_source_of_truth():
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        async with session_scope() as s:
            run = await repositories.create_pipeline_run(s, uid, "scout", pipeline_id=pid)
            await repositories.finish_pipeline_run(
                s, uid, run.id, "completed", output_ref='{"new": 7}')
        async with session_scope() as s:
            row = await repositories.get_stage_status(s, uid, pid, "scout")
        assert row["status"] == "completed" and row["counts"]["new"] == 7
        # a different stage / pipeline is independent
        async with session_scope() as s:
            assert await repositories.get_stage_status(s, uid, pid, "qualify") is None
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


@requires_db
async def test_reap_stale_pipeline_frees_the_guard():
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        async with session_scope() as s:
            run = await repositories.open_pipeline(s, uid, pid)
            # backdate started_at so it counts as stale
            run.started_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        async with session_scope() as s:
            reaped = await repositories.reap_stale_pipelines(s, uid, ttl_seconds=3600)
        assert reaped == 1
        async with session_scope() as s:  # guard freed -> a fresh open succeeds
            assert await repositories.get_active_pipeline(s, uid) is None
            await repositories.open_pipeline(s, uid, uuid.uuid4())
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


@requires_db
async def test_reap_closes_an_orphaned_stage_row_not_just_the_umbrella():
    """The bug this exists for: only the umbrella was ever reaped, so a stage row left
    'running' by a lost worker stayed that way forever and the UI showed a spinner for a
    task that no longer existed."""
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        old = datetime.now(timezone.utc) - timedelta(seconds=3600)
        async with session_scope() as s:
            umbrella = await repositories.open_pipeline(s, uid, pid)
            umbrella.started_at = old
            run = await repositories.create_pipeline_run(s, uid, "qualify", pipeline_id=pid)
            run.status = "running"
            run.started_at = old
        async with session_scope() as s:
            stages = await repositories.active_run_stages(s, uid)
            assert set(stages) == {"pipeline", "qualify"}
            # Nothing is beating, so both are dead.
            reaped = await repositories.reap_stale_runs(s, uid, alive=set(), grace_seconds=120)
        assert reaped == 2
        async with session_scope() as s:
            rows = await repositories.get_pipeline_runs(s, uid, stage="qualify")
            assert rows[0]["status"] == "failed" and "stopped reporting" in (rows[0]["error"] or "")
            assert await repositories.get_active_pipeline(s, uid) is None
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


@requires_db
async def test_reap_never_touches_a_stage_that_is_still_beating():
    """The dangerous direction. Reaping a live run is far worse than showing a stale one,
    so a stage that still reports must survive however long it has been going."""
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        old = datetime.now(timezone.utc) - timedelta(seconds=7200)
        async with session_scope() as s:
            umbrella = await repositories.open_pipeline(s, uid, pid)
            umbrella.started_at = old
            run = await repositories.create_pipeline_run(s, uid, "qualify", pipeline_id=pid)
            run.status = "running"
            run.started_at = old
        async with session_scope() as s:
            reaped = await repositories.reap_stale_runs(
                s, uid, alive={"qualify", "pipeline"}, grace_seconds=120)
        assert reaped == 0
        async with session_scope() as s:
            assert await repositories.get_active_pipeline(s, uid) is not None
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


@requires_db
async def test_reap_spares_a_run_still_inside_its_grace_period():
    """A row is opened before its first beat; reaping in that window would kill every run
    at the moment it starts."""
    uid = await _mk_user()
    try:
        pid = uuid.uuid4()
        async with session_scope() as s:
            await repositories.open_pipeline(s, uid, pid)  # started_at = now
        async with session_scope() as s:
            assert await repositories.reap_stale_runs(
                s, uid, alive=set(), grace_seconds=120) == 0
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)


async def test_alive_stages_reports_everything_alive_when_redis_is_down():
    """Fail-safe direction: an unreadable heartbeat must never be read as "they all died",
    or one broker hiccup would mass-reap every healthy run."""
    from jobpilot_core import events

    alive = await events.alive_stages(
        "redis://127.0.0.1:6390/0", uuid.uuid4(), ["scout", "qualify"], stale_seconds=600)
    assert alive == {"scout", "qualify"}


@requires_db
async def test_list_scheduled_users_is_a_system_read():
    uid = await _mk_user()
    try:
        async with session_scope() as s:
            await repositories.upsert_search_config(s, uid, {"schedule": "hourly"})
        async with session_scope() as s:
            users = await repositories.list_scheduled_users(s)
        mine = [u for u in users if u["user_id"] == str(uid)]
        assert len(mine) == 1 and mine[0]["schedule"] == "hourly"
    finally:
        async with session_scope() as s:
            await repositories.delete_user_cascade(s, uid)
