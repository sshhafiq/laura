"""Ask Laura: a deterministic investigation layer over the store.

Commercial tools ship an "investigation agent" that answers questions
about your engineering data. Laura's version is deliberately *not* a
fake LLM: a small intent parser maps a question to a real query over
the metrics engine, and every answer carries its sources — the period,
the metric values, and the PR numbers behind them — so an answer can
always be checked. If a question matches no intent, Laura says so and
lists what it can answer.

Supported intents:

- "which team is slowest to review" / "slowest review"
- "top contributors" (by Laura Hours in a period)
- "where did turnover spike" / "turnover"
- "what did <tool> cost per hour" / "cost per hour"
- "cycle time" / "how long does a PR take"
- "dora" / "deployment frequency" / "change failure"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from laura import metrics, roi as roi_module

Period = tuple[datetime, datetime]


@dataclass
class Answer:
    question: str
    text: str
    intent: str
    sources: list[dict] = field(default_factory=list)


def _default_period() -> Period:
    end = datetime.now(timezone.utc)
    return (end - timedelta(days=90), end)


def _month_periods() -> tuple[Period, Period]:
    """(last full-ish month, the month before) for ROI questions."""
    end = datetime.now(timezone.utc)
    return ((end - timedelta(days=30), end),
            (end - timedelta(days=60), end - timedelta(days=30)))


_TOOL_COST_RE = re.compile(
    r"what did ([\w\- ]+?) cost|cost per (?:laura )?hour|"
    r"([\w\-]+) cost per hour", re.IGNORECASE)

KNOWN_TOOLS = ("claude-code", "claude code", "cursor", "copilot", "codex")


def ask(store, org_id: int, question: str, *,
        period: Period | None = None) -> Answer:
    """Answer ``question`` for ``org_id`` with sourced, real numbers."""
    period = period or _default_period()
    q = question.lower()

    # -- slowest to review ------------------------------------------------
    if "slowest" in q and "review" in q or "review turnaround" in q:
        stats = metrics.review_stats(store, org_id, period)
        if stats.slowest_team is None:
            text = "No reviews in the period, so there is no slowest team."
        else:
            text = (f"Slowest to review: {stats.slowest_team} "
                    f"(median first-review turnaround "
                    f"{stats.team_turnaround_h[stats.slowest_team]}h). "
                    f"Org median: {stats.median_turnaround_h}h.")
        return Answer(question, text, "slowest_review",
                      [{"metric": "review_turnaround_by_team",
                        "period": [period[0].isoformat(),
                                   period[1].isoformat()],
                        "values": stats.team_turnaround_h}])

    # -- top contributors ---------------------------------------------------
    if "top contributor" in q or "most laura hours" in q \
            or "who shipped" in q:
        tp = metrics.throughput(store, org_id, period)
        ranked = sorted(tp.by_engineer.items(),
                        key=lambda kv: kv[1]["laura_hours"], reverse=True)
        top = ranked[:5]
        text = "Top contributors by Laura Hours: " + ", ".join(
            f"{login} ({v['laura_hours']}h across {v['prs']} PRs)"
            for login, v in top) if top else "No merged PRs in the period."
        return Answer(question, text, "top_contributors",
                      [{"metric": "throughput.by_engineer",
                        "period": [period[0].isoformat(),
                                   period[1].isoformat()],
                        "values": dict(top)}])

    # -- turnover -----------------------------------------------------------
    if "turnover" in q or "rewritten" in q or "didn't stick" in q:
        result = metrics.code_turnover(store, org_id, period)
        worst = None
        for name, entry in result.by_repo.items():
            if entry["turnover_rate"] is not None and (
                    worst is None
                    or entry["turnover_rate"] > result.by_repo[worst][
                        "turnover_rate"]):
                worst = name
        if result.turnover_rate is None:
            text = "No line-change history in the period."
        else:
            text = (f"Code turnover: {result.turnover_rate:.1%} of lines "
                    f"changed in the period were changed again within "
                    f"90 days ({result.rechurned_lines} of "
                    f"{result.cohort_lines} lines). Worst repo: {worst} "
                    f"({result.by_repo[worst]['turnover_rate']:.1%}).")
        return Answer(question, text, "turnover",
                      [{"metric": "code_turnover",
                        "period": [period[0].isoformat(),
                                   period[1].isoformat()],
                        "values": {"rate": result.turnover_rate,
                                   "by_repo": result.by_repo}}])

    # -- tool cost per hour ---------------------------------------------------
    if "cost per" in q or _TOOL_COST_RE.search(q):
        tool = next((t for t in KNOWN_TOOLS if t in q), None)
        tool_key = tool.replace(" ", "-") if tool else None
        current, baseline = _month_periods()
        result = roi_module.ai_roi(store, org_id, current, baseline,
                                   tool=tool_key)
        label = tool or "all AI tools"
        if result.cost_per_laura_hour is None:
            text = (f"No scored output in the last 30 days to divide "
                    f"{label} spend by (spend ${result.spend_usd}).")
        else:
            text = (f"{label} cost per Laura Hour (last 30 days): "
                    f"${result.cost_per_laura_hour} "
                    f"(${result.spend_usd} spend / {result.laura_hours} "
                    f"Laura Hours). Baseline month: "
                    f"${result.baseline_cost_per_laura_hour}.")
        return Answer(question, text, "tool_cost_per_hour",
                      [{"metric": "roi.cost_per_laura_hour", "tool": label,
                        "values": {
                            "spend_usd": result.spend_usd,
                            "laura_hours": result.laura_hours,
                            "cost_per_laura_hour":
                                result.cost_per_laura_hour}}])

    # -- cycle time -----------------------------------------------------------
    if "cycle time" in q or "how long" in q:
        ct = metrics.cycle_time(store, org_id, period)
        text = (f"Cycle time medians over {ct.sample_size} merged PRs: "
                f"commit→first review {ct.commit_to_review_h}h, "
                f"review→approval {ct.review_to_approval_h}h, "
                f"approval→merge {ct.approval_to_merge_h}h, "
                f"commit→merge total {ct.commit_to_merge_h}h.")
        return Answer(question, text, "cycle_time",
                      [{"metric": "cycle_time",
                        "period": [period[0].isoformat(),
                                   period[1].isoformat()],
                        "values": ct.__dict__}])

    # -- DORA -------------------------------------------------------------------
    if "dora" in q or "deployment" in q or "change failure" in q:
        d = metrics.dora(store, org_id, period)
        text = (f"DORA: {d.deployments_per_week} deployments/week "
                f"(merges to default branch), lead time {d.lead_time_h}h, "
                f"change-failure rate {d.change_failure_rate}, MTTR "
                f"proxy {d.mttr_proxy_h}h, over {d.merges} merges "
                f"({d.failures} failures).")
        return Answer(question, text, "dora",
                      [{"metric": "dora",
                        "period": [period[0].isoformat(),
                                   period[1].isoformat()],
                        "values": d.__dict__}])

    return Answer(
        question,
        "I don't know how to answer that yet. I can answer: which team "
        "is slowest to review, top contributors, where turnover spiked, "
        "what a tool cost per Laura Hour, cycle time, and DORA.",
        "unknown", [])
