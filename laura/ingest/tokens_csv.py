"""Token-usage CSV importer.

Expected columns (header row required)::

    date, engineer, tool, model, input_tokens, output_tokens, cost_usd

This is the shape AI coding-tool admin exports (Claude Code, Cursor,
Copilot, Codex) can be normalised into. Import is validated row by row:
bad rows are collected as errors with line numbers and *skipped* — one
typo never blocks a whole month of usage data. Valid rows land in the
store as :class:`TokenUsageRecord` rows scoped to the org.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

from laura.models import TokenUsageRecord

REQUIRED_COLUMNS = ["date", "engineer", "tool", "model", "input_tokens",
                    "output_tokens", "cost_usd"]


@dataclass
class ImportResult:
    imported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_day(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def import_token_csv(store, org_id: int, text: str) -> ImportResult:
    """Import CSV text into the store for ``org_id``."""
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        result.errors.append("empty CSV: no header row")
        return result
    header = [h.strip() for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result.errors.append(
            f"missing required columns: {', '.join(missing)}")
        return result
    for lineno, row in enumerate(reader, start=2):
        try:
            day = _parse_day(row["date"])
            engineer = (row["engineer"] or "").strip()
            tool = (row["tool"] or "").strip()
            if not engineer or not tool:
                raise ValueError("engineer and tool must be non-empty")
            input_tokens = int(row["input_tokens"])
            output_tokens = int(row["output_tokens"])
            cost = float(row["cost_usd"])
            if input_tokens < 0 or output_tokens < 0 or cost < 0:
                raise ValueError("tokens and cost must be non-negative")
        except (ValueError, TypeError, KeyError) as exc:
            result.errors.append(f"line {lineno}: {exc}")
            continue
        store.add_token_usage(TokenUsageRecord(
            id=None, org_id=org_id, day=day, engineer_login=engineer,
            tool=tool, model=(row["model"] or "").strip(),
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost,
        ))
        result.imported += 1
    return result
