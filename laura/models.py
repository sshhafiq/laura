"""Domain models for Laura.

Plain dataclasses, no ORM. Every record that belongs to a tenant carries
``org_id`` and the store refuses to return rows across org boundaries.
Timestamps are timezone-aware UTC datetimes; the store serialises them as
ISO-8601 strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Org:
    """A tenant: one company (or one GitHub organisation) per Org."""

    id: int | None
    name: str
    slug: str
    created_at: datetime
    #: sha256 hex of the API key — the plaintext is shown exactly once.
    api_key_hash: str = ""
    #: shared secret used to verify GitHub webhook signatures.
    webhook_secret: str = ""
    #: learned effort-scoring multiplier (see scoring.calibrate).
    calibration_multiplier: float = 1.0


@dataclass
class User:
    """An engineer. ``team`` is a free-form label (e.g. "platform")."""

    id: int | None
    org_id: int
    login: str
    name: str = ""
    email: str = ""
    team: str = ""


@dataclass
class Repo:
    id: int | None
    org_id: int
    full_name: str  # "acme/api"
    default_branch: str = "main"


@dataclass
class PRFile:
    """One file touched by a pull request, with its churn."""

    path: str
    additions: int = 0
    deletions: int = 0

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass
class PullRequest:
    id: int | None
    org_id: int
    repo_id: int
    number: int
    title: str
    author_login: str
    branch: str = ""
    base_branch: str = "main"
    body: str = ""
    state: str = "open"  # open | closed | merged
    created_at: datetime | None = None
    first_commit_at: datetime | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    is_revert: bool = False
    is_hotfix: bool = False
    files: list[PRFile] = field(default_factory=list)
    #: Laura Hours, filled in by the scoring pipeline (None = unscored).
    score_hours: float | None = None
    #: JSON-serialisable reasoning breakdown produced with the score.
    score_reasoning: dict | None = None

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass
class Review:
    id: int | None
    org_id: int
    pr_id: int
    reviewer_login: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED
    submitted_at: datetime
    body: str = ""
    comments_count: int = 0


@dataclass
class Commit:
    id: int | None
    org_id: int
    pr_id: int
    sha: str
    author_login: str
    committed_at: datetime
    message: str = ""


@dataclass
class LineEvent:
    """Per-file line-change history, the raw material for code turnover.

    One row per (file, PR merge): how many lines that merge changed in
    the file. Turnover asks: of the lines changed on day D, how many were
    changed *again* in the same file within 90 days?
    """

    id: int | None
    org_id: int
    repo_id: int
    path: str
    day: datetime
    lines_changed: int
    pr_id: int | None = None


@dataclass
class TokenUsageRecord:
    """One day of AI coding-tool usage for one engineer."""

    id: int | None
    org_id: int
    day: datetime
    engineer_login: str
    tool: str  # e.g. "claude-code", "cursor", "copilot", "codex"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Initiative:
    """A named grouping of work (a project, an epic, a migration)."""

    id: int | None
    org_id: int
    name: str


@dataclass
class InitiativeRule:
    """How PRs get auto-linked to an initiative.

    kind:
      - ``branch_prefix``: PR head branch starts with ``pattern``
      - ``title_tag``:     PR title contains ``[pattern]`` or ``pattern:``
      - ``issue_key``:     PR title/body/branch contains ``pattern``
                           (e.g. ``PROJ-`` followed by digits)
      - ``repo``:          PR lives in the repo whose full name == pattern
    """

    id: int | None
    org_id: int
    initiative_id: int
    kind: str
    pattern: str
