# Laura — open-source engineering intelligence

Commercial engineering-intelligence tools (Weave and friends) charge
~$50 per engineer per month to tell you what your GitHub data already
knows. **Laura is the open-source counterpart**: self-host it, connect
your repos, and get PR effort scoring, AI-spend ROI, DORA metrics, code
turnover, and a coding-agent model router — with every number
inspectable down to the formula that produced it.

No SaaS, no per-seat pricing, no black-box scores. The core is pure
Python (stdlib only), one SQLite file, and runs anywhere.

## Quickstart

```bash
pip install "laura[dev]"        # or: pip install -e . from a clone
laura demo                      # seeds a realistic demo company
laura serve                     # API on http://127.0.0.1:8730
```

or the whole stack (API + Temporal + UI) in Docker:

```bash
docker compose up -d
curl localhost:8730/health
```

Then create your own org and connect a repo:

```bash
laura init --name Acme --slug acme        # prints your API key (once)
export LAURA_API_KEY=laura_...
curl -X POST localhost:8730/repos -H "X-API-Key: $LAURA_API_KEY" \
     -d '{"full_name": "acme/api"}'
# point your GitHub webhook at /webhooks/github/acme with the printed
# webhook secret, import AI tool usage, and ask questions:
laura import-tokens usage.csv
curl "localhost:8730/ask?q=which+team+is+slowest+to+review" \
     -H "X-API-Key: $LAURA_API_KEY"
```

## Feature map (the category, open-sourced)

| Capability | Commercial version | Laura |
|---|---|---|
| PR effort score | LLM scores every PR in "expert hours" with reasoning | **Laura Hours**: deterministic heuristic scorer with a per-factor reasoning breakdown that sums to the score, plus an `LLMScorer` seam to plug in a real model |
| AI ROI report | token spend vs output, two value formulas | `laura/roi.py`: spend joined with Laura Hours; Output *and* Payroll formulas, all rate inputs visible |
| Review scoring | review effort/quality tracked separately | `score_review`: effort estimate + quality signals (incl. rubber-stamp risk) |
| Code turnover | lines rewritten within 90 days of merge | line-event history → turnover rate per repo, 90-day window |
| Flow metrics | DORA / SPACE / cycle time | cycle-time stages, throughput by team/engineer, full DORA set |
| Initiatives | PRs grouped into initiatives | rule-based auto-linking (branch prefix, title tag, issue key, repo) |
| Investigation agent | ask questions about your eng data | **Ask Laura**: deterministic intent parser over the real metrics — answers carry sources, never invented numbers |
| Model router | route coding-agent prompts to cheaper models | rule-based tier classifier, escalation on stall, session ledger with *measured* savings vs the priciest model |
| Durable pipelines | hosted sync/scoring jobs | optional Temporal workflows: per-repo sync + parallel scoring waves |

## Architecture

```
            GitHub webhooks ──┐
            backfill sync ────┤        ┌── scoring.py ── Laura Hours
                              ▼        │      (+ reasoning, calibration)
   token CSV ──► ingest/ ──► store.py ─┼── metrics.py ── cycle/DORA/turnover
   (AI tools)     (SQLite,   (org-     ├── roi.py ────── AI ROI, 2 formulas
                   multi-    scoped)   ├── router.py ─── model routing ledger
                   tenant)             ├── ask.py ────── sourced answers
                                       └── api.py / cli.py
            Temporal (optional): RepoSyncWorkflow + ScoringPipelineWorkflow
            fan the ingest and scoring work out durably, with retries.
```

- `laura/models.py` — Org (tenant), User, Repo, PullRequest, Review,
  Commit, TokenUsageRecord, Initiative: plain dataclasses.
- `laura/store.py` — SQLite via stdlib `sqlite3`, schema-versioned,
  every query scoped by `org_id`.
- `laura/ingest/` — webhook parsers + HMAC verification, a backfill
  sync client (transport interface; fake transport included), the
  token-usage CSV importer, initiative auto-linking.
- `laura/temporal/` — optional durable workflows (`pip install
  laura[temporal]`).

## How scoring works (the transparency bit)

A Laura Hours score is a sum of named factors, stored with the PR:

- **weighted churn** — per-file churn weighted by file kind (source
  1.0, test 0.55, config 0.45, docs 0.25, generated 0.08), compressed
  with `log2` because big diffs repeat patterns;
- **file spread** and **cross-file coupling** — more files / more
  top-level areas = more integration effort;
- **risk surface** — migrations, refactors, auth, payments in the title;
- **description** and **untested change** adjustments.

Each org also learns a **calibration multiplier** from its own merged
PRs (median business-hours from first commit to merge vs median raw
score, clamped), so the scale bends toward how *your* team actually
works. Review effort is scored separately from authoring effort.

## API summary

| Endpoint | What |
|---|---|
| `POST /orgs` | create org → API key + webhook secret (shown once) |
| `POST /repos` | register a repo (API key auth) |
| `POST /webhooks/github/{slug}` | GitHub webhook receiver (HMAC verified) |
| `POST /tokens/import` | body = token-usage CSV |
| `GET /metrics/summary?days=90` | cycle time, throughput, DORA, turnover, review stats |
| `GET /roi` | AI ROI: last 30 days vs the prior 30 |
| `GET /ask?q=...` | Ask Laura answer with sources |
| `GET /health` | liveness |

Token CSV format: `date,engineer,tool,model,input_tokens,output_tokens,cost_usd`.

## Development

```bash
pip install -e ".[dev,temporal]"
python -m pytest        # 49 tests, no network, no live Temporal needed
```

## License

MIT — see `LICENSE`.
