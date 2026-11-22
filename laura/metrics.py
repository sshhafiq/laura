"""The metrics engine.

Everything here is a pure function over the store: give it an org and a
period, get numbers back. Medians everywhere (means lie about software
work). Periods are ``(start, end)`` datetimes; a PR belongs to a period
by its merge time for throughput/DORA, by creation for review stats.

Metrics provided:

- **cycle time stages** — first commit → first review → approval →
  merge, median hours per stage
- **throughput** — merged PRs and Laura Hours, per team/engineer too
- **DORA** — deployment frequency (merges to the default branch per
  week), lead time for changes, change-failure rate (revert + hotfix
  share of merges), MTTR proxy (hotfix creation → merge time)
- **code turnover** — of the lines changed in a window, what share were
  changed again within 90 days ("code that didn't stick")
- **review load** — reviews per reviewer, turnaround medians, so load
  balancing problems are visible instead of anecdotal
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

Period = tuple[datetime, datetime]


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds() / 3600.0


def _in(dt: datetime | None, period: Period) -> bool:
    return dt is not None and period[0] <= dt < period[1]


# ---------------------------------------------------------------------------
# Cycle time
# ---------------------------------------------------------------------------

@dataclass
class CycleTime:
    commit_to_review_h: float | None = None
    review_to_approval_h: float | None = None
    approval_to_merge_h: float | None = None
    commit_to_merge_h: float | None = None
    sample_size: int = 0


def cycle_time(store, org_id: int, period: Period) -> CycleTime:
    stages: dict[str, list[float]] = defaultdict(list)
    count = 0
    for pr in store.list_pull_requests(org_id, state="merged"):
        if not _in(pr.merged_at, period):
            continue
        reviews = store.list_reviews(org_id, pr.id)
        first_review = reviews[0].submitted_at if reviews else None
        approval = next((r.submitted_at for r in reviews
                         if r.state == "APPROVED"), None)
        count += 1
        for name, a, b in (
            ("commit_to_review", pr.first_commit_at, first_review),
            ("review_to_approval", first_review, approval),
            ("approval_to_merge", approval, pr.merged_at),
            ("commit_to_merge", pr.first_commit_at, pr.merged_at),
        ):
            value = _hours(a, b)
            if value is not None:
                stages[name].append(value)
    return CycleTime(
        commit_to_review_h=_median(stages["commit_to_review"]),
        review_to_approval_h=_median(stages["review_to_approval"]),
        approval_to_merge_h=_median(stages["approval_to_merge"]),
        commit_to_merge_h=_median(stages["commit_to_merge"]),
        sample_size=count,
    )


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------

@dataclass
class Throughput:
    merged_prs: int = 0
    laura_hours: float = 0.0
    by_engineer: dict[str, dict] = field(default_factory=dict)
    by_team: dict[str, dict] = field(default_factory=dict)


def throughput(store, org_id: int, period: Period) -> Throughput:
    result = Throughput()
    for pr in store.list_pull_requests(org_id, state="merged"):
        if not _in(pr.merged_at, period):
            continue
        hours = pr.score_hours or 0.0
        result.merged_prs += 1
        result.laura_hours = round(result.laura_hours + hours, 2)
        for bucket, key in (
            (result.by_engineer, pr.author_login),
            (result.by_team, store.team_of(org_id, pr.author_login)
             or "(no team)"),
        ):
            entry = bucket.setdefault(key, {"prs": 0, "laura_hours": 0.0})
            entry["prs"] += 1
            entry["laura_hours"] = round(entry["laura_hours"] + hours, 2)
    return result


# ---------------------------------------------------------------------------
# DORA
# ---------------------------------------------------------------------------

@dataclass
class DoraMetrics:
    deployments_per_week: float = 0.0
    lead_time_h: float | None = None
    change_failure_rate: float | None = None
    mttr_proxy_h: float | None = None
    merges: int = 0
    failures: int = 0


def dora(store, org_id: int, period: Period) -> DoraMetrics:
    repos = {r.id: r for r in store.list_repos(org_id)}
    merged = [pr for pr in store.list_pull_requests(org_id, state="merged")
              if _in(pr.merged_at, period)]
    deploys = [pr for pr in merged
               if pr.base_branch == repos[pr.repo_id].default_branch]
    weeks = max((period[1] - period[0]).days / 7.0, 1 / 7.0)
    failures = [pr for pr in merged if pr.is_revert or pr.is_hotfix]
    lead = [v for pr in merged
            if (v := _hours(pr.first_commit_at, pr.merged_at)) is not None]
    mttr = [v for pr in failures if pr.is_hotfix
            and (v := _hours(pr.created_at, pr.merged_at)) is not None]
    return DoraMetrics(
        deployments_per_week=round(len(deploys) / weeks, 2),
        lead_time_h=_median(lead),
        change_failure_rate=(round(len(failures) / len(merged), 4)
                             if merged else None),
        mttr_proxy_h=_median(mttr),
        merges=len(merged),
        failures=len(failures),
    )


# ---------------------------------------------------------------------------
# Code turnover
# ---------------------------------------------------------------------------

@dataclass
class Turnover:
    cohort_lines: int = 0
    rechurned_lines: int = 0
    turnover_rate: float | None = None
    by_repo: dict[str, dict] = field(default_factory=dict)


def code_turnover(store, org_id: int, period: Period, *,
                  window_days: int = 90) -> Turnover:
    """Share of lines changed in ``period`` that changed again soon.

    For each line event in the period (the cohort), look at later events
    for the same file within ``window_days``: the re-changed volume is
    capped at the cohort event's size (you can't rewrite more of a
    change than the change). High turnover = code that didn't stick:
    unclear requirements, missing tests, or churny hotspots.
    """
    events = store.list_line_events(org_id)
    repo_names = {r.id: r.full_name for r in store.list_repos(org_id)}
    result = Turnover()
    for event in events:
        if not _in(event.day, period):
            continue
        later = sum(
            e.lines_changed for e in events
            if e.repo_id == event.repo_id and e.path == event.path
            and event.day < e.day <= event.day + timedelta(days=window_days)
        )
        rechurned = min(later, event.lines_changed)
        result.cohort_lines += event.lines_changed
        result.rechurned_lines += rechurned
        entry = result.by_repo.setdefault(
            repo_names.get(event.repo_id, "?"),
            {"cohort_lines": 0, "rechurned_lines": 0})
        entry["cohort_lines"] += event.lines_changed
        entry["rechurned_lines"] += rechurned
    if result.cohort_lines:
        result.turnover_rate = round(
            result.rechurned_lines / result.cohort_lines, 4)
    for entry in result.by_repo.values():
        entry["turnover_rate"] = (
            round(entry["rechurned_lines"] / entry["cohort_lines"], 4)
            if entry["cohort_lines"] else None)
    return result


# ---------------------------------------------------------------------------
# Review load & turnaround
# ---------------------------------------------------------------------------

@dataclass
class ReviewStats:
    median_turnaround_h: float | None = None
    reviews_per_reviewer: dict[str, int] = field(default_factory=dict)
    slowest_team: str | None = None
    team_turnaround_h: dict[str, float] = field(default_factory=dict)


def review_stats(store, org_id: int, period: Period) -> ReviewStats:
    turnarounds: list[float] = []
    per_reviewer: dict[str, int] = defaultdict(int)
    team_turns: dict[str, list[float]] = defaultdict(list)
    for pr in store.list_pull_requests(org_id):
        if not _in(pr.created_at, period):
            continue
        reviews = store.list_reviews(org_id, pr.id)
        if not reviews:
            continue
        first = reviews[0]
        value = _hours(pr.created_at, first.submitted_at)
        if value is not None:
            turnarounds.append(value)
            team = store.team_of(org_id, first.reviewer_login) or "(no team)"
            team_turns[team].append(value)
        for review in reviews:
            per_reviewer[review.reviewer_login] += 1
    team_medians = {t: round(statistics.median(v), 2)
                    for t, v in team_turns.items() if v}
    slowest = max(team_medians, key=team_medians.get) if team_medians else None
    return ReviewStats(
        median_turnaround_h=_median(turnarounds),
        reviews_per_reviewer=dict(per_reviewer),
        slowest_team=slowest,
        team_turnaround_h=team_medians,
    )
