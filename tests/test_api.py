"""API via in-process handle_request calls — no sockets needed."""

import hashlib
import hmac
import json

from laura.api import handle_request


def _call(store, method, path, headers=None, body=b""):
    return handle_request(store, method, path, headers or {}, body)


def test_health_and_org_creation(store):
    status, payload = _call(store, "GET", "/health")
    assert status == 200 and payload["status"] == "ok"
    status, payload = _call(store, "POST", "/orgs", body=json.dumps(
        {"name": "Acme", "slug": "acme"}).encode())
    assert status == 201
    assert payload["api_key"].startswith("laura_")
    assert payload["webhook_secret"]


def test_auth_required_and_scoping(store):
    _, org_a = _call(store, "POST", "/orgs", body=json.dumps(
        {"name": "A", "slug": "a"}).encode())
    _, org_b = _call(store, "POST", "/orgs", body=json.dumps(
        {"name": "B", "slug": "b"}).encode())
    status, _ = _call(store, "GET", "/metrics/summary")
    assert status == 401
    status, _ = _call(store, "GET", "/metrics/summary",
                      {"X-API-Key": "laura_nope"})
    assert status == 401
    # register a repo under A; B's metrics stay empty of A's throughput
    status, payload = _call(
        store, "POST", "/repos", {"X-API-Key": org_a["api_key"]},
        json.dumps({"full_name": "a/api"}).encode())
    assert status == 201
    status, payload = _call(store, "GET", "/metrics/summary",
                            {"X-API-Key": org_b["api_key"]})
    assert status == 200
    assert payload["throughput"]["merged_prs"] == 0


def test_webhook_flow_with_signature(store):
    _, org = _call(store, "POST", "/orgs", body=json.dumps(
        {"name": "Acme", "slug": "acme"}).encode())
    _call(store, "POST", "/repos", {"X-API-Key": org["api_key"]},
          json.dumps({"full_name": "acme/api"}).encode())
    body = json.dumps({
        "action": "opened",
        "pull_request": {
            "number": 3, "title": "Webhook PR", "body": "via webhook",
            "state": "open", "merged": False,
            "created_at": "2026-11-20T10:00:00Z", "merged_at": None,
            "closed_at": None, "additions": 10, "deletions": 2,
            "changed_files": 1, "user": {"login": "ada"},
            "head": {"ref": "feat/wh"}, "base": {"ref": "main"}},
        "repository": {"full_name": "acme/api"}}).encode()
    headers = {"X-GitHub-Event": "pull_request"}
    # unsigned -> rejected
    status, _ = _call(store, "POST", "/webhooks/github/acme", headers, body)
    assert status == 401
    sig = "sha256=" + hmac.new(org["webhook_secret"].encode(), body,
                               hashlib.sha256).hexdigest()
    headers["X-Hub-Signature-256"] = sig
    status, payload = _call(store, "POST", "/webhooks/github/acme",
                            headers, body)
    assert status == 200 and payload["number"] == 3
    saved = store.get_org_by_slug("acme")
    assert len(store.list_pull_requests(saved.id)) == 1


def test_tokens_import_and_ask_endpoints(store):
    _, org = _call(store, "POST", "/orgs", body=json.dumps(
        {"name": "Acme", "slug": "acme"}).encode())
    csv_body = ("date,engineer,tool,model,input_tokens,output_tokens,"
                "cost_usd\n2026-11-01,ada,cursor,m,100,50,0.25\n").encode()
    status, payload = _call(store, "POST", "/tokens/import",
                            {"X-API-Key": org["api_key"]}, csv_body)
    assert status == 200 and payload["imported"] == 1
    status, payload = _call(store, "GET", "/ask?q=what+is+our+cycle+time",
                            {"X-API-Key": org["api_key"]})
    assert status == 200 and payload["intent"] == "cycle_time"
    status, payload = _call(store, "GET", "/roi",
                            {"X-API-Key": org["api_key"]})
    assert status == 200 and payload["spend_usd"] == 0.25
