"""Ask Laura: every intent returns a sourced answer from real data."""

from laura.ask import ask
from laura.demo import seed_demo


def test_ask_on_demo_data(store):
    seed_demo(store)
    org = store.get_org_by_slug("democo")

    slowest = ask(store, org.id, "which team is slowest to review?")
    assert slowest.intent == "slowest_review"
    assert slowest.sources and "Slowest to review" in slowest.text

    top = ask(store, org.id, "who are the top contributors?")
    assert top.intent == "top_contributors"
    assert "Laura Hours" in top.text

    turnover = ask(store, org.id, "where did turnover spike?")
    assert turnover.intent == "turnover"
    assert "%" in turnover.text

    cost = ask(store, org.id, "what did cursor cost per hour?")
    assert cost.intent == "tool_cost_per_hour"

    cycle = ask(store, org.id, "what is our cycle time?")
    assert cycle.intent == "cycle_time"
    assert "commit→merge" in cycle.text

    dora = ask(store, org.id, "show me the dora metrics")
    assert dora.intent == "dora"


def test_ask_unknown_intent_is_honest(store):
    seed_demo(store)
    org = store.get_org_by_slug("democo")
    answer = ask(store, org.id, "what is the meaning of life?")
    assert answer.intent == "unknown"
    assert "can answer" in answer.text
