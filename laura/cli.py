"""Laura command line.

    laura init --name Acme --slug acme      create an org + API key
    laura import-tokens usage.csv           import AI tool usage (needs key)
    laura demo                              seed the DemoCo demo org
    laura serve                             run the HTTP API

The database defaults to ``./laura.db`` (override with ``--db`` or the
``LAURA_DB`` env var). The API key for import/serve comes from
``--api-key`` or ``LAURA_API_KEY``.
"""

from __future__ import annotations

import argparse
import os
import sys

from laura.store import LauraStore


def _store(args) -> LauraStore:
    return LauraStore(args.db or os.environ.get("LAURA_DB", "laura.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="laura", description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite db path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create an org and API key")
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--slug", required=True)

    p_import = sub.add_parser("import-tokens", help="import token usage CSV")
    p_import.add_argument("file")
    p_import.add_argument("--api-key", default=None)

    sub.add_parser("demo", help="seed the DemoCo demo organisation")

    p_serve = sub.add_parser("serve", help="run the HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8730)

    args = parser.parse_args(argv)
    store = _store(args)

    if args.command == "init":
        org, key = store.create_org(args.name, args.slug)
        print(f"org created: {org.name} (slug {org.slug}, id {org.id})")
        print(f"API key (shown once): {key}")
        print(f"webhook secret: {org.webhook_secret}")
        print(f"webhook URL: /webhooks/github/{org.slug}")
        return 0

    if args.command == "import-tokens":
        key = args.api_key or os.environ.get("LAURA_API_KEY")
        if not key:
            print("error: --api-key or LAURA_API_KEY is required",
                  file=sys.stderr)
            return 2
        org = store.get_org_by_api_key(key)
        if org is None:
            print("error: API key not recognised", file=sys.stderr)
            return 2
        from laura.ingest.tokens_csv import import_token_csv
        with open(args.file, encoding="utf-8") as fh:
            result = import_token_csv(store, org.id, fh.read())
        print(f"imported {result.imported} rows")
        for error in result.errors:
            print(f"  skipped: {error}", file=sys.stderr)
        return 0 if result.ok else 1

    if args.command == "demo":
        from laura.demo import seed_demo
        summary = seed_demo(store)
        for name, value in summary.items():
            print(f"{name}: {value}")
        return 0

    if args.command == "serve":
        from laura.api import serve
        print(f"laura api on http://{args.host}:{args.port}")
        serve(store, args.host, args.port)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
