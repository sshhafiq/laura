"""Store: schema, orgs/keys, multi-tenant isolation, PR round-trips."""

from laura.models import Repo, TokenUsageRecord, User
from laura.store import hash_api_key
from tests.conftest import make_pr


def test_schema_version_and_org_creation(store):
    org, key = store.create_org("Acme", "acme")
    assert store.schema_version == 1
    assert org.id is not None
    assert org.webhook_secret
    # only the hash is stored, and it resolves back to the org
    assert org.api_key_hash == hash_api_key(key)
    assert store.get_org_by_api_key(key).id == org.id
    assert store.get_org_by_api_key("laura_wrong") is None
    assert store.get_org_by_slug("acme").id == org.id


def test_users_and_repos_round_trip(store, org):
    user = store.upsert_user(User(id=None, org_id=org.id, login="ada",
                                  name="Ada", team="platform"))
    again = store.upsert_user(User(id=None, org_id=org.id, login="ada",
                                   name="Ada O.", team="product"))
    assert again.id == user.id and again.team == "product"
    assert store.team_of(org.id, "ada") == "product"
    assert store.team_of(org.id, "ghost") == ""
    repo = store.upsert_repo(Repo(id=None, org_id=org.id,
                                  full_name="acme/api"))
    assert store.get_repo_by_name(org.id, "acme/api").id == repo.id
    assert [r.full_name for r in store.list_repos(org.id)] == ["acme/api"]


def test_pull_request_upsert_and_files(store, org, repo):
    pr = store.upsert_pull_request(make_pr(org.id, repo.id, 7))
    assert pr.id is not None and len(pr.files) == 2
    pr.title = "Renamed"
    updated = store.upsert_pull_request(pr)
    assert updated.id == pr.id and updated.title == "Renamed"
    assert store.find_pull_request(org.id, repo.id, 7).id == pr.id
    assert store.find_pull_request(org.id, repo.id, 99) is None
    store.set_score(org.id, pr.id, 3.25, {"hours": 3.25})
    assert store.get_pull_request(org.id, pr.id).score_hours == 3.25
    assert [p.id for p in store.list_pull_requests(
        org.id, unscored_only=True)] == []
    assert [p.id for p in store.list_pull_requests(
        org.id, state="merged")] == [pr.id]


def test_multi_tenant_isolation(store):
    org_a, _ = store.create_org("A Corp", "a-corp")
    org_b, _ = store.create_org("B Corp", "b-corp")
    repo_a = store.upsert_repo(Repo(id=None, org_id=org_a.id,
                                    full_name="a/api"))
    repo_b = store.upsert_repo(Repo(id=None, org_id=org_b.id,
                                    full_name="b/api"))
    pr_a = store.upsert_pull_request(make_pr(org_a.id, repo_a.id, 1))
    store.upsert_pull_request(make_pr(org_b.id, repo_b.id, 1))
    store.add_token_usage(TokenUsageRecord(
        id=None, org_id=org_a.id, day=pr_a.created_at, engineer_login="ada",
        tool="cursor", input_tokens=10, output_tokens=5, cost_usd=1.0))
    # B sees none of A's data through any list path
    assert [p.id for p in store.list_pull_requests(org_b.id)] != [pr_a.id]
    assert len(store.list_pull_requests(org_b.id)) == 1
    assert store.list_token_usage(org_b.id) == []
    assert len(store.list_token_usage(org_a.id)) == 1
    try:
        store.get_pull_request(org_b.id, pr_a.id)
        raise AssertionError("cross-org PR read should fail")
    except KeyError:
        pass
    try:
        store.get_repo_by_name(org_b.id, "a/api")
        raise AssertionError("cross-org repo read should fail")
    except KeyError:
        pass
