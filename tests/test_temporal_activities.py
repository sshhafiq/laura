"""Temporal activities, exercised directly (no server needed)."""

import asyncio

from laura.demo import seed_demo
from laura.ingest.github_sync import FakeGitHubTransport
from laura.store import LauraStore
from laura.temporal.activities import LauraActivities
from laura.temporal.models import (RepoSyncInput, RollupInput,
                                   ScoreBatchInput)
from laura.temporal.worker import demo_transport


def _db(tmp_path):
    path = str(tmp_path / "laura.db")
    store = LauraStore(path)
    summary = seed_demo(store)
    store.close()
    return path, summary["org_id"]


def test_sync_activity_ingests_demo_transport(tmp_path):
    path, org_id = _db(tmp_path)
    activities = LauraActivities(transport=demo_transport())
    store = LauraStore(path)
    repo = store.get_repo_by_name(org_id, "acme/api")
    store.close()
    result = asyncio.run(activities.sync_repo_activity(RepoSyncInput(
        db_path=path, org_id=org_id, repo_id=repo.id,
        repo_full_name="acme/api")))
    assert result.pull_requests == 2 and result.reviews == 2
    store = LauraStore(path)
    assert store.find_pull_request(org_id, repo.id, 900) is not None
    store.close()


def test_scoring_pipeline_activities(tmp_path):
    path, org_id = _db(tmp_path)
    activities = LauraActivities(transport=FakeGitHubTransport())
    head = asyncio.run(activities.unscored_ids_activity(
        RollupInput(db_path=path, org_id=org_id)))
    # demo PRs arrive scored; wipe two scores to simulate unscored work
    store = LauraStore(path)
    prs = store.list_pull_requests(org_id)[:2]
    store.conn.execute(
        "UPDATE pull_requests SET score_hours = NULL WHERE id IN (?, ?)",
        (prs[0].id, prs[1].id))
    store.conn.commit()
    store.close()
    head = asyncio.run(activities.unscored_ids_activity(
        RollupInput(db_path=path, org_id=org_id)))
    assert sorted(head.pr_ids) == sorted([prs[0].id, prs[1].id])
    batch = asyncio.run(activities.score_batch_activity(ScoreBatchInput(
        db_path=path, org_id=org_id, pr_ids=head.pr_ids,
        multiplier=head.multiplier)))
    assert batch.scored == 2 and batch.total_hours > 0
    rollup = asyncio.run(activities.rollup_activity(
        RollupInput(db_path=path, org_id=org_id)))
    assert rollup.scored_prs_total >= 20 and rollup.laura_hours_total > 0


def test_sync_activity_requires_transport(tmp_path):
    path, org_id = _db(tmp_path)
    activities = LauraActivities()
    try:
        asyncio.run(activities.sync_repo_activity(RepoSyncInput(
            db_path=path, org_id=org_id, repo_id=1,
            repo_full_name="acme/api")))
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "transport" in str(exc)
