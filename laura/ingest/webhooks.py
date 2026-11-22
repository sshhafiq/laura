"""GitHub webhook parsing.

Two pure functions turn raw webhook payloads (dicts, exactly as GitHub
POSTs them) into normalised Laura records, plus an HMAC-SHA256 signature
verifier. Nothing here touches the network or the database, so every
function is trivially testable with fixture payloads.

Supported events:

- ``pull_request`` — opened / edited / closed / reopened / synchronize
- ``pull_request_review`` — submitted reviews (approved, changes
  requested, commented)
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone

from laura.models import PullRequest, Review

_REVERT_RE = re.compile(r"\brevert\b", re.IGNORECASE)
_HOTFIX_RE = re.compile(r"\b(hotfix|hot-fix|urgent fix|emergency fix)\b",
                        re.IGNORECASE)


def verify_signature(secret: str, body: bytes,
                     signature_header: str | None) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` header.

    GitHub sends ``sha256=<hex hmac>`` computed over the raw request body
    with the webhook secret. Comparison is constant-time.
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def detect_revert(title: str, body: str = "") -> bool:
    return bool(_REVERT_RE.search(title or ""))


def detect_hotfix(title: str, branch: str = "") -> bool:
    return bool(_HOTFIX_RE.search(title or "") or
                _HOTFIX_RE.search(branch or ""))


def parse_pull_request_event(payload: dict, *, org_id: int,
                             repo_id: int) -> PullRequest | None:
    """Normalise a ``pull_request`` webhook payload.

    Returns ``None`` for actions Laura does not track (label changes,
    assignments, …) so callers can simply skip them.
    """
    action = payload.get("action", "")
    if action not in {"opened", "edited", "closed", "reopened",
                      "synchronize"}:
        return None
    pr = payload.get("pull_request") or {}
    if not pr:
        return None
    merged = bool(pr.get("merged"))
    state = "merged" if merged else (pr.get("state") or "open")
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return PullRequest(
        id=None,
        org_id=org_id,
        repo_id=repo_id,
        number=int(pr["number"]),
        title=title,
        author_login=(pr.get("user") or {}).get("login", ""),
        branch=head.get("ref", ""),
        base_branch=base.get("ref", "main"),
        body=body,
        state=state,
        created_at=_ts(pr.get("created_at")),
        merged_at=_ts(pr.get("merged_at")),
        closed_at=_ts(pr.get("closed_at")),
        additions=int(pr.get("additions") or 0),
        deletions=int(pr.get("deletions") or 0),
        changed_files=int(pr.get("changed_files") or 0),
        is_revert=detect_revert(title, body),
        is_hotfix=detect_hotfix(title, head.get("ref", "")),
    )


def parse_review_event(payload: dict) -> dict | None:
    """Normalise a ``pull_request_review`` payload.

    Returns a plain dict (``pr_number``, ``repo_full_name`` plus the
    review fields) because a Review row needs the internal PR id, which
    only the store knows; the caller resolves it. Returns ``None`` for
    non-submitted actions (edits, dismissals).
    """
    if payload.get("action") != "submitted":
        return None
    review = payload.get("review") or {}
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    if not review or not pr:
        return None
    state_map = {
        "approved": "APPROVED",
        "changes_requested": "CHANGES_REQUESTED",
        "commented": "COMMENTED",
    }
    state = state_map.get((review.get("state") or "").lower())
    if state is None:
        return None
    return {
        "pr_number": int(pr["number"]),
        "repo_full_name": repo.get("full_name", ""),
        "review": Review(
            id=None,
            org_id=0,  # caller fills in
            pr_id=0,   # caller fills in
            reviewer_login=(review.get("user") or {}).get("login", ""),
            state=state,
            submitted_at=_ts(review.get("submitted_at")),
            body=review.get("body") or "",
            comments_count=0,  # webhooks don't carry inline comment counts
        ),
    }
