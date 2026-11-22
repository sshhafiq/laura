"""Scorer math, reasoning sums, review scoring, calibration."""

from datetime import timedelta

from laura.models import PRFile, Review
from laura.scoring import (HeuristicScorer, StubLLMScorer, business_hours,
                           calibrate, classify_file, score_review)
from tests.conftest import BASE, make_pr

scorer = HeuristicScorer()


def _pr(**kw):
    return make_pr(1, 1, 1, **kw)


def test_reasoning_factors_sum_to_score():
    result = scorer.score(_pr(churn=600), multiplier=1.3)
    raw = sum(f.hours for f in result.factors)
    assert abs(raw * 1.3 - result.hours) < 0.011
    assert result.multiplier == 1.3
    as_dict = result.to_dict()
    assert as_dict["hours"] == result.hours and as_dict["factors"]


def test_bigger_pr_scores_more_than_smaller():
    small = scorer.score(_pr(churn=40)).hours
    big = scorer.score(_pr(churn=1500)).hours
    assert 0 < small < big


def test_generated_files_are_cheap():
    gen = _pr(files=[PRFile("src/generated/api_pb2.py", 900, 100)])
    real = _pr(files=[PRFile("src/api/core.py", 900, 100)])
    assert scorer.score(gen).hours < scorer.score(real).hours * 0.5


def test_risky_title_adds_factor_and_spread_counts():
    pr = _pr(title="Migration: split the users table", files=[
        PRFile("src/a/x.py", 100, 0), PRFile("web/b/y.ts", 100, 0),
        PRFile("db/c/z.sql", 100, 0)])
    names = [f.name for f in scorer.score(pr).factors]
    assert "risk_surface" in names and "cross_file_coupling" in names
    assert "file_spread" in names


def test_classify_file_kinds():
    assert classify_file("tests/test_api.py") == "test"
    assert classify_file("src/api.py") == "source"
    assert classify_file("docs/guide.md") == "docs"
    assert classify_file("config/app.yaml") == "config"
    assert classify_file("web/yarn.lock") == "generated"
    assert classify_file("src/api.test.ts") == "test"


def test_stub_llm_scorer_refuses_without_client():
    try:
        StubLLMScorer().score(_pr())
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "no LLM client" in str(exc)


def test_stub_llm_scorer_delegates_with_client():
    class FakeClient:
        def estimate_hours(self, summary):
            from laura.scoring import Factor, ScoreResult
            assert summary["additions"] > 0
            return ScoreResult(hours=9.0, factors=[
                Factor("llm", 9.0, "model said so")])

    result = StubLLMScorer(FakeClient()).score(_pr())
    assert result.hours == 9.0


def test_review_scoring_rubber_stamp_and_thorough():
    big = _pr(churn=900)
    fast = Review(id=None, org_id=1, pr_id=1, reviewer_login="ben",
                  state="APPROVED",
                  submitted_at=big.created_at + timedelta(minutes=3),
                  body="", comments_count=0)
    scored = score_review(fast, big, pr_opened_at=big.created_at)
    assert scored.quality == "rubber_stamp_risk"
    deep = Review(id=None, org_id=1, pr_id=1, reviewer_login="ben",
                  state="CHANGES_REQUESTED",
                  submitted_at=big.created_at + timedelta(hours=4),
                  body="x" * 220, comments_count=5)
    scored = score_review(deep, big, pr_opened_at=big.created_at)
    assert scored.quality == "thorough"
    assert scored.effort_hours > 0.5


def test_business_hours_skips_weekends():
    friday = BASE + timedelta(days=1)  # 2026-10-02 is a Friday
    assert friday.weekday() == 4
    monday = friday + timedelta(days=3)
    # Fri 09:00 -> Mon 09:00 spans exactly one business day of 09-18 = 9h
    assert business_hours(friday, monday) == 9.0


def test_calibration_learns_multiplier(store, org, repo):
    # PRs whose raw score is ~1h but which took ~10 business hours
    for n in range(6):
        pr = make_pr(org.id, repo.id, 100 + n, churn=30,
                     days_ago=20 - n)
        pr.first_commit_at = pr.created_at - timedelta(hours=1)
        pr.merged_at = pr.created_at + timedelta(days=2)  # ~2 business days
        store.upsert_pull_request(pr)
    learned = calibrate(store, org.id)
    assert learned > 1.5  # actual >> raw, multiplier grows
    assert store.get_org(org.id).calibration_multiplier == learned


def test_calibration_keeps_multiplier_without_samples(store, org, repo):
    store.set_calibration(org.id, 1.7)
    assert calibrate(store, org.id) == 1.7
