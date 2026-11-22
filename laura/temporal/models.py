"""Serializable inputs/outputs for Laura's Temporal workflows.

Everything that crosses a workflow/activity boundary is a plain
dataclass of primitives — Temporal serialises these, so no store
objects, no datetimes with tz surprises (ISO strings instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RepoSyncInput:
    db_path: str
    org_id: int
    repo_id: int
    repo_full_name: str


@dataclass
class RepoSyncResult:
    repo_full_name: str
    pull_requests: int = 0
    reviews: int = 0
    commits: int = 0
    line_events: int = 0


@dataclass
class OrgSyncInput:
    """Sync every registered repo of an org, one activity per repo."""

    db_path: str
    org_id: int


@dataclass
class OrgSyncResult:
    repos: list[RepoSyncResult] = field(default_factory=list)


@dataclass
class ScoringPipelineInput:
    db_path: str
    org_id: int
    #: PRs scored per parallel wave (activity fan-out width).
    wave_size: int = 8


@dataclass
class UnscoredIdsResult:
    pr_ids: list[int] = field(default_factory=list)
    multiplier: float = 1.0


@dataclass
class ScoreBatchInput:
    db_path: str
    org_id: int
    pr_ids: list[int]
    multiplier: float = 1.0


@dataclass
class ScoreBatchResult:
    scored: int = 0
    total_hours: float = 0.0


@dataclass
class RollupInput:
    db_path: str
    org_id: int


@dataclass
class ScoringPipelineResult:
    scored: int = 0
    total_hours: float = 0.0
    scored_prs_total: int = 0
    laura_hours_total: float = 0.0
