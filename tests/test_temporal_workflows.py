"""Workflow tests against Temporal's time-skipping test environment.

These run the real workflows end to end with no docker and no cluster.
In sandboxes where the test-server binary cannot run (it downloads on
first use), the whole module is skipped and the activity tests still
cover the logic — the same pattern as the sibling projects.
"""

from __future__ import annotations

import asyncio

import pytest

temporalio = pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from laura.demo import seed_demo  # noqa: E402
from laura.store import LauraStore  # noqa: E402
from laura.temporal.activities import LauraActivities  # noqa: E402
from laura.temporal.models import ScoringPipelineInput  # noqa: E402
from laura.temporal.worker import demo_transport  # noqa: E402
from laura.temporal.workflows import ScoringPipelineWorkflow  # noqa: E402


async def _run_pipeline(db_path: str, org_id: int):
    try:
        env = await asyncio.wait_for(
            WorkflowEnvironment.start_time_skipping(), timeout=90)
    except Exception as exc:  # server binary unavailable / hangs
        pytest.skip(f"Temporal test server unavailable: {exc}")
    async with env:
        activities = LauraActivities(transport=demo_transport())
        async with Worker(
                env.client, task_queue="laura-test",
                workflows=[ScoringPipelineWorkflow],
                activities=[activities.unscored_ids_activity,
                            activities.score_batch_activity,
                            activities.rollup_activity]):
            return await env.client.execute_workflow(
                ScoringPipelineWorkflow.run,
                ScoringPipelineInput(db_path=db_path, org_id=org_id),
                id="laura-test-scoring", task_queue="laura-test")


def test_scoring_pipeline_workflow(tmp_path):
    db_path = str(tmp_path / "laura.db")
    store = LauraStore(db_path)
    summary = seed_demo(store)
    org_id = summary["org_id"]
    store.conn.execute(
        "UPDATE pull_requests SET score_hours = NULL WHERE number IN "
        "(SELECT number FROM pull_requests LIMIT 3)")
    store.conn.commit()
    store.close()
    result = asyncio.run(_run_pipeline(db_path, org_id))
    assert result.scored == 3
    assert result.scored_prs_total >= 20
