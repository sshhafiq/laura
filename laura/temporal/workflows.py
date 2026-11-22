"""Laura's durable workflows.

- ``RepoSyncWorkflow`` — backfill every registered repo of an org, one
  activity per repo, each with its own retry policy. A GitHub outage
  mid-backfill retries the failed repo, not the whole org.
- ``ScoringPipelineWorkflow`` — find unscored PRs, score them in
  parallel *waves* of activities (bounded fan-out, heartbeats on long
  batches), then a rollup activity totals the org's Laura Hours. If the
  worker dies mid-wave, Temporal resumes exactly where it stopped —
  scoring 10k PRs is a batch job you should never babysit.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from laura.temporal.activities import LauraActivities
    from laura.temporal.models import (
        OrgSyncInput,
        OrgSyncResult,
        RepoSyncInput,
        RepoSyncResult,
        RollupInput,
        ScoreBatchInput,
        ScoringPipelineInput,
        ScoringPipelineResult,
    )

_SYNC_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2), maximum_attempts=4)
_SCORE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30), maximum_attempts=3)


@workflow.defn
class RepoSyncWorkflow:
    @workflow.run
    async def run(self, inp: OrgSyncInput) -> OrgSyncResult:
        repos = await workflow.execute_activity(
            LauraActivities.list_repos_activity,
            (inp.db_path, inp.org_id),
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=_SCORE_RETRY)
        results: list[RepoSyncResult] = []
        for repo in repos:
            result = await workflow.execute_activity(
                LauraActivities.sync_repo_activity,
                RepoSyncInput(db_path=inp.db_path, org_id=inp.org_id,
                              repo_id=repo["id"],
                              repo_full_name=repo["full_name"]),
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=_SYNC_RETRY)
            results.append(result)
        return OrgSyncResult(repos=results)


@workflow.defn
class ScoringPipelineWorkflow:
    def __init__(self):
        self._scored_so_far = 0

    @workflow.query
    def progress(self) -> int:
        return self._scored_so_far

    @workflow.run
    async def run(self, inp: ScoringPipelineInput) -> ScoringPipelineResult:
        head = await workflow.execute_activity(
            LauraActivities.unscored_ids_activity,
            RollupInput(db_path=inp.db_path, org_id=inp.org_id),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_SCORE_RETRY)
        total = ScoringPipelineResult()
        ids = head.pr_ids
        for start in range(0, len(ids), inp.wave_size):
            wave = ids[start:start + inp.wave_size]
            batches = await asyncio.gather(*(
                workflow.execute_activity(
                    LauraActivities.score_batch_activity,
                    ScoreBatchInput(db_path=inp.db_path, org_id=inp.org_id,
                                    pr_ids=[pr_id], multiplier=head.multiplier),
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=_SCORE_RETRY)
                for pr_id in wave))
            for batch in batches:
                total.scored += batch.scored
                total.total_hours = round(
                    total.total_hours + batch.total_hours, 2)
            self._scored_so_far = total.scored
        rollup = await workflow.execute_activity(
            LauraActivities.rollup_activity,
            RollupInput(db_path=inp.db_path, org_id=inp.org_id),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_SCORE_RETRY)
        total.scored_prs_total = rollup.scored_prs_total
        total.laura_hours_total = rollup.laura_hours_total
        return total
