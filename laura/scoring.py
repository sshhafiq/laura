"""The effort engine: "Laura Hours".

Every PR gets an estimated number of *expert hours* — the time a strong
engineer who already knows the codebase would need to produce the same
change — plus a **reasoning breakdown**: a list of named factors whose
contributions sum (times the org calibration multiplier) to the score.
No black box: anyone can open a score and see exactly where the hours
came from. That transparency is the point of doing this open-source.

The default scorer is a deterministic heuristic over diff features, so
it runs offline with zero API keys. Teams that want a model in the loop
implement the :class:`LLMScorer` protocol and inject their client;
:class:`StubLLMScorer` shows the seam and refuses to run without one.

Review effort is scored separately: reviewing is real work, and it is
not proportional to authoring effort.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from laura.models import PullRequest, Review

# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(_test|_spec|test_|spec_)\.|\.test\.|"
    r"\.spec\.|conftest\.",
    re.IGNORECASE)
_DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
_CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"}
_GENERATED_HINTS = ("lock", "generated", ".min.", "vendor/", "dist/",
                    "node_modules/", ".pb.go", "_pb2.py")

#: Effort weight per line by file kind. Generated code is nearly free to
#: "write"; config is cheap; tests cost real thought but less than the
#: behaviour they pin down.
FILE_TYPE_WEIGHTS = {
    "source": 1.0,
    "test": 0.55,
    "config": 0.45,
    "docs": 0.25,
    "generated": 0.08,
}


def classify_file(path: str) -> str:
    lower = path.lower()
    if any(hint in lower for hint in _GENERATED_HINTS):
        return "generated"
    if _TEST_RE.search(lower):
        return "test"
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if suffix in _DOC_EXTS:
        return "docs"
    if suffix in _CONFIG_EXTS or lower.endswith(("dockerfile", "makefile")):
        return "config"
    return "source"


def _top_dir(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[0] if len(parts) > 1 else "."


# ---------------------------------------------------------------------------
# Scoring result types
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    """One named contribution to a score, in raw (pre-calibration) hours."""

    name: str
    hours: float
    detail: str


@dataclass
class ScoreResult:
    hours: float
    factors: list[Factor]
    multiplier: float = 1.0

    def to_dict(self) -> dict:
        return {
            "hours": self.hours,
            "multiplier": self.multiplier,
            "factors": [
                {"name": f.name, "hours": round(f.hours, 4), "detail": f.detail}
                for f in self.factors
            ],
        }


# ---------------------------------------------------------------------------
# Heuristic scorer
# ---------------------------------------------------------------------------

_RISKY_TITLE_RE = re.compile(
    r"\b(migrat\w*|refactor\w*|rewrite|overhaul|upgrade|security|auth\w*|"
    r"payment\w*|billing|crypto|permission\w*|schema)\b", re.IGNORECASE)


class HeuristicScorer:
    """Deterministic Laura Hours from diff features.

    Formula sketch (each term is a reported factor):

    - **weighted churn** — per-file churn weighted by file kind, then
      ``0.9 * log2(1 + weighted_churn / 25)``: effort grows with size
      but sub-linearly, because much of a big diff is pattern repetition.
    - **file spread** — 0.12h per file beyond the first: context
      switching and keeping files consistent.
    - **cross-file coupling** — 0.3h per top-level directory beyond the
      first: changes that span modules carry integration risk.
    - **risk surface** — migrations/refactors/auth/payments keywords in
      the title add a flat 0.75h of care-and-verification effort.
    - **description** — a real PR description (>= 40 chars) *saves*
      reviewer/author round-trips; an empty one on a non-trivial PR
      adds clarification effort.
    - **test coverage of the change** — tests present: small effort
      credit is already in the weights; a source-only change with zero
      test files adds a verification factor.
    """

    def score(self, pr: PullRequest, *, multiplier: float = 1.0) -> ScoreResult:
        factors: list[Factor] = []

        if pr.files:
            weighted = sum(f.churn * FILE_TYPE_WEIGHTS[classify_file(f.path)]
                           for f in pr.files)
            files_n = len(pr.files)
            dirs = {_top_dir(f.path) for f in pr.files}
            test_files = sum(1 for f in pr.files
                             if classify_file(f.path) == "test")
        else:  # webhook payloads sometimes lack per-file detail
            weighted = float(pr.churn)
            files_n = pr.changed_files or (1 if pr.churn else 0)
            dirs = set()
            test_files = 0

        churn_hours = 0.9 * math.log2(1.0 + weighted / 25.0)
        factors.append(Factor(
            "weighted_churn", churn_hours,
            f"{weighted:.0f} weighted lines across {files_n} file(s)"))

        if files_n > 1:
            factors.append(Factor(
                "file_spread", 0.12 * (files_n - 1),
                f"{files_n} files to keep consistent"))

        if len(dirs) > 1:
            factors.append(Factor(
                "cross_file_coupling", 0.3 * (len(dirs) - 1),
                f"spans {len(dirs)} top-level areas: "
                + ", ".join(sorted(dirs))))

        if _RISKY_TITLE_RE.search(pr.title or ""):
            factors.append(Factor(
                "risk_surface", 0.75,
                "title signals a high-care change (migration / refactor / "
                "auth / payments / schema class)"))

        body_len = len((pr.body or "").strip())
        if body_len >= 40:
            factors.append(Factor(
                "description", -0.15,
                "substantive PR description reduces round-trips"))
        elif weighted >= 100:
            factors.append(Factor(
                "description", 0.25,
                "no real description on a non-trivial change: "
                "clarification effort"))

        if pr.files and weighted >= 100 and test_files == 0:
            factors.append(Factor(
                "untested_change", 0.4,
                "no test files in a non-trivial change: manual "
                "verification effort"))

        raw = sum(f.hours for f in factors)
        hours = max(0.05, round(raw * multiplier, 2))
        return ScoreResult(hours=hours, factors=factors,
                           multiplier=multiplier)


# ---------------------------------------------------------------------------
# LLM scorer seam
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMScorer(Protocol):
    """Plug a real model in here.

    Implementations receive the PR (with per-file stats) and return a
    :class:`ScoreResult` whose factors explain the estimate. Laura
    stores whatever comes back next to the heuristic score's shape, so
    dashboards don't care which scorer produced a number.
    """

    def score(self, pr: PullRequest, *, multiplier: float = 1.0) -> ScoreResult:
        ...


class StubLLMScorer:
    """The seam, unimplemented on purpose.

    Construct with a client object exposing
    ``estimate_hours(pr_summary: dict) -> ScoreResult`` to delegate;
    without one, scoring raises — Laura never silently fakes an LLM.
    """

    def __init__(self, client=None):
        self.client = client

    def score(self, pr: PullRequest, *, multiplier: float = 1.0) -> ScoreResult:
        if self.client is None:
            raise RuntimeError(
                "StubLLMScorer has no LLM client injected; use "
                "HeuristicScorer or pass a client implementing "
                "estimate_hours(pr_summary)")
        summary = {
            "title": pr.title, "body": pr.body, "additions": pr.additions,
            "deletions": pr.deletions, "changed_files": pr.changed_files,
            "files": [{"path": f.path, "additions": f.additions,
                       "deletions": f.deletions} for f in pr.files],
        }
        return self.client.estimate_hours(summary)


# ---------------------------------------------------------------------------
# Review scoring
# ---------------------------------------------------------------------------

@dataclass
class ReviewScore:
    effort_hours: float
    quality: str  # "thorough" | "standard" | "rubber_stamp_risk"
    signals: list[str] = field(default_factory=list)


def score_review(review: Review, pr: PullRequest, *,
                 pr_opened_at: datetime | None = None) -> ReviewScore:
    """Estimate review effort and flag quality signals for one review.

    Effort grows with the size of what was reviewed and the depth of the
    feedback. Quality is a *signal*, not a judgement of the person: a
    fast, comment-free approval of a huge PR is flagged so teams can
    look, not so anyone gets punished.
    """
    signals: list[str] = []
    size_term = 0.25 * math.log2(1.0 + pr.churn / 50.0)
    depth_term = 0.05 * review.comments_count + (
        0.1 if len((review.body or "").strip()) >= 80 else 0.0)
    effort = round(0.1 + size_term + depth_term, 2)

    turnaround_min = None
    if pr_opened_at and review.submitted_at:
        turnaround_min = (review.submitted_at - pr_opened_at
                          ).total_seconds() / 60.0

    if review.state == "APPROVED" and review.comments_count == 0 \
            and pr.churn >= 400 and turnaround_min is not None \
            and turnaround_min < 5:
        quality = "rubber_stamp_risk"
        signals.append(
            f"approved {pr.churn} changed lines in "
            f"{turnaround_min:.0f} min with no comments")
    elif review.comments_count >= 3 or len((review.body or "").strip()) >= 200:
        quality = "thorough"
        signals.append("substantive written feedback")
    else:
        quality = "standard"
    if review.state == "CHANGES_REQUESTED":
        signals.append("changes requested: author round-trip expected")
    return ReviewScore(effort_hours=effort, quality=quality, signals=signals)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def business_hours(start: datetime, end: datetime) -> float:
    """Elapsed hours counting only Mon–Fri 09:00–18:00 UTC.

    A deliberately simple, documented proxy for "how long did this PR
    actually occupy the team" used by calibration.
    """
    if end <= start:
        return 0.0
    total = 0.0
    cursor = start
    step_hours = 1.0
    from datetime import timedelta
    while cursor < end:
        nxt = min(cursor + timedelta(hours=step_hours), end)
        if cursor.weekday() < 5 and 9 <= cursor.hour < 18:
            total += (nxt - cursor).total_seconds() / 3600.0
        cursor = nxt
    return total


def calibrate(store, org_id: int, *, scorer: HeuristicScorer | None = None,
              min_samples: int = 5) -> float:
    """Learn the org's calibration multiplier from merged PRs.

    Method (documented, deliberately simple): for every merged PR with a
    first-commit and merge timestamp, the *actual* effort proxy is the
    business-hours span from first commit to merge, capped at 40h (a PR
    that sat in a queue over a holiday is not 300 hours of work). The
    multiplier is ``median(actual) / median(raw_heuristic_score)``,
    clamped to [0.25, 4.0] so a weird month can't wreck the scale. With
    fewer than ``min_samples`` usable PRs the current multiplier is
    kept. The result is persisted on the org and returned.
    """
    scorer = scorer or HeuristicScorer()
    org = store.get_org(org_id)
    actuals: list[float] = []
    raws: list[float] = []
    for pr in store.list_pull_requests(org_id, state="merged"):
        if pr.first_commit_at is None or pr.merged_at is None:
            continue
        actual = min(business_hours(pr.first_commit_at, pr.merged_at), 40.0)
        if actual <= 0:
            continue
        actuals.append(actual)
        raws.append(scorer.score(pr, multiplier=1.0).hours)
    if len(actuals) < min_samples:
        return org.calibration_multiplier
    ratio = statistics.median(actuals) / max(statistics.median(raws), 0.05)
    multiplier = round(min(4.0, max(0.25, ratio)), 3)
    store.set_calibration(org_id, multiplier)
    return multiplier
