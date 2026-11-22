"""Initiative auto-linking: decide which initiatives a PR belongs to.

Rules are evaluated in a fixed order and *all* matching initiatives are
linked (a PR can serve two initiatives). Matching is deliberately
simple and explainable — the same rules a human would apply skimming a
PR list:

- ``branch_prefix`` — head branch starts with the pattern
  (``payments/`` matches ``payments/retry-logic``)
- ``title_tag`` — title contains ``[TAG]`` or starts with ``TAG:``
- ``issue_key`` — title, body, or branch contains the pattern followed
  by digits (``PROJ-`` matches ``PROJ-123``); a bare pattern without the
  issue-key shape falls back to a plain substring match
- ``repo`` — the PR's repo full name equals the pattern
"""

from __future__ import annotations

import re

from laura.models import InitiativeRule, PullRequest


def match_rule(rule: InitiativeRule, pr: PullRequest,
               repo_full_name: str = "") -> bool:
    pattern = rule.pattern
    if rule.kind == "branch_prefix":
        return pr.branch.startswith(pattern)
    if rule.kind == "title_tag":
        tag = pattern.strip("[]")
        return (f"[{tag}]" in pr.title) or pr.title.startswith(f"{tag}:")
    if rule.kind == "issue_key":
        haystacks = [pr.title, pr.body or "", pr.branch]
        keyed = re.compile(re.escape(pattern) + r"\d+", re.IGNORECASE)
        if any(keyed.search(h) for h in haystacks):
            return True
        return any(pattern.lower() in h.lower() for h in haystacks)
    if rule.kind == "repo":
        return repo_full_name == pattern
    return False


def link_initiatives(store, pr: PullRequest,
                     repo_full_name: str = "") -> list[int]:
    """Evaluate every rule for the PR's org, persist links, return ids."""
    matched: list[int] = []
    for rule in store.list_initiative_rules(pr.org_id):
        if match_rule(rule, pr, repo_full_name):
            store.link_pr_initiative(pr.org_id, pr.id, rule.initiative_id)
            if rule.initiative_id not in matched:
                matched.append(rule.initiative_id)
    return matched
