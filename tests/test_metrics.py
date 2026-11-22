"""Metrics engine on fixture data: cycle time, throughput, DORA,
turnover windows, review load."""

from datetime import timedelta

from laura import metrics
from laura.models import LineEvent, PRFile, User
from tests.conftest import BASE, add_review, make_pr

PERIOD = (BASE, BASE + timedelta(days=60))


def _seed_two_prs(store, org, repo):
    store.upsert_user(User(id=None, org_id=org.id, login="ada",
                           team="platform"))
    store.upsert_user(User(id=None, org_id=org.id, login="dee",
                           team="product"))
    pr1 = store.upsert_pull_request(make_pr(org.id, repo.id, 1, author="ada"))
    store.set_score(org.id, pr1.id, 4.0, {})
    add_review(store, org.id, pr1, reviewer="dee", hours_after=8)
    pr2 = store.upsert_pull_request(make_pr(
        org.id, repo.id, 2, author="dee", days_ago=5, hotfix=True))
    store.set_score(org.id, pr2.id, 2.0, {})
    add_review(store, org.id, pr2, reviewer="ada", hours_after=4)
    return pr1, pr2


def test_cycle_time_stages(store, org, repo):
    _seed_two_prs(store, org, repo)
    ct = metrics.cycle_time(store, org.id, PERIOD)
    assert ct.sample_size == 2
    # first commit is 5h before creation; first reviews at +8h / +4h
    assert ct.commit_to_review_h == 11.0  # median of 13 and 9
    assert ct.commit_to_merge_h == 35.0   # 30h after creation + 5h


def test_throughput_by_team_and_engineer(store, org, repo):
    _seed_two_prs(store, org, repo)
    tp = metrics.throughput(store, org.id, PERIOD)
    assert tp.merged_prs == 2 and tp.laura_hours == 6.0
    assert tp.by_engineer["ada"] == {"prs": 1, "laura_hours": 4.0}
    assert tp.by_team["platform"]["laura_hours"] == 4.0
    assert tp.by_team["product"]["laura_hours"] == 2.0


def test_dora_metrics(store, org, repo):
    _seed_two_prs(store, org, repo)
    # one revert among three merges -> CFR 1/3
    pr3 = store.upsert_pull_request(make_pr(
        org.id, repo.id, 3, author="ada", days_ago=3, revert=True))
    store.set_score(org.id, pr3.id, 1.0, {})
    d = metrics.dora(store, org.id, PERIOD)
    assert d.merges == 3 and d.failures == 2  # revert + hotfix
    assert d.change_failure_rate == round(2 / 3, 4)
    assert d.deployments_per_week > 0
    assert d.lead_time_h == 35.0
    assert d.mttr_proxy_h == 30.0  # hotfix created->merged


def test_code_turnover_window(store, org, repo):
    day0 = BASE + timedelta(days=10)
    store.add_line_event(LineEvent(None, org.id, repo.id, "src/a.py",
                                   day0, 100))
    # 40 of those lines rewritten 55 days later: inside the 90d window
    # but outside the 60d reporting period, so it is not itself a cohort
    store.add_line_event(LineEvent(None, org.id, repo.id, "src/a.py",
                                   day0 + timedelta(days=55), 40))
    # another file changed once, never again
    store.add_line_event(LineEvent(None, org.id, repo.id, "src/b.py",
                                   day0, 50))
    # a rewrite 120 days later is OUTSIDE the window for the day0 cohort
    store.add_line_event(LineEvent(None, org.id, repo.id, "src/b.py",
                                   day0 + timedelta(days=120), 50))
    result = metrics.code_turnover(store, org.id, PERIOD)
    assert result.cohort_lines == 150
    assert result.rechurned_lines == 40
    assert result.turnover_rate == round(40 / 150, 4)
    assert result.by_repo["acme/api"]["turnover_rate"] == round(40 / 150, 4)


def test_review_stats_slowest_team(store, org, repo):
    _seed_two_prs(store, org, repo)
    stats = metrics.review_stats(store, org.id, PERIOD)
    # dee (product) reviewed at +8h, ada (platform) at +4h
    assert stats.team_turnaround_h["product"] == 8.0
    assert stats.slowest_team == "product"
    assert stats.reviews_per_reviewer == {"dee": 1, "ada": 1}
    assert stats.median_turnaround_h == 6.0
