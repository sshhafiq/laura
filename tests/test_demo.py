"""Demo seed integrity: the quickstart data must be complete and sane."""

from laura.demo import seed_demo
from laura.scoring import HeuristicScorer


def test_demo_seed(store):
    summary = seed_demo(store)
    assert summary["users"] == 6 and summary["repos"] == 2
    assert summary["pull_requests"] >= 20
    assert summary["reviews"] >= 20 and summary["commits"] >= 20
    assert summary["token_rows"] > 100
    org = store.get_org_by_slug("democo")
    prs = store.list_pull_requests(org.id)
    # every demo PR is scored, with a reasoning breakdown attached
    assert all(pr.score_hours is not None for pr in prs)
    assert all(pr.score_reasoning and pr.score_reasoning["factors"]
               for pr in prs)
    assert all(pr.state == "merged" for pr in prs)
    # the payments initiative picked up its PRs
    initiatives = store.list_initiatives(org.id)
    assert [i.name for i in initiatives] == ["Payments reliability"]
    linked = [pr for pr in prs
              if store.initiatives_for_pr(org.id, pr.id)]
    assert linked, "expected at least one PR linked to the initiative"
    # line events exist for turnover queries
    assert store.list_line_events(org.id)


def test_demo_seed_is_idempotent(store):
    first = seed_demo(store)
    second = seed_demo(store)
    assert second.get("already_seeded") is True
    assert second["org_id"] == first["org_id"]


def test_demo_scores_are_deterministic(store):
    seed_demo(store)
    org = store.get_org_by_slug("democo")
    scorer = HeuristicScorer()
    for pr in store.list_pull_requests(org.id):
        fresh = scorer.score(pr, multiplier=1.0)
        assert abs(fresh.hours - pr.score_hours) < 0.011
