"""The Router: send each coding-agent prompt to the cheapest model
that can plausibly do the job, and prove what that saved.

Commercial routers claim ~40% savings versus always using the biggest
model. Laura makes no claim — it *measures*: every routed session is
written to a ledger with its actual token cost, and the savings report
compares that against the counterfactual where every session ran on the
most expensive model in the table.

Pieces:

- :func:`classify_prompt` — rule-based complexity tiers from prompt
  features (length, code volume, task keywords, file spread). No LLM
  needed to route; the classifier is inspectable and unit-tested.
- :class:`RoutingTable` — tier → model + per-model token prices.
- :class:`RouterSession` — one agent session; when a retry/stall signal
  fires, the session *escalates* one tier instead of burning more
  tokens on a model that is stuck.
- :class:`SessionLedger` — the record; :func:`savings_report` does the
  arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIERS = ("small", "medium", "large")


@dataclass
class ModelPrice:
    model: str
    input_per_mtok: float   # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(input_tokens / 1e6 * self.input_per_mtok
                     + output_tokens / 1e6 * self.output_per_mtok, 6)


#: Default table (public list prices at time of writing; override freely).
DEFAULT_PRICES = {
    "claude-haiku-4.5": ModelPrice("claude-haiku-4.5", 1.00, 5.00),
    "claude-sonnet-4.5": ModelPrice("claude-sonnet-4.5", 3.00, 15.00),
    "claude-opus-4.5": ModelPrice("claude-opus-4.5", 15.00, 75.00),
    "gpt-5-mini": ModelPrice("gpt-5-mini", 0.25, 2.00),
    "gpt-5": ModelPrice("gpt-5", 1.25, 10.00),
}

DEFAULT_ROUTES = {
    "small": "claude-haiku-4.5",
    "medium": "claude-sonnet-4.5",
    "large": "claude-opus-4.5",
}


@dataclass
class RoutingTable:
    routes: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ROUTES))
    prices: dict[str, ModelPrice] = field(
        default_factory=lambda: dict(DEFAULT_PRICES))

    def model_for(self, tier: str) -> ModelPrice:
        return self.prices[self.routes[tier]]

    @property
    def baseline_model(self) -> ModelPrice:
        """The most expensive model in the table — the counterfactual."""
        return max(self.prices.values(),
                   key=lambda p: p.input_per_mtok + p.output_per_mtok)


# ---------------------------------------------------------------------------
# Prompt classification
# ---------------------------------------------------------------------------

_LARGE_KEYWORDS = re.compile(
    r"\b(architect\w*|design|refactor\w*|migrat\w*|debug|root cause|"
    r"race condition|deadlock|security|performance|optimi[sz]e|"
    r"distributed|concurren\w*)\b", re.IGNORECASE)
_SMALL_KEYWORDS = re.compile(
    r"\b(rename|typo|format\w*|comment|docstring|bump|update (the )?version|"
    r"add a test|simple|quick)\b", re.IGNORECASE)
_FILE_MENTION = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|go|rs|java|rb|sql)\b")


@dataclass
class Classification:
    tier: str
    score: int
    reasons: list[str] = field(default_factory=list)


def classify_prompt(prompt: str) -> Classification:
    """Score a prompt's complexity and bucket it into a tier.

    Points: length, embedded code, number of distinct files mentioned,
    and task-class keywords. >= 6 points is ``large``, 3–5 ``medium``,
    below that ``small``. Every point is reported in ``reasons`` so a
    routing decision can always be explained after the fact.
    """
    reasons: list[str] = []
    score = 0
    chars = len(prompt)
    if chars > 4000:
        score += 3
        reasons.append(f"long prompt ({chars} chars)")
    elif chars > 1200:
        score += 2
        reasons.append(f"medium-long prompt ({chars} chars)")
    elif chars > 400:
        score += 1
        reasons.append(f"moderate prompt ({chars} chars)")

    code_blocks = prompt.count("```")
    if code_blocks >= 2:
        score += 2
        reasons.append("contains pasted code blocks")
    files = set(_FILE_MENTION.findall(prompt))
    if len(files) >= 4:
        score += 2
        reasons.append(f"mentions {len(files)} files (cross-file work)")
    elif len(files) >= 2:
        score += 1
        reasons.append(f"mentions {len(files)} files")

    if _LARGE_KEYWORDS.search(prompt):
        score += 3
        reasons.append("task keywords signal deep work "
                       "(design/refactor/debug class)")
    if _SMALL_KEYWORDS.search(prompt) and score < 3:
        score -= 1
        reasons.append("task keywords signal mechanical work")

    tier = "large" if score >= 6 else "medium" if score >= 3 else "small"
    return Classification(tier=tier, score=score, reasons=reasons)


# ---------------------------------------------------------------------------
# Sessions and the ledger
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    session_id: str
    tier: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    baseline_cost_usd: float
    escalated_from: str | None = None


@dataclass
class RouterSession:
    """One coding-agent session under the router's control.

    Call :meth:`record_usage` as the agent works. When the agent stalls
    — a failed attempt, a retry loop, a human saying "it's stuck" —
    call :meth:`signal_stall`: the session escalates one tier and the
    ledger remembers that it did, so escalation cost is visible rather
    than hidden.
    """

    session_id: str
    table: RoutingTable
    ledger: "SessionLedger"
    tier: str
    escalated_from: str | None = None

    @classmethod
    def start(cls, session_id: str, prompt: str, table: RoutingTable,
              ledger: "SessionLedger") -> "RouterSession":
        return cls(session_id=session_id, table=table, ledger=ledger,
                   tier=classify_prompt(prompt).tier)

    @property
    def model(self) -> ModelPrice:
        return self.table.model_for(self.tier)

    def signal_stall(self) -> bool:
        """Escalate one tier. Returns False if already at the top."""
        idx = TIERS.index(self.tier)
        if idx >= len(TIERS) - 1:
            return False
        if self.escalated_from is None:
            self.escalated_from = self.tier
        self.tier = TIERS[idx + 1]
        return True

    def record_usage(self, input_tokens: int,
                     output_tokens: int) -> LedgerEntry:
        price = self.model
        entry = LedgerEntry(
            session_id=self.session_id, tier=self.tier, model=price.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=price.cost(input_tokens, output_tokens),
            baseline_cost_usd=self.table.baseline_model.cost(
                input_tokens, output_tokens),
            escalated_from=self.escalated_from,
        )
        self.ledger.add(entry)
        return entry


@dataclass
class SessionLedger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def add(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)


@dataclass
class SavingsReport:
    sessions: int
    actual_cost_usd: float
    baseline_cost_usd: float
    savings_usd: float
    savings_percent: float | None
    escalations: int


def savings_report(ledger: SessionLedger) -> SavingsReport:
    """Measured savings vs always running the most expensive model."""
    actual = round(sum(e.cost_usd for e in ledger.entries), 4)
    baseline = round(sum(e.baseline_cost_usd for e in ledger.entries), 4)
    savings = round(baseline - actual, 4)
    sessions = len({e.session_id for e in ledger.entries})
    return SavingsReport(
        sessions=sessions,
        actual_cost_usd=actual,
        baseline_cost_usd=baseline,
        savings_usd=savings,
        savings_percent=(round(savings / baseline * 100, 1)
                         if baseline else None),
        escalations=sum(1 for e in ledger.entries if e.escalated_from),
    )
