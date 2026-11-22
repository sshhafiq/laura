"""ROI engine: spend joins, cost per hour, both value formulas."""

from datetime import timedelta

from laura.models import TokenUsageRecord
from laura.roi import ai_roi
from tests.conftest import BASE, make_pr

CURRENT = (BASE + timedelta(days=30), BASE + timedelta(days=60))
BASELINE = (BASE, BASE + timedelta(days=30))


def _usage(store, org_id, day, engineer, tool, cost):
    store.add_token_usage(TokenUsageRecord(
        id=None, org_id=org_id, day=day, engineer_login=engineer, tool=tool,
        model="m", input_tokens=1000, output_tokens=300, cost_usd=cost))


def test_roi_both_formulas(store, org, repo):
    # baseline month: $100 spend, 10 Laura Hours -> $10/hour
    pr_old = store.upsert_pull_request(make_pr(
        org.id, repo.id, 1, author="ada", days_ago=20))
    store.set_score(org.id, pr_old.id, 10.0, {})
    _usage(store, org.id, BASE + timedelta(days=12), "ada", "cursor", 100.0)
    # current month: $50 spend, 10 Laura Hours -> $5/hour (cheaper!)
    pr_new = store.upsert_pull_request(make_pr(
        org.id, repo.id, 2, author="ada", days_ago=0))
    store.set_score(org.id, pr_new.id, 10.0, {})
    _usage(store, org.id, BASE + timedelta(days=55), "ada", "cursor", 50.0)

    result = ai_roi(store, org.id, CURRENT, BASELINE, blended_rate=100.0)
    assert result.spend_usd == 50.0 and result.laura_hours == 10.0
    assert result.cost_per_laura_hour == 5.0
    assert result.baseline_cost_per_laura_hour == 10.0
    # Output formula: 10h x $100 blended = $1000 value, net $950, ROI 1900%
    assert result.value_output_usd == 1000.0
    assert result.net_savings_usd == 950.0
    assert result.roi_percent == 1900.0
    # Payroll formula: ada used AI in the current period -> all 10h count
    assert result.ai_assisted_hours == 10.0
    assert result.payroll_rate == round(220_000 / 1880, 2)
    assert result.value_payroll_usd == round(10 * result.payroll_rate, 2)
    # per-tool breakdown
    assert result.by_tool["cursor"]["cost_per_laura_hour"] == 5.0
    assert result.by_tool["cursor"]["engineers"] == 1


def test_roi_no_spend_is_safe(store, org, repo):
    result = ai_roi(store, org.id, CURRENT, BASELINE)
    assert result.spend_usd == 0.0 and result.roi_percent is None
    assert result.cost_per_laura_hour is None
