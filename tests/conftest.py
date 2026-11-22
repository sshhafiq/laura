"""Shared fixtures: an in-memory store, one org, and PR builders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from laura.models import PRFile, PullRequest, Review, User
from laura.store import LauraStore

BASE = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store():
    s = LauraStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def org(store):
    org_obj, _key = store.create_org("Acme", "acme")
    return org_obj


@pytest.fixture()
def repo(store, org):
    from laura.models import Repo
    return store.upsert_repo(Repo(id=None, org_id=org.id,
                                  full_name="acme/api", default_branch="main"))


def make_pr(org_id: int, repo_id: int, number: int, *, title="A change",
            author="ada", days_ago=10, churn=120, files=None, state="merged",
            branch="feat/x", base="main", body="A real description of the "
            "change and why it is needed.", revert=False, hotfix=False,
            created_offset_h=0) -> PullRequest:
    created = BASE + timedelta(days=30 - days_ago, hours=created_offset_h)
    files = files if files is not None else [
        PRFile("src/api/core.py", int(churn * 0.5), int(churn * 0.2)),
        PRFile("tests/test_core.py", int(churn * 0.2), int(churn * 0.1)),
    ]
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)
    return PullRequest(
        id=None, org_id=org_id, repo_id=repo_id, number=number, title=title,
        author_login=author, branch=branch, base_branch=base, body=body,
        state=state, created_at=created,
        first_commit_at=created - timedelta(hours=5),
        merged_at=created + timedelta(hours=30) if state == "merged" else None,
        closed_at=created + timedelta(hours=30) if state != "open" else None,
        additions=additions, deletions=deletions, changed_files=len(files),
        is_revert=revert, is_hotfix=hotfix, files=files)


def add_review(store, org_id: int, pr, reviewer="ben", state="APPROVED",
               hours_after=8, comments=2, body="Looks good to me, ship it."):
    return store.add_review(Review(
        id=None, org_id=org_id, pr_id=pr.id, reviewer_login=reviewer,
        state=state,
        submitted_at=pr.created_at + timedelta(hours=hours_after),
        body=body, comments_count=comments))
