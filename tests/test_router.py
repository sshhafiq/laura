"""Router: classification tiers, escalation, measured savings."""

from laura.router import (RoutingTable, RouterSession, SessionLedger,
                          classify_prompt, savings_report)


def test_classify_small_medium_large():
    small = classify_prompt("rename the variable foo to bar")
    assert small.tier == "small"
    medium = classify_prompt(
        "Update the search endpoint in src/api/search.py and "
        "src/api/routes.py to accept a new filter parameter, like:\n"
        "```python\ndef search(q, filters=None): ...\n```\n"
        "and adjust the response shape accordingly for the dashboard")
    assert medium.tier == "medium"
    large = classify_prompt(
        "Design and refactor the distributed session layer: debug the "
        "race condition across src/api/session.py, src/core/cache.py, "
        "src/core/db.py and src/api/auth.py, then migrate the schema. "
        "The current implementation loses sessions under load, the fix "
        "must preserve the existing public API, keep backward "
        "compatibility for in-flight requests, and include a careful "
        "rollout plan with metrics at every step of the migration.")
    assert large.tier == "large"
    assert large.reasons  # every decision is explainable


def test_session_escalates_on_stall():
    table = RoutingTable()
    ledger = SessionLedger()
    session = RouterSession.start("s1", "rename a variable", table, ledger)
    assert session.tier == "small"
    assert session.signal_stall() is True and session.tier == "medium"
    assert session.signal_stall() is True and session.tier == "large"
    assert session.signal_stall() is False  # already at the top


def test_ledger_measures_savings_vs_baseline():
    table = RoutingTable()
    ledger = SessionLedger()
    cheap = RouterSession.start("cheap", "fix a typo in the docs",
                                table, ledger)
    cheap.record_usage(100_000, 20_000)
    pricey = RouterSession.start(
        "pricey", "architect a distributed migration with debugging of "
                  "race conditions across many files", table, ledger)
    pricey.record_usage(100_000, 20_000)
    report = savings_report(ledger)
    assert report.sessions == 2
    assert report.actual_cost_usd < report.baseline_cost_usd
    assert report.savings_usd > 0
    assert 0 < report.savings_percent < 100
    # the cheap session drives the savings: haiku vs opus pricing
    cheap_entry = ledger.entries[0]
    assert cheap_entry.model == "claude-haiku-4.5"
    assert cheap_entry.cost_usd < cheap_entry.baseline_cost_usd


def test_escalation_is_visible_in_ledger():
    table = RoutingTable()
    ledger = SessionLedger()
    session = RouterSession.start("s9", "simple rename", table, ledger)
    session.signal_stall()
    entry = session.record_usage(1_000, 500)
    assert entry.escalated_from == "small" and entry.tier == "medium"
    assert savings_report(ledger).escalations == 1
