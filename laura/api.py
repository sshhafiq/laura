"""HTTP API: stdlib ``http.server`` only, no web framework dependency.

The request handler is a pure function — :func:`handle_request` takes a
store, method, path, headers and body and returns ``(status, dict)`` —
so the entire API is testable in-process without opening a socket.
:func:`serve` is a thin adapter that wires it to ``http.server``.

Auth: every endpoint except ``POST /orgs`` and ``GET /health`` needs
``X-API-Key: <key>``; the key identifies the org and *all* data access
is scoped to it. The GitHub webhook endpoint instead verifies the
``X-Hub-Signature-256`` HMAC against the org's webhook secret (the org
is identified by slug in the path).

Endpoints:

- ``POST /orgs``                       create org → {org, api_key, webhook_secret}
- ``POST /repos``                      register a repo for the caller's org
- ``POST /webhooks/github/{slug}``     GitHub webhook receiver
- ``POST /tokens/import``              body = token-usage CSV text
- ``GET  /metrics/summary``            cycle time + throughput + DORA + turnover
- ``GET  /roi``                        AI ROI (last 30d vs prior 30d by default)
- ``GET  /ask?q=...``                  Ask Laura investigation answer
- ``GET  /health``                     liveness
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from laura import ask as ask_module
from laura import metrics
from laura.ingest.initiatives import link_initiatives
from laura.ingest.tokens_csv import import_token_csv
from laura.ingest.webhooks import (parse_pull_request_event,
                                   parse_review_event, verify_signature)
from laura.models import Repo
from laura.roi import ai_roi


def _period(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return (end - timedelta(days=days), end)


def _auth(store, headers: dict):
    key = headers.get("x-api-key") or headers.get("X-API-Key")
    if not key:
        return None
    return store.get_org_by_api_key(key)


def handle_request(store, method: str, raw_path: str, headers: dict,
                   body: bytes) -> tuple[int, dict]:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    if method == "POST" and path == "/orgs":
        try:
            data = json.loads(body or b"{}")
            org, key = store.create_org(data["name"], data["slug"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            return 400, {"error": f"invalid org payload: {exc}"}
        return 201, {"org_id": org.id, "slug": org.slug, "api_key": key,
                     "webhook_secret": org.webhook_secret}

    if method == "POST" and path.startswith("/webhooks/github/"):
        slug = path.rsplit("/", 1)[-1]
        org = store.get_org_by_slug(slug)
        if org is None:
            return 404, {"error": "unknown org slug"}
        signature = headers.get("x-hub-signature-256") or \
            headers.get("X-Hub-Signature-256")
        if not verify_signature(org.webhook_secret, body, signature):
            return 401, {"error": "bad webhook signature"}
        event = headers.get("x-github-event") or headers.get("X-GitHub-Event")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, {"error": "invalid JSON"}
        return _handle_webhook(store, org, event, payload)

    org = _auth(store, headers)
    if org is None:
        return 401, {"error": "missing or invalid X-API-Key"}

    if method == "POST" and path == "/repos":
        try:
            data = json.loads(body or b"{}")
            repo = store.upsert_repo(Repo(
                id=None, org_id=org.id, full_name=data["full_name"],
                default_branch=data.get("default_branch", "main")))
        except (KeyError, json.JSONDecodeError) as exc:
            return 400, {"error": f"invalid repo payload: {exc}"}
        return 201, {"repo_id": repo.id, "full_name": repo.full_name}

    if method == "POST" and path == "/tokens/import":
        result = import_token_csv(store, org.id, body.decode("utf-8"))
        status = 200 if result.ok else 422
        return status, {"imported": result.imported,
                        "errors": result.errors}

    if method == "GET" and path == "/metrics/summary":
        days = int(query.get("days", ["90"])[0])
        period = _period(days)
        ct = metrics.cycle_time(store, org.id, period)
        tp = metrics.throughput(store, org.id, period)
        dora = metrics.dora(store, org.id, period)
        turnover = metrics.code_turnover(store, org.id, period)
        reviews = metrics.review_stats(store, org.id, period)
        return 200, {
            "period_days": days,
            "cycle_time": ct.__dict__,
            "throughput": {"merged_prs": tp.merged_prs,
                           "laura_hours": tp.laura_hours,
                           "by_team": tp.by_team,
                           "by_engineer": tp.by_engineer},
            "dora": dora.__dict__,
            "turnover": {"rate": turnover.turnover_rate,
                         "cohort_lines": turnover.cohort_lines,
                         "rechurned_lines": turnover.rechurned_lines,
                         "by_repo": turnover.by_repo},
            "reviews": {"median_turnaround_h":
                        reviews.median_turnaround_h,
                        "reviews_per_reviewer":
                        reviews.reviews_per_reviewer,
                        "slowest_team": reviews.slowest_team},
        }

    if method == "GET" and path == "/roi":
        current = _period(30)
        baseline = (current[0] - timedelta(days=30), current[0])
        result = ai_roi(store, org.id, current, baseline)
        return 200, {
            "spend_usd": result.spend_usd,
            "laura_hours": result.laura_hours,
            "cost_per_laura_hour": result.cost_per_laura_hour,
            "baseline_cost_per_laura_hour":
                result.baseline_cost_per_laura_hour,
            "value_output_usd": result.value_output_usd,
            "value_payroll_usd": result.value_payroll_usd,
            "net_savings_usd": result.net_savings_usd,
            "roi_percent": result.roi_percent,
            "rates": {"blended_rate": result.blended_rate,
                      "payroll_rate": result.payroll_rate},
            "by_tool": result.by_tool,
        }

    if method == "GET" and path == "/ask":
        question = query.get("q", [""])[0]
        if not question:
            return 400, {"error": "missing q parameter"}
        answer = ask_module.ask(store, org.id, question)
        return 200, {"question": answer.question, "answer": answer.text,
                     "intent": answer.intent, "sources": answer.sources}

    return 404, {"error": "not found"}


def _handle_webhook(store, org, event: str | None,
                    payload: dict) -> tuple[int, dict]:
    repo_name = (payload.get("repository") or {}).get("full_name", "")
    try:
        repo = store.get_repo_by_name(org.id, repo_name)
    except KeyError:
        repo = store.upsert_repo(Repo(id=None, org_id=org.id,
                                      full_name=repo_name))
    if event == "pull_request":
        pr = parse_pull_request_event(payload, org_id=org.id,
                                      repo_id=repo.id)
        if pr is None:
            return 200, {"ignored": True}
        existing = store.find_pull_request(org.id, repo.id, pr.number)
        if existing is not None:
            pr.first_commit_at = existing.first_commit_at
            if not pr.files:  # webhook payloads carry no per-file detail
                pr.files = existing.files
        saved = store.upsert_pull_request(pr)
        if saved.state == "merged" and (
                existing is None or existing.state != "merged"):
            store.record_merge_line_events(saved)
        link_initiatives(store, saved, repo.full_name)
        return 200, {"pr_id": saved.id, "number": saved.number}
    if event == "pull_request_review":
        parsed = parse_review_event(payload)
        if parsed is None:
            return 200, {"ignored": True}
        pr = store.find_pull_request(org.id, repo.id, parsed["pr_number"])
        if pr is None:
            return 200, {"ignored": True, "reason": "unknown PR"}
        review = parsed["review"]
        review.org_id = org.id
        review.pr_id = pr.id
        store.add_review(review)
        return 200, {"review_recorded": True}
    return 200, {"ignored": True, "reason": f"unhandled event {event}"}


def serve(store, host: str = "127.0.0.1", port: int = 8730) -> None:
    """Run the API server (blocking)."""

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k: v for k, v in self.headers.items()}
            status, payload = handle_request(store, method, self.path,
                                             headers, body)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 (http.server naming)
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *args):  # keep stdout clean
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()
