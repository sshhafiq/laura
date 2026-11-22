"""Webhook parsing, HMAC verification, initiative auto-linking, sync."""

import hashlib
import hmac

from laura.ingest.github_sync import FakeGitHubTransport, sync_repo
from laura.ingest.initiatives import link_initiatives, match_rule
from laura.ingest.webhooks import (parse_pull_request_event,
                                   parse_review_event, verify_signature)
from laura.models import InitiativeRule, PullRequest, Repo


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body,
                                hashlib.sha256).hexdigest()


def test_signature_verification():
    body = b'{"action":"opened"}'
    assert verify_signature("s3cret", body, _sign("s3cret", body))
    assert not verify_signature("s3cret", body, _sign("other", body))
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "md5=abc")
    assert not verify_signature("", body, _sign("s3cret", body))


def _pr_payload(**overrides):
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 42, "title": "Add caching layer",
            "body": "Speeds up the hot path.",
            "state": "open", "merged": False,
            "created_at": "2026-11-01T10:00:00Z", "merged_at": None,
            "closed_at": None, "additions": 120, "deletions": 30,
            "changed_files": 4, "user": {"login": "ada"},
            "head": {"ref": "feat/cache"}, "base": {"ref": "main"},
        },
        "repository": {"full_name": "acme/api"},
    }
    payload["pull_request"].update(overrides)
    return payload


def test_parse_pull_request_opened():
    pr = parse_pull_request_event(_pr_payload(), org_id=1, repo_id=2)
    assert pr.number == 42 and pr.state == "open"
    assert pr.author_login == "ada" and pr.branch == "feat/cache"
    assert pr.churn == 150 and not pr.is_revert and not pr.is_hotfix


def test_parse_pull_request_merged_revert_hotfix():
    payload = _pr_payload(state="closed", merged=True,
                          merged_at="2026-11-02T10:00:00Z",
                          title="Revert the cache change")
    payload["action"] = "closed"
    pr = parse_pull_request_event(payload, org_id=1, repo_id=2)
    assert pr.state == "merged" and pr.is_revert
    hot = parse_pull_request_event(
        _pr_payload(title="Hotfix: payments down"), org_id=1, repo_id=2)
    assert hot.is_hotfix


def test_parse_pull_request_ignores_label_actions():
    payload = _pr_payload()
    payload["action"] = "labeled"
    assert parse_pull_request_event(payload, org_id=1, repo_id=2) is None


def test_parse_review_event():
    payload = {
        "action": "submitted",
        "review": {"state": "approved", "body": "LGTM",
                   "submitted_at": "2026-11-01T12:00:00Z",
                   "user": {"login": "ben"}},
        "pull_request": {"number": 42},
        "repository": {"full_name": "acme/api"},
    }
    parsed = parse_review_event(payload)
    assert parsed["pr_number"] == 42
    assert parsed["review"].state == "APPROVED"
    assert parsed["review"].reviewer_login == "ben"
    payload["action"] = "edited"
    assert parse_review_event(payload) is None


def test_initiative_matching_rules(store, org, repo):
    initiative = store.create_initiative(org.id, "Payments")
    rules = [
        InitiativeRule(None, org.id, initiative.id, "branch_prefix",
                       "feat/payments"),
        InitiativeRule(None, org.id, initiative.id, "title_tag", "PAY"),
        InitiativeRule(None, org.id, initiative.id, "issue_key", "PAY-"),
        InitiativeRule(None, org.id, initiative.id, "repo", "acme/api"),
    ]
    for rule in rules:
        store.add_initiative_rule(rule)
    pr = PullRequest(id=None, org_id=org.id, repo_id=repo.id, number=1,
                     title="Improve checkout", author_login="ada",
                     branch="feat/payments-retry")
    assert match_rule(rules[0], pr)
    assert not match_rule(rules[1], pr)
    pr.title = "[PAY] Improve checkout"
    assert match_rule(rules[1], pr)
    pr.title = "Improve checkout for PAY-123"
    assert match_rule(rules[2], pr)
    assert match_rule(rules[3], pr, "acme/api")
    assert not match_rule(rules[3], pr, "acme/web")
    saved = store.upsert_pull_request(pr)
    assert link_initiatives(store, saved, "acme/api") == [initiative.id]
    assert store.initiatives_for_pr(org.id, saved.id) == [initiative.id]


def test_sync_repo_with_fake_transport(store, org):
    repo = store.upsert_repo(Repo(id=None, org_id=org.id,
                                  full_name="acme/api"))
    transport = FakeGitHubTransport()
    transport.add_repo("acme/api", prs=[{
        "number": 5, "title": "Sync me", "body": "b", "state": "closed",
        "merged_at": "2026-11-05T12:00:00Z",
        "created_at": "2026-11-04T09:00:00Z",
        "closed_at": "2026-11-05T12:00:00Z", "user": {"login": "ada"},
        "head": {"ref": "feat/sync"}, "base": {"ref": "main"},
        "additions": 50, "deletions": 10, "changed_files": 1,
        "files": [{"filename": "src/a.py", "additions": 50,
                   "deletions": 10}],
    }], reviews={5: [{"user": {"login": "ben"}, "state": "approved",
                      "submitted_at": "2026-11-05T10:00:00Z", "body": "ok",
                      "comments_count": 1}]},
        commits={5: [{"sha": "abc123", "author": {"login": "ada"},
                      "commit": {"author": {"date": "2026-11-04T08:00:00Z"},
                                 "message": "start"}}]})
    counts = sync_repo(store, transport, org.id, repo)
    assert counts == {"pull_requests": 1, "reviews": 1, "commits": 1,
                      "line_events": 1}
    pr = store.find_pull_request(org.id, repo.id, 5)
    assert pr.state == "merged" and pr.first_commit_at is not None
    # second sync is idempotent for line events
    counts2 = sync_repo(store, transport, org.id, repo)
    assert counts2["line_events"] == 0
    assert len(store.list_line_events(org.id)) == 1
