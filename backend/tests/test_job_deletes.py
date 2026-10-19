"""Bulk job deletes — which jobs each one actually resolves to.

The Concierge now offers four deletes, and the failure they share is silent: the tool
reports a count, the user reads a number, and nobody notices it took the wrong bucket
until the pool is short a role they were mid-application on. Two of them resolve their
target in SQL, so that is what these tests pin.

  * ``unscored`` and ``disqualified`` are complements, not overlapping filters. A job
    with no qualification row is unscored; a job with a *disqualified* verdict is scored.
    Reading either as "the ones that didn't work out" would let one delete take the
    other's jobs, and a disqualified job is one a re-tuned threshold can bring back.
  * ``all_job_ids`` is the only delete with no filter to hide behind, so its user scoping
    is the whole safety story.

Skips cleanly without Postgres.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import _db_reachable

requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


async def _mk_user(session):
    import jobpilot_core.repositories as R
    from jobpilot_core.security import hash_password

    user = await R.create_user(session, f"del_{uuid.uuid4().hex}@ex.com", hash_password("x"))
    return user.id


async def _mk_job(session, uid, *, title="Backend Eng"):
    import jobpilot_core.repositories as R
    from jobpilot_core.schemas import JobIn
    from jobpilot_core.scout import normalize_raw

    raw = normalize_raw({
        "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
        "company": "Acme", "title": title, "location": "Dublin",
        "url": f"https://x/{uuid.uuid4().hex}",
        "description": "Go, k8s", "description_completeness": "full",
    })
    jobs = await R.upsert_jobs(session, uid, [JobIn.model_validate(raw.model_dump())])
    return jobs[0].id


async def _score(session, uid, jid, decision):
    import jobpilot_core.repositories as R

    await R.upsert_qualification(session, uid, jid, 0.7, decision, {}, [], [])


@requires_db
async def test_unscored_and_disqualified_never_take_each_others_jobs():
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        qualified = await _mk_job(s, uid, title="Qualified Eng")
        rejected = await _mk_job(s, uid, title="Rejected Eng")
        never = await _mk_job(s, uid, title="Never Scored Eng")
        await _score(s, uid, qualified, "qualified")
        await _score(s, uid, rejected, "disqualified")
    try:
        async with session_scope() as s:
            assert await R.unscored_job_ids(s, uid) == [never]
            assert await R.disqualified_job_ids(s, uid) == [rejected]
            # The two buckets plus the qualified one account for the whole pool, exactly once.
            assert sorted(map(str, await R.all_job_ids(s, uid))) == sorted(
                map(str, [qualified, rejected, never]))

        async with session_scope() as s:
            assert await R.delete_jobs(s, uid, await R.unscored_job_ids(s, uid)) == 1
        async with session_scope() as s:
            # Deleting the unscored bucket leaves both verdicts standing — a disqualified
            # job survives to be re-qualified after a threshold change.
            assert await R.unscored_job_ids(s, uid) == []
            assert await R.disqualified_job_ids(s, uid) == [rejected]
            assert len(await R.all_job_ids(s, uid)) == 2
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_a_scored_job_is_never_unscored_even_with_no_verdict():
    """Qualify writes a row before it has a decision (score-only, decision NULL). That job
    has been looked at, so 'delete the ones that were never scored' must leave it alone —
    the filter is the presence of the row, not the value in it."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        pending = await _mk_job(s, uid)
        await R.upsert_qualification(s, uid, pending, 0.5, None, {}, [], [])
    try:
        async with session_scope() as s:
            assert await R.unscored_job_ids(s, uid) == []
            assert await R.disqualified_job_ids(s, uid) == []
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_emptying_one_pool_leaves_another_users_alone():
    """all_job_ids has no filter to hide a scoping bug behind: get the WHERE wrong and
    'clear my pool' clears everyone's."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        mine = await _mk_user(s)
        theirs = await _mk_user(s)
        my_job = await _mk_job(s, mine)
        their_job = await _mk_job(s, theirs)
    try:
        async with session_scope() as s:
            assert await R.all_job_ids(s, mine) == [my_job]
            assert await R.delete_jobs(s, mine, await R.all_job_ids(s, mine)) == 1
        async with session_scope() as s:
            assert await R.all_job_ids(s, mine) == []
            assert await R.all_job_ids(s, theirs) == [their_job]
            # ...and a delete can't be aimed across the boundary by passing an id.
            assert await R.delete_jobs(s, mine, [their_job]) == 0
        async with session_scope() as s:
            assert await R.all_job_ids(s, theirs) == [their_job]
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, mine)
            await R.delete_user_cascade(s, theirs)
