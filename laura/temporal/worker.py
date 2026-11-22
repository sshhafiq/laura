"""Worker + demo client for Laura's Temporal workflows.

Run a local Temporal server first (from the repo root)::

    docker compose up -d          # temporalio/auto-setup on :7233

then, in one terminal::

    python -m laura.temporal.worker worker --db laura.db

and in another::

    python -m laura.temporal.worker demo --db laura.db

The demo seeds the DemoCo org (if absent), registers a fake GitHub
transport carrying two extra PRs, runs ``RepoSyncWorkflow`` to ingest
them, then ``ScoringPipelineWorkflow`` to score everything unscored,
and prints both results — the full durable pipeline with no GitHub
account and no API keys.
"""

from __future__ import annotations

import argparse
import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from laura.ingest.github_sync import FakeGitHubTransport
from laura.temporal.activities import LauraActivities
from laura.temporal.models import OrgSyncInput, ScoringPipelineInput
from laura.temporal.workflows import RepoSyncWorkflow, ScoringPipelineWorkflow

TASK_QUEUE = "laura"


def demo_transport() -> FakeGitHubTransport:
    transport = FakeGitHubTransport()
    transport.add_repo("acme/api", prs=[
        {
            "number": 900, "title": "Add request tracing middleware",
            "body": "Adds OpenTelemetry-style trace ids to every request.",
            "state": "closed", "merged_at": "2026-11-18T15:00:00Z",
            "created_at": "2026-11-17T09:00:00Z",
            "closed_at": "2026-11-18T15:00:00Z",
            "user": {"login": "ada"}, "head": {"ref": "feat/tracing"},
            "base": {"ref": "main"}, "additions": 210, "deletions": 40,
            "changed_files": 3,
            "files": [
                {"filename": "src/core/tracing.py", "additions": 150,
                 "deletions": 10},
                {"filename": "src/api/app.py", "additions": 40,
                 "deletions": 25},
                {"filename": "tests/test_tracing.py", "additions": 20,
                 "deletions": 5},
            ],
        },
        {
            "number": 901, "title": "Fix off-by-one in pagination",
            "body": "", "state": "closed",
            "merged_at": "2026-11-19T11:00:00Z",
            "created_at": "2026-11-19T09:30:00Z",
            "closed_at": "2026-11-19T11:00:00Z",
            "user": {"login": "ben"}, "head": {"ref": "fix/pagination"},
            "base": {"ref": "main"}, "additions": 12, "deletions": 9,
            "changed_files": 1,
            "files": [{"filename": "src/api/search.py", "additions": 12,
                       "deletions": 9}],
        },
    ], reviews={
        900: [{"user": {"login": "cy"}, "state": "approved",
               "submitted_at": "2026-11-18T10:00:00Z",
               "body": "Clean, well tested.", "comments_count": 2}],
        901: [{"user": {"login": "ada"}, "state": "approved",
               "submitted_at": "2026-11-19T10:00:00Z", "body": "",
               "comments_count": 0}],
    }, commits={
        900: [{"sha": "t900a", "author": {"login": "ada"},
               "commit": {"author": {"date": "2026-11-17T08:00:00Z"},
                          "message": "wip tracing"}}],
        901: [{"sha": "t901a", "author": {"login": "ben"},
               "commit": {"author": {"date": "2026-11-19T09:00:00Z"},
                          "message": "fix pagination"}}],
    })
    return transport


async def _run_worker(db_path: str) -> None:
    client = await Client.connect("localhost:7233")
    activities = LauraActivities(transport=demo_transport())
    worker = Worker(
        client, task_queue=TASK_QUEUE,
        workflows=[RepoSyncWorkflow, ScoringPipelineWorkflow],
        activities=[activities.sync_repo_activity,
                    activities.list_repos_activity,
                    activities.unscored_ids_activity,
                    activities.score_batch_activity,
                    activities.rollup_activity])
    await worker.run()


async def _run_demo(db_path: str) -> None:
    from laura.demo import seed_demo
    from laura.store import LauraStore

    store = LauraStore(db_path)
    summary = seed_demo(store)
    org_id = summary["org_id"]
    store.close()

    client = await Client.connect("localhost:7233")
    activities = LauraActivities(transport=demo_transport())
    async with Worker(
            client, task_queue=TASK_QUEUE,
            workflows=[RepoSyncWorkflow, ScoringPipelineWorkflow],
            activities=[activities.sync_repo_activity,
                        activities.list_repos_activity,
                        activities.unscored_ids_activity,
                        activities.score_batch_activity,
                        activities.rollup_activity]):
        sync = await client.execute_workflow(
            RepoSyncWorkflow.run, OrgSyncInput(db_path=db_path,
                                               org_id=org_id),
            id="laura-demo-sync", task_queue=TASK_QUEUE)
        print("sync:", sync)
        scored = await client.execute_workflow(
            ScoringPipelineWorkflow.run,
            ScoringPipelineInput(db_path=db_path, org_id=org_id),
            id="laura-demo-scoring", task_queue=TASK_QUEUE)
        print("scoring:", scored)


def main() -> None:
    parser = argparse.ArgumentParser(prog="laura-temporal-worker")
    parser.add_argument("mode", choices=["worker", "demo"])
    parser.add_argument("--db", default="laura.db")
    args = parser.parse_args()
    if args.mode == "worker":
        asyncio.run(_run_worker(args.db))
    else:
        asyncio.run(_run_demo(args.db))


if __name__ == "__main__":
    main()
