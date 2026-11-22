"""The AI ROI engine.

Joins AI coding-tool token spend (ingested usage records) with output
(Laura Hours on merged PRs) and answers the only question that matters:
**what does an hour of engineering output cost us via AI tools, and is
it cheaper than the alternative?**

Two value formulas, both reported, with every input visible in the
result object — ROI claims you can't audit are marketing, not metrics:

- **Output formula** — value = Laura Hours produced in the period × a
  blended market rate for equivalent contract engineering.
- **Payroll formula** — value = Laura Hours produced by engineers who
  used AI tools × the org's own loaded payroll rate (loaded annual cost
  per engineer ÷ working hours per year). This is the conservative
  "what would this output have cost on payroll" view.

ROI is always measured against a baseline period's cost per Laura Hour:
if AI spend per hour of output fell versus baseline, the difference on
this period's hours is the saving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from laura import metrics

Period = tuple[datetime, datetime]

DEFAULT_BLENDED_RATE = 95.0        # $/hour, contract engineering market
DEFAULT_LOADED_ANNUAL_COST = 220_000.0
DEFAULT_ANNUAL_HOURS = 1880.0


@dataclass
class RoiResult:
    period: Period
    baseline: Period
    spend_usd: float
    baseline_spend_usd: float
    laura_hours: float
    baseline_laura_hours: float
    ai_assisted_hours: float
    cost_per_laura_hour: float | None
    baseline_cost_per_laura_hour: float | None
    # Output formula
    blended_rate: float
    value_output_usd: float
    # Payroll formula
    loaded_annual_cost: float
    annual_hours: float
    payroll_rate: float
    value_payroll_usd: float
    # Headline numbers (Output formula is the headline, Weave-style)
    net_savings_usd: float
    roi_percent: float | None
    by_tool: dict[str, dict] = field(default_factory=dict)


def _spend(store, org_id: int, period: Period,
           tool: str | None = None) -> tuple[float, dict[str, dict]]:
    total = 0.0
    by_tool: dict[str, dict] = {}
    for record in store.list_token_usage(org_id):
        if not (period[0] <= record.day < period[1]):
            continue
        if tool and record.tool != tool:
            continue
        total += record.cost_usd
        entry = by_tool.setdefault(
            record.tool, {"spend_usd": 0.0, "tokens": 0, "engineers": set()})
        entry["spend_usd"] = round(entry["spend_usd"] + record.cost_usd, 2)
        entry["tokens"] += record.total_tokens
        entry["engineers"].add(record.engineer_login)
    for entry in by_tool.values():
        entry["engineers"] = len(entry["engineers"])
    return round(total, 2), by_tool


def _ai_assisted_hours(store, org_id: int, period: Period) -> float:
    """Laura Hours on PRs whose authors used any AI tool in the period."""
    ai_users = {r.engineer_login for r in store.list_token_usage(org_id)
                if period[0] <= r.day < period[1]}
    hours = 0.0
    for pr in store.list_pull_requests(org_id, state="merged"):
        if pr.merged_at and period[0] <= pr.merged_at < period[1] \
                and pr.author_login in ai_users:
            hours += pr.score_hours or 0.0
    return round(hours, 2)


def ai_roi(store, org_id: int, period: Period, baseline: Period, *,
           blended_rate: float = DEFAULT_BLENDED_RATE,
           loaded_annual_cost: float = DEFAULT_LOADED_ANNUAL_COST,
           annual_hours: float = DEFAULT_ANNUAL_HOURS,
           tool: str | None = None) -> RoiResult:
    spend, by_tool = _spend(store, org_id, period, tool)
    baseline_spend, _ = _spend(store, org_id, baseline, tool)
    hours = metrics.throughput(store, org_id, period).laura_hours
    baseline_hours = metrics.throughput(store, org_id, baseline).laura_hours
    assisted = _ai_assisted_hours(store, org_id, period)

    cost_per_hour = round(spend / hours, 2) if hours else None
    baseline_cph = (round(baseline_spend / baseline_hours, 2)
                    if baseline_hours else None)

    payroll_rate = round(loaded_annual_cost / annual_hours, 2)
    value_output = round(hours * blended_rate, 2)
    value_payroll = round(assisted * payroll_rate, 2)
    net = round(value_output - spend, 2)
    roi_pct = round(net / spend * 100, 1) if spend else None

    # Per-tool cost per hour uses org-wide hours for that tool's users.
    for name, entry in by_tool.items():
        tool_hours = 0.0
        users = {r.engineer_login for r in store.list_token_usage(org_id)
                 if r.tool == name and period[0] <= r.day < period[1]}
        for pr in store.list_pull_requests(org_id, state="merged"):
            if pr.merged_at and period[0] <= pr.merged_at < period[1] \
                    and pr.author_login in users:
                tool_hours += pr.score_hours or 0.0
        entry["laura_hours"] = round(tool_hours, 2)
        entry["cost_per_laura_hour"] = (
            round(entry["spend_usd"] / tool_hours, 2) if tool_hours else None)

    return RoiResult(
        period=period, baseline=baseline,
        spend_usd=spend, baseline_spend_usd=baseline_spend,
        laura_hours=hours, baseline_laura_hours=baseline_hours,
        ai_assisted_hours=assisted,
        cost_per_laura_hour=cost_per_hour,
        baseline_cost_per_laura_hour=baseline_cph,
        blended_rate=blended_rate, value_output_usd=value_output,
        loaded_annual_cost=loaded_annual_cost, annual_hours=annual_hours,
        payroll_rate=payroll_rate, value_payroll_usd=value_payroll,
        net_savings_usd=net, roi_percent=roi_pct, by_tool=by_tool,
    )
