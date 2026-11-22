"""Demo seed: a realistic little company, generated deterministically.

``laura demo`` builds "DemoCo": two teams, six engineers, two repos,
~30 pull requests over the last four months with diffs, reviews and
commits, plus two months of AI tool usage — then scores every PR. A new
user goes from ``pip install`` to real dashboards in under a minute,
with data shaped like a real startup's (a migration, a hotfix, a revert,
one rubber-stamped monster PR, a turnover hotspot).

The generator is seeded, so the demo is identical on every machine and
the test-suite can assert on it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from laura.ingest.tokens_csv import import_token_csv
from laura.models import (Commit, Initiative, InitiativeRule, PullRequest,
                          PRFile, Repo, Review, User)
from laura.scoring import HeuristicScorer

ENGINEERS = [
    ("ada", "Ada Osei", "platform"),
    ("ben", "Ben Ruiz", "platform"),
    ("cy", "Cy Park", "platform"),
    ("dee", "Dee Nguyen", "product"),
    ("eli", "Eli Ford", "product"),
    ("fay", "Fay Ibrahim", "product"),
]

REPOS = [("acme/api", "main"), ("acme/web", "main")]

_TITLES = [
    ("feat/payments-retry", "Add retry with backoff to payment capture", "api"),
    ("fix/session-race", "Fix race condition in session cache", "api"),
    ("feat/search-filters", "Add faceted filters to search endpoint", "api"),
    ("chore/deps-bump", "Bump dependencies to latest minor", "api"),
    ("feat/onboarding-flow", "New onboarding checklist UI", "web"),
    ("fix/checkout-total", "Fix checkout total rounding", "web"),
    ("refactor/auth-module", "Refactor auth module into service layer", "api"),
    ("feat/dark-mode", "Add dark mode toggle", "web"),
    ("migration/users-table", "Migration: split users table for tenancy", "api"),
    ("feat/export-csv", "Add CSV export for reports", "web"),
    ("fix/timeout-504", "Increase gateway timeout and add circuit breaker", "api"),
    ("feat/notifications", "Email notifications for mentions", "api"),
    ("perf/query-index", "Add index for slow orders query", "api"),
    ("feat/profile-page", "Redesign profile page", "web"),
    ("fix/memory-leak", "Fix memory leak in websocket handler", "api"),
    ("feat/audit-log", "Add audit log for admin actions", "api"),
    ("hotfix/payment-500", "Hotfix: payment endpoint 500 on empty cart", "api"),
    ("feat/i18n", "Internationalisation scaffolding", "web"),
    ("revert/feat-flags", "Revert feature flag rollout", "api"),
    ("feat/rate-limits", "Per-tenant API rate limits", "api"),
]

_FILE_POOLS = {
    "api": [
        ("src/api/payments.py", "source"), ("src/api/auth.py", "source"),
        ("src/api/search.py", "source"), ("src/core/cache.py", "source"),
        ("src/core/db.py", "source"), ("migrations/0042_users.py", "source"),
        ("tests/test_payments.py", "test"), ("tests/test_auth.py", "test"),
        ("tests/test_search.py", "test"), ("config/settings.yaml", "config"),
        ("README.md", "docs"), ("src/api/orders.py", "source"),
    ],
    "web": [
        ("src/pages/Checkout.tsx", "source"), ("src/pages/Profile.tsx", "source"),
        ("src/components/Onboarding.tsx", "source"),
        ("src/lib/api.ts", "source"), ("src/styles/theme.css", "config"),
        ("tests/checkout.test.ts", "test"), ("package.json", "config"),
        ("src/i18n/en.json", "config"),
    ],
}


def seed_demo(store, *, slug: str = "democo") -> dict:
    """Create the demo org in ``store``; return a summary dict."""
    existing = store.get_org_by_slug(slug)
    if existing is not None:
        return {"org_id": existing.id, "slug": slug, "already_seeded": True}
    org, api_key = store.create_org("DemoCo", slug)
    org_id = org.id
    rng = random.Random(20261121)
    now = datetime.now(timezone.utc).replace(minute=0, second=0,
                                             microsecond=0)

    users = {}
    for login, name, team in ENGINEERS:
        users[login] = store.upsert_user(User(
            id=None, org_id=org_id, login=login, name=name,
            email=f"{login}@democo.example", team=team))
    repos = {}
    for full_name, branch in REPOS:
        repos[full_name] = store.upsert_repo(Repo(
            id=None, org_id=org_id, full_name=full_name,
            default_branch=branch))

    initiative = store.create_initiative(org_id, "Payments reliability")
    store.add_initiative_rule(InitiativeRule(
        id=None, org_id=org_id, initiative_id=initiative.id,
        kind="branch_prefix", pattern="feat/payments"))
    store.add_initiative_rule(InitiativeRule(
        id=None, org_id=org_id, initiative_id=initiative.id,
        kind="issue_key", pattern="PAY-"))

    scorer = HeuristicScorer()
    counts = {"pull_requests": 0, "reviews": 0, "commits": 0}
    pr_number = {name: 100 for name in repos}

    def make_pr(repo_key: str, branch: str, title: str, days_ago: int,
                size: str, *, hotfix: bool = False,
                revert: bool = False) -> None:
        repo = repos[f"acme/{repo_key}"]
        pr_number[repo.full_name] += 1
        number = pr_number[repo.full_name]
        author = rng.choice(list(users))
        created = now - timedelta(days=days_ago, hours=rng.randint(1, 9))
        first_commit = created - timedelta(hours=rng.randint(2, 30))
        churn_target = {"small": 60, "medium": 350, "large": 1400}[size]
        pool = _FILE_POOLS[repo_key]
        chosen = rng.sample(pool, k=min(len(pool), rng.randint(2, 6)))
        files = []
        remaining = churn_target
        for i, (path, _kind) in enumerate(chosen):
            share = remaining if i == len(chosen) - 1 else int(
                remaining * rng.uniform(0.15, 0.45))
            remaining -= share
            add = int(share * 0.7)
            files.append(PRFile(path=path, additions=add,
                                deletions=share - add))
        merged = created + timedelta(hours=rng.randint(6, 72))
        pr = store.upsert_pull_request(PullRequest(
            id=None, org_id=org_id, repo_id=repo.id, number=number,
            title=title, author_login=author, branch=branch,
            base_branch="main",
            body=("Implements the change described in the ticket, with "
                  "tests and a staged rollout plan." if size != "small"
                  else ""),
            state="merged", created_at=created,
            first_commit_at=first_commit, merged_at=merged,
            closed_at=merged,
            additions=sum(f.additions for f in files),
            deletions=sum(f.deletions for f in files),
            changed_files=len(files), is_revert=revert, is_hotfix=hotfix,
            files=files))
        counts["pull_requests"] += 1
        counts["line_events"] = counts.get("line_events", 0) \
            + store.record_merge_line_events(pr)
        # reviews: a peer from the other side of the roster
        reviewers = [u for u in users if u != author]
        for j in range(rng.randint(1, 2)):
            reviewer = rng.choice(reviewers)
            when = created + timedelta(hours=rng.randint(2, 20) + j * 8)
            if when > merged:
                when = merged - timedelta(minutes=30)
            store.add_review(Review(
                id=None, org_id=org_id, pr_id=pr.id,
                reviewer_login=reviewer,
                state="APPROVED" if j == 0 else "COMMENTED",
                submitted_at=when,
                body=("Looks solid, one question on the retry bounds."
                      if j == 0 else ""),
                comments_count=rng.randint(0, 4)))
            counts["reviews"] += 1
        for k in range(rng.randint(1, 4)):
            store.add_commit(Commit(
                id=None, org_id=org_id, pr_id=pr.id,
                sha=f"demo{repo_key}{number:04d}{k:02d}" + "0" * 24,
                author_login=author,
                committed_at=first_commit + timedelta(hours=k * 3),
                message=title))
            counts["commits"] += 1
        result = scorer.score(pr, multiplier=org.calibration_multiplier)
        store.set_score(org_id, pr.id, result.hours, result.to_dict())
        from laura.ingest.initiatives import link_initiatives
        link_initiatives(store, pr, repo.full_name)

    for i, (branch, title, repo_key) in enumerate(_TITLES):
        size = ("large" if i in (8, 6) else
                "medium" if i % 3 else "small")
        make_pr(repo_key, branch, title, days_ago=110 - i * 5, size=size,
                hotfix="hotfix" in branch, revert=branch.startswith("revert"))

    # A turnover hotspot: the payments file gets rewritten twice more.
    for j, days in enumerate((40, 12)):
        make_pr("api", f"feat/payments-rework-{j}",
                "Rework payment capture flow (PAY-20%d)" % j,
                days_ago=days, size="medium")

    # Token usage: two months, three tools, CSV import path (dogfooding).
    lines = ["date,engineer,tool,model,input_tokens,output_tokens,cost_usd"]
    tools = [("claude-code", "claude-sonnet-4.5", 420),
             ("cursor", "cursor-default", 260),
             ("copilot", "gpt-5", 90)]
    for day_offset in range(60):
        day = (now - timedelta(days=day_offset)).date().isoformat()
        for login in users:
            for tool, model, base in tools:
                if rng.random() < 0.45:
                    continue
                inp = int(base * rng.uniform(600, 1600))
                out = int(inp * 0.3)
                cost = round(inp / 1e6 * 3 + out / 1e6 * 15, 2)
                lines.append(f"{day},{login},{tool},{model},{inp},{out},{cost}")
    imported = import_token_csv(store, org_id, "\n".join(lines))

    return {
        "org_id": org_id, "slug": slug, "api_key": api_key,
        "users": len(users), "repos": len(repos),
        "token_rows": imported.imported, **counts,
    }
