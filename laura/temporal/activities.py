"""Activities for Laura's Temporal workflows.

Activities own all the I/O: opening the SQLite store, running a repo
sync through a transport, scoring PRs. Workflows stay deterministic
and just orchestrate them with retry policies.

The sync activity takes its transport from the activity instance, so a
deployment injects an HTTP-backed GitHub transport while tests and the
demo inject :class:`FakeGitHubTransport` — the workflow code is
identical either way.
"""

from __future__ import annotations

from temporalio import activity

from laura.ingest.github_sync import GitHubTransport, sync_repo
from laura.scoring import HeuristicScorer
from laura.store import LauraStore
from laura.temporal.models import (
    RepoSyncInput,
    RepoSyncResult,
    RollupInput,
    ScoreBatchInput,
    ScoreBatchResult,
    ScoringPipelineResult,
    UnscoredIdsResult,
)


class LauraActivities:
    def __init__(self, transport: GitHubTransport | None = None):
        self.transport = transport
        self.scorer = HeuristicScorer()

    @activity.defn
    async def sync_repo_activity(self, inp: RepoSyncInput) -> RepoSyncResult:
        if self.transport is None:
            raise RuntimeError(
                "sync_repo_activity needs a GitHub transport; construct "
                "LauraActivities(transport=...) on the worker")
        store = LauraStore(inp.db_path)
        try:
            repo = store.get_repo_by_name(inp.org_id, inp.repo_full_name)
            counts = sync_repo(store, self.transport, inp.org_id, repo)
        finally:
            store.close()
        return RepoSyncResult(repo_full_name=inp.repo_full_name, **counts)

    @activity.defn
    async def list_repos_activity(self, org_id_and_db: tuple) -> list[dict]:
        db_path, org_id = org_id_and_db
        store = LauraStore(db_path)
        try:
            return [{"id": r.id, "full_name": r.full_name}
                    for r in store.list_repos(org_id)]
        finally:
            store.close()

    @activity.defn
    async def unscored_ids_activity(
            self, inp: RollupInput) -> UnscoredIdsResult:
        store = LauraStore(inp.db_path)
        try:
            org = store.get_org(inp.org_id)
            prs = store.list_pull_requests(inp.org_id, unscored_only=True)
            return UnscoredIdsResult(
                pr_ids=[pr.id for pr in prs],
                multiplier=org.calibration_multiplier)
        finally:
            store.close()

    @activity.defn
    async def score_batch_activity(
            self, inp: ScoreBatchInput) -> ScoreBatchResult:
        store = LauraStore(inp.db_path)
        result = ScoreBatchResult()
        try:
            for pr_id in inp.pr_ids:
                pr = store.get_pull_request(inp.org_id, pr_id)
                scored = self.scorer.score(pr, multiplier=inp.multiplier)
                store.set_score(inp.org_id, pr_id, scored.hours,
                                scored.to_dict())
                result.scored += 1
                result.total_hours = round(
                    result.total_hours + scored.hours, 2)
                try:
                    activity.heartbeat(f"scored PR {pr.number}")
                except Exception:
                    pass  # direct (non-Temporal) invocation in tests
        finally:
            store.close()
        return result

    @activity.defn
    async def rollup_activity(
            self, inp: RollupInput) -> ScoringPipelineResult:
        store = LauraStore(inp.db_path)
        try:
            prs = [pr for pr in store.list_pull_requests(inp.org_id)
                   if pr.score_hours is not None]
            return ScoringPipelineResult(
                scored_prs_total=len(prs),
                laura_hours_total=round(
                    sum(pr.score_hours or 0.0 for pr in prs), 2))
        finally:
            store.close()
