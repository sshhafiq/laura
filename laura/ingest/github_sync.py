"""Backfill sync: pull a repo's history through a transport interface.

The transport is a tiny protocol — three paginated list calls returning
GitHub-API-shaped dicts. Production code can back it with real HTTP;
tests (and the Temporal demo) use :class:`FakeGitHubTransport`, an
in-memory implementation, so the whole sync path runs offline.

Sync is idempotent: PRs upsert on (org, repo, number), commits on
(org, sha), and line events are only recorded the first time a PR is
seen in the merged state.
"""

from __future__ import annotations

from typing import Protocol

from laura.ingest.webhooks import _ts, detect_hotfix, detect_revert
from laura.models import Commit, PRFile, PullRequest, Repo, Review

PAGE_SIZE = 30


class GitHubTransport(Protocol):
    """The slice of the GitHub API Laura's backfill needs."""

    def list_pull_requests(self, repo_full_name: str,
                           page: int) -> list[dict]:
        ...

    def list_reviews(self, repo_full_name: str,
                     pr_number: int) -> list[dict]:
        ...

    def list_commits(self, repo_full_name: str,
                     pr_number: int) -> list[dict]:
        ...


class FakeGitHubTransport:
    """In-memory transport for tests and demos.

    Seed it with GitHub-API-shaped dicts::

        transport = FakeGitHubTransport()
        transport.add_repo("acme/api", prs=[{...}], reviews={1: [...]},
                           commits={1: [...]})
    """

    def __init__(self):
        self._repos: dict[str, dict] = {}

    def add_repo(self, full_name: str, *, prs: list[dict] | None = None,
                 reviews: dict[int, list[dict]] | None = None,
                 commits: dict[int, list[dict]] | None = None) -> None:
        self._repos[full_name] = {
            "prs": prs or [],
            "reviews": reviews or {},
            "commits": commits or {},
        }

    def list_pull_requests(self, repo_full_name: str,
                           page: int) -> list[dict]:
        prs = self._repos.get(repo_full_name, {}).get("prs", [])
        start = (page - 1) * PAGE_SIZE
        return prs[start:start + PAGE_SIZE]

    def list_reviews(self, repo_full_name: str,
                     pr_number: int) -> list[dict]:
        return self._repos.get(repo_full_name, {}).get(
            "reviews", {}).get(pr_number, [])

    def list_commits(self, repo_full_name: str,
                     pr_number: int) -> list[dict]:
        return self._repos.get(repo_full_name, {}).get(
            "commits", {}).get(pr_number, [])


def _pr_from_api(raw: dict, *, org_id: int, repo_id: int) -> PullRequest:
    merged = bool(raw.get("merged_at"))
    title = raw.get("title") or ""
    files = [
        PRFile(path=f.get("filename", ""), additions=int(f.get("additions") or 0),
               deletions=int(f.get("deletions") or 0))
        for f in raw.get("files", [])
    ]
    return PullRequest(
        id=None, org_id=org_id, repo_id=repo_id,
        number=int(raw["number"]), title=title,
        author_login=(raw.get("user") or {}).get("login", ""),
        branch=(raw.get("head") or {}).get("ref", ""),
        base_branch=(raw.get("base") or {}).get("ref", "main"),
        body=raw.get("body") or "",
        state="merged" if merged else (raw.get("state") or "open"),
        created_at=_ts(raw.get("created_at")),
        merged_at=_ts(raw.get("merged_at")),
        closed_at=_ts(raw.get("closed_at")),
        additions=int(raw.get("additions") or 0),
        deletions=int(raw.get("deletions") or 0),
        changed_files=int(raw.get("changed_files") or len(files)),
        is_revert=detect_revert(title),
        is_hotfix=detect_hotfix(title, (raw.get("head") or {}).get("ref", "")),
        files=files,
    )


def sync_repo(store, transport: GitHubTransport, org_id: int,
              repo: Repo) -> dict:
    """Backfill one repo. Returns counts of what was ingested."""
    counts = {"pull_requests": 0, "reviews": 0, "commits": 0,
              "line_events": 0}
    page = 1
    while True:
        raw_prs = transport.list_pull_requests(repo.full_name, page)
        if not raw_prs:
            break
        for raw in raw_prs:
            existing = store.find_pull_request(
                org_id, repo.id, int(raw["number"]))
            was_merged = existing is not None and existing.state == "merged"
            pr = store.upsert_pull_request(
                _pr_from_api(raw, org_id=org_id, repo_id=repo.id))
            counts["pull_requests"] += 1
            for raw_review in transport.list_reviews(repo.full_name,
                                                     pr.number):
                store.add_review(Review(
                    id=None, org_id=org_id, pr_id=pr.id,
                    reviewer_login=(raw_review.get("user") or {}).get(
                        "login", ""),
                    state=(raw_review.get("state") or "commented").upper(),
                    submitted_at=_ts(raw_review.get("submitted_at")),
                    body=raw_review.get("body") or "",
                    comments_count=int(
                        raw_review.get("comments_count") or 0),
                ))
                counts["reviews"] += 1
            commit_times = []
            for raw_commit in transport.list_commits(repo.full_name,
                                                     pr.number):
                commit = (raw_commit.get("commit") or {})
                when = _ts((commit.get("author") or {}).get("date"))
                store.add_commit(Commit(
                    id=None, org_id=org_id, pr_id=pr.id,
                    sha=raw_commit.get("sha", ""),
                    author_login=(raw_commit.get("author") or {}).get(
                        "login", ""),
                    committed_at=when,
                    message=(commit.get("message") or "").splitlines()[0]
                    if commit.get("message") else "",
                ))
                counts["commits"] += 1
                if when:
                    commit_times.append(when)
            if commit_times and pr.first_commit_at is None:
                pr.first_commit_at = min(commit_times)
                store.upsert_pull_request(pr)
            if pr.state == "merged" and not was_merged:
                counts["line_events"] += store.record_merge_line_events(pr)
        page += 1
    return counts
