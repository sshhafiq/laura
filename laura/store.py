"""SQLite persistence for Laura (stdlib ``sqlite3`` only).

Design rules:

- **Multi-tenant by construction.** Every table that holds tenant data
  has an ``org_id`` column and every read/write helper takes ``org_id``;
  there is no code path that returns another org's rows.
- **Migrations-lite.** ``_MIGRATIONS`` is an ordered list of
  ``(version, sql)`` pairs. A ``schema_meta`` table records the applied
  version; opening a store applies whatever is missing, in order.
- **One store, one file.** ``LauraStore(":memory:")`` works for tests;
  a path on disk works for real self-hosting.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from laura.models import (
    Commit,
    Initiative,
    InitiativeRule,
    LineEvent,
    Org,
    PRFile,
    PullRequest,
    Repo,
    Review,
    TokenUsageRecord,
    User,
)

_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE orgs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        api_key_hash TEXT NOT NULL DEFAULT '',
        webhook_secret TEXT NOT NULL DEFAULT '',
        calibration_multiplier REAL NOT NULL DEFAULT 1.0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES orgs(id),
        login TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        team TEXT NOT NULL DEFAULT '',
        UNIQUE (org_id, login)
    );
    CREATE TABLE repos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES orgs(id),
        full_name TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main',
        UNIQUE (org_id, full_name)
    );
    CREATE TABLE pull_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES orgs(id),
        repo_id INTEGER NOT NULL REFERENCES repos(id),
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        author_login TEXT NOT NULL,
        branch TEXT NOT NULL DEFAULT '',
        base_branch TEXT NOT NULL DEFAULT 'main',
        body TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'open',
        created_at TEXT,
        first_commit_at TEXT,
        merged_at TEXT,
        closed_at TEXT,
        additions INTEGER NOT NULL DEFAULT 0,
        deletions INTEGER NOT NULL DEFAULT 0,
        changed_files INTEGER NOT NULL DEFAULT 0,
        is_revert INTEGER NOT NULL DEFAULT 0,
        is_hotfix INTEGER NOT NULL DEFAULT 0,
        score_hours REAL,
        score_reasoning TEXT,
        UNIQUE (org_id, repo_id, number)
    );
    CREATE TABLE pr_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        pr_id INTEGER NOT NULL REFERENCES pull_requests(id),
        path TEXT NOT NULL,
        additions INTEGER NOT NULL DEFAULT 0,
        deletions INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        pr_id INTEGER NOT NULL REFERENCES pull_requests(id),
        reviewer_login TEXT NOT NULL,
        state TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        comments_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE commits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        pr_id INTEGER NOT NULL REFERENCES pull_requests(id),
        sha TEXT NOT NULL,
        author_login TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        UNIQUE (org_id, sha)
    );
    CREATE TABLE line_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        repo_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        day TEXT NOT NULL,
        lines_changed INTEGER NOT NULL DEFAULT 0,
        pr_id INTEGER
    );
    CREATE TABLE token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        engineer_login TEXT NOT NULL,
        tool TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0.0
    );
    CREATE TABLE initiatives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL REFERENCES orgs(id),
        name TEXT NOT NULL,
        UNIQUE (org_id, name)
    );
    CREATE TABLE initiative_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER NOT NULL,
        initiative_id INTEGER NOT NULL REFERENCES initiatives(id),
        kind TEXT NOT NULL,
        pattern TEXT NOT NULL
    );
    CREATE TABLE pr_initiatives (
        org_id INTEGER NOT NULL,
        pr_id INTEGER NOT NULL,
        initiative_id INTEGER NOT NULL,
        PRIMARY KEY (pr_id, initiative_id)
    );
    """),
]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "laura_" + secrets.token_urlsafe(24)


class LauraStore:
    """The system of record. All queries are org-scoped."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # -- migrations -----------------------------------------------------
    def _migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
        )
        row = self.conn.execute("SELECT version FROM schema_meta").fetchone()
        current = row["version"] if row else 0
        for version, sql in _MIGRATIONS:
            if version > current:
                self.conn.executescript(sql)
                current = version
        if row is None:
            self.conn.execute(
                "INSERT INTO schema_meta (version) VALUES (?)", (current,)
            )
        else:
            self.conn.execute(
                "UPDATE schema_meta SET version = ?", (current,)
            )
        self.conn.commit()

    @property
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT version FROM schema_meta").fetchone()
        return row["version"]

    # -- orgs & users ---------------------------------------------------
    def create_org(self, name: str, slug: str) -> tuple[Org, str]:
        """Create an org and return ``(org, plaintext_api_key)``.

        The plaintext key is returned exactly once; only its hash and a
        freshly generated webhook secret are stored.
        """
        key = new_api_key()
        cur = self.conn.execute(
            "INSERT INTO orgs (name, slug, api_key_hash, webhook_secret, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, slug, hash_api_key(key), secrets.token_hex(20),
             _iso(datetime.now(timezone.utc))),
        )
        self.conn.commit()
        return self.get_org(cur.lastrowid), key

    def get_org(self, org_id: int) -> Org:
        row = self.conn.execute(
            "SELECT * FROM orgs WHERE id = ?", (org_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"org {org_id} not found")
        return self._org_from_row(row)

    def get_org_by_slug(self, slug: str) -> Org | None:
        row = self.conn.execute(
            "SELECT * FROM orgs WHERE slug = ?", (slug,)
        ).fetchone()
        return self._org_from_row(row) if row else None

    def get_org_by_api_key(self, key: str) -> Org | None:
        row = self.conn.execute(
            "SELECT * FROM orgs WHERE api_key_hash = ?", (hash_api_key(key),)
        ).fetchone()
        return self._org_from_row(row) if row else None

    def set_calibration(self, org_id: int, multiplier: float) -> None:
        self.conn.execute(
            "UPDATE orgs SET calibration_multiplier = ? WHERE id = ?",
            (multiplier, org_id),
        )
        self.conn.commit()

    @staticmethod
    def _org_from_row(row: sqlite3.Row) -> Org:
        return Org(
            id=row["id"], name=row["name"], slug=row["slug"],
            created_at=_parse(row["created_at"]),
            api_key_hash=row["api_key_hash"],
            webhook_secret=row["webhook_secret"],
            calibration_multiplier=row["calibration_multiplier"],
        )

    def upsert_user(self, user: User) -> User:
        self.conn.execute(
            "INSERT INTO users (org_id, login, name, email, team)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (org_id, login) DO UPDATE SET"
            " name = excluded.name, email = excluded.email, team = excluded.team",
            (user.org_id, user.login, user.name, user.email, user.team),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND login = ?",
            (user.org_id, user.login),
        ).fetchone()
        return User(id=row["id"], org_id=row["org_id"], login=row["login"],
                    name=row["name"], email=row["email"], team=row["team"])

    def list_users(self, org_id: int) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? ORDER BY login", (org_id,)
        ).fetchall()
        return [User(id=r["id"], org_id=r["org_id"], login=r["login"],
                     name=r["name"], email=r["email"], team=r["team"])
                for r in rows]

    def team_of(self, org_id: int, login: str) -> str:
        row = self.conn.execute(
            "SELECT team FROM users WHERE org_id = ? AND login = ?",
            (org_id, login),
        ).fetchone()
        return row["team"] if row else ""

    # -- repos ----------------------------------------------------------
    def upsert_repo(self, repo: Repo) -> Repo:
        self.conn.execute(
            "INSERT INTO repos (org_id, full_name, default_branch)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT (org_id, full_name) DO UPDATE SET"
            " default_branch = excluded.default_branch",
            (repo.org_id, repo.full_name, repo.default_branch),
        )
        self.conn.commit()
        return self.get_repo_by_name(repo.org_id, repo.full_name)

    def get_repo_by_name(self, org_id: int, full_name: str) -> Repo:
        row = self.conn.execute(
            "SELECT * FROM repos WHERE org_id = ? AND full_name = ?",
            (org_id, full_name),
        ).fetchone()
        if row is None:
            raise KeyError(f"repo {full_name} not found in org {org_id}")
        return Repo(id=row["id"], org_id=row["org_id"],
                    full_name=row["full_name"],
                    default_branch=row["default_branch"])

    def list_repos(self, org_id: int) -> list[Repo]:
        rows = self.conn.execute(
            "SELECT * FROM repos WHERE org_id = ? ORDER BY full_name", (org_id,)
        ).fetchall()
        return [Repo(id=r["id"], org_id=r["org_id"], full_name=r["full_name"],
                     default_branch=r["default_branch"]) for r in rows]

    # -- pull requests --------------------------------------------------
    def upsert_pull_request(self, pr: PullRequest) -> PullRequest:
        existing = self.conn.execute(
            "SELECT id FROM pull_requests"
            " WHERE org_id = ? AND repo_id = ? AND number = ?",
            (pr.org_id, pr.repo_id, pr.number),
        ).fetchone()
        values = (
            pr.title, pr.author_login, pr.branch, pr.base_branch, pr.body,
            pr.state, _iso(pr.created_at), _iso(pr.first_commit_at),
            _iso(pr.merged_at), _iso(pr.closed_at), pr.additions,
            pr.deletions, pr.changed_files, int(pr.is_revert),
            int(pr.is_hotfix),
        )
        if existing:
            pr_id = existing["id"]
            self.conn.execute(
                "UPDATE pull_requests SET title=?, author_login=?, branch=?,"
                " base_branch=?, body=?, state=?, created_at=?,"
                " first_commit_at=?, merged_at=?, closed_at=?, additions=?,"
                " deletions=?, changed_files=?, is_revert=?, is_hotfix=?"
                " WHERE id=?",
                values + (pr_id,),
            )
            self.conn.execute("DELETE FROM pr_files WHERE pr_id = ?", (pr_id,))
        else:
            cur = self.conn.execute(
                "INSERT INTO pull_requests (org_id, repo_id, number, title,"
                " author_login, branch, base_branch, body, state, created_at,"
                " first_commit_at, merged_at, closed_at, additions, deletions,"
                " changed_files, is_revert, is_hotfix)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pr.org_id, pr.repo_id, pr.number) + values,
            )
            pr_id = cur.lastrowid
        for f in pr.files:
            self.conn.execute(
                "INSERT INTO pr_files (org_id, pr_id, path, additions, deletions)"
                " VALUES (?, ?, ?, ?, ?)",
                (pr.org_id, pr_id, f.path, f.additions, f.deletions),
            )
        self.conn.commit()
        return self.get_pull_request(pr.org_id, pr_id)

    def get_pull_request(self, org_id: int, pr_id: int) -> PullRequest:
        row = self.conn.execute(
            "SELECT * FROM pull_requests WHERE org_id = ? AND id = ?",
            (org_id, pr_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"pull request {pr_id} not found in org {org_id}")
        return self._pr_from_row(row)

    def find_pull_request(self, org_id: int, repo_id: int,
                          number: int) -> PullRequest | None:
        row = self.conn.execute(
            "SELECT * FROM pull_requests"
            " WHERE org_id = ? AND repo_id = ? AND number = ?",
            (org_id, repo_id, number),
        ).fetchone()
        return self._pr_from_row(row) if row else None

    def list_pull_requests(self, org_id: int, *, repo_id: int | None = None,
                           state: str | None = None,
                           unscored_only: bool = False) -> list[PullRequest]:
        sql = "SELECT * FROM pull_requests WHERE org_id = ?"
        args: list = [org_id]
        if repo_id is not None:
            sql += " AND repo_id = ?"
            args.append(repo_id)
        if state is not None:
            sql += " AND state = ?"
            args.append(state)
        if unscored_only:
            sql += " AND score_hours IS NULL"
        sql += " ORDER BY COALESCE(merged_at, created_at), number"
        rows = self.conn.execute(sql, args).fetchall()
        return [self._pr_from_row(r) for r in rows]

    def set_score(self, org_id: int, pr_id: int, hours: float,
                  reasoning: dict) -> None:
        self.conn.execute(
            "UPDATE pull_requests SET score_hours = ?, score_reasoning = ?"
            " WHERE org_id = ? AND id = ?",
            (hours, json.dumps(reasoning), org_id, pr_id),
        )
        self.conn.commit()

    def _pr_from_row(self, row: sqlite3.Row) -> PullRequest:
        files = [
            PRFile(path=r["path"], additions=r["additions"],
                   deletions=r["deletions"])
            for r in self.conn.execute(
                "SELECT * FROM pr_files WHERE pr_id = ? ORDER BY path",
                (row["id"],),
            ).fetchall()
        ]
        return PullRequest(
            id=row["id"], org_id=row["org_id"], repo_id=row["repo_id"],
            number=row["number"], title=row["title"],
            author_login=row["author_login"], branch=row["branch"],
            base_branch=row["base_branch"], body=row["body"],
            state=row["state"], created_at=_parse(row["created_at"]),
            first_commit_at=_parse(row["first_commit_at"]),
            merged_at=_parse(row["merged_at"]),
            closed_at=_parse(row["closed_at"]), additions=row["additions"],
            deletions=row["deletions"], changed_files=row["changed_files"],
            is_revert=bool(row["is_revert"]), is_hotfix=bool(row["is_hotfix"]),
            files=files, score_hours=row["score_hours"],
            score_reasoning=(json.loads(row["score_reasoning"])
                              if row["score_reasoning"] else None),
        )

    # -- reviews & commits ----------------------------------------------
    def add_review(self, review: Review) -> Review:
        cur = self.conn.execute(
            "INSERT INTO reviews (org_id, pr_id, reviewer_login, state,"
            " submitted_at, body, comments_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (review.org_id, review.pr_id, review.reviewer_login,
             review.state, _iso(review.submitted_at), review.body,
             review.comments_count),
        )
        self.conn.commit()
        review.id = cur.lastrowid
        return review

    def list_reviews(self, org_id: int, pr_id: int) -> list[Review]:
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE org_id = ? AND pr_id = ?"
            " ORDER BY submitted_at",
            (org_id, pr_id),
        ).fetchall()
        return [Review(id=r["id"], org_id=r["org_id"], pr_id=r["pr_id"],
                       reviewer_login=r["reviewer_login"], state=r["state"],
                       submitted_at=_parse(r["submitted_at"]), body=r["body"],
                       comments_count=r["comments_count"]) for r in rows]

    def add_commit(self, commit: Commit) -> Commit:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO commits (org_id, pr_id, sha, author_login,"
            " committed_at, message) VALUES (?, ?, ?, ?, ?, ?)",
            (commit.org_id, commit.pr_id, commit.sha, commit.author_login,
             _iso(commit.committed_at), commit.message),
        )
        self.conn.commit()
        commit.id = cur.lastrowid
        return commit

    def list_commits(self, org_id: int, pr_id: int) -> list[Commit]:
        rows = self.conn.execute(
            "SELECT * FROM commits WHERE org_id = ? AND pr_id = ?"
            " ORDER BY committed_at",
            (org_id, pr_id),
        ).fetchall()
        return [Commit(id=r["id"], org_id=r["org_id"], pr_id=r["pr_id"],
                       sha=r["sha"], author_login=r["author_login"],
                       committed_at=_parse(r["committed_at"]),
                       message=r["message"]) for r in rows]

    # -- line events (turnover) ------------------------------------------
    def add_line_event(self, event: LineEvent) -> LineEvent:
        cur = self.conn.execute(
            "INSERT INTO line_events (org_id, repo_id, path, day,"
            " lines_changed, pr_id) VALUES (?, ?, ?, ?, ?, ?)",
            (event.org_id, event.repo_id, event.path, _iso(event.day),
             event.lines_changed, event.pr_id),
        )
        self.conn.commit()
        event.id = cur.lastrowid
        return event

    def record_merge_line_events(self, pr: PullRequest) -> int:
        """Derive line events from a merged PR's per-file churn."""
        if pr.state != "merged" or pr.merged_at is None:
            return 0
        count = 0
        for f in pr.files:
            if f.churn > 0:
                self.add_line_event(LineEvent(
                    id=None, org_id=pr.org_id, repo_id=pr.repo_id,
                    path=f.path, day=pr.merged_at, lines_changed=f.churn,
                    pr_id=pr.id,
                ))
                count += 1
        return count

    def list_line_events(self, org_id: int,
                         repo_id: int | None = None) -> list[LineEvent]:
        sql = "SELECT * FROM line_events WHERE org_id = ?"
        args: list = [org_id]
        if repo_id is not None:
            sql += " AND repo_id = ?"
            args.append(repo_id)
        sql += " ORDER BY day"
        rows = self.conn.execute(sql, args).fetchall()
        return [LineEvent(id=r["id"], org_id=r["org_id"],
                          repo_id=r["repo_id"], path=r["path"],
                          day=_parse(r["day"]),
                          lines_changed=r["lines_changed"], pr_id=r["pr_id"])
                for r in rows]

    # -- token usage ------------------------------------------------------
    def add_token_usage(self, record: TokenUsageRecord) -> TokenUsageRecord:
        cur = self.conn.execute(
            "INSERT INTO token_usage (org_id, day, engineer_login, tool,"
            " model, input_tokens, output_tokens, cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record.org_id, _iso(record.day), record.engineer_login,
             record.tool, record.model, record.input_tokens,
             record.output_tokens, record.cost_usd),
        )
        self.conn.commit()
        record.id = cur.lastrowid
        return record

    def list_token_usage(self, org_id: int) -> list[TokenUsageRecord]:
        rows = self.conn.execute(
            "SELECT * FROM token_usage WHERE org_id = ? ORDER BY day", (org_id,)
        ).fetchall()
        return [TokenUsageRecord(
            id=r["id"], org_id=r["org_id"], day=_parse(r["day"]),
            engineer_login=r["engineer_login"], tool=r["tool"],
            model=r["model"], input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"], cost_usd=r["cost_usd"])
            for r in rows]

    # -- initiatives ------------------------------------------------------
    def create_initiative(self, org_id: int, name: str) -> Initiative:
        cur = self.conn.execute(
            "INSERT INTO initiatives (org_id, name) VALUES (?, ?)",
            (org_id, name),
        )
        self.conn.commit()
        return Initiative(id=cur.lastrowid, org_id=org_id, name=name)

    def add_initiative_rule(self, rule: InitiativeRule) -> InitiativeRule:
        cur = self.conn.execute(
            "INSERT INTO initiative_rules (org_id, initiative_id, kind, pattern)"
            " VALUES (?, ?, ?, ?)",
            (rule.org_id, rule.initiative_id, rule.kind, rule.pattern),
        )
        self.conn.commit()
        rule.id = cur.lastrowid
        return rule

    def list_initiatives(self, org_id: int) -> list[Initiative]:
        rows = self.conn.execute(
            "SELECT * FROM initiatives WHERE org_id = ? ORDER BY name",
            (org_id,),
        ).fetchall()
        return [Initiative(id=r["id"], org_id=r["org_id"], name=r["name"])
                for r in rows]

    def list_initiative_rules(self, org_id: int) -> list[InitiativeRule]:
        rows = self.conn.execute(
            "SELECT * FROM initiative_rules WHERE org_id = ? ORDER BY id",
            (org_id,),
        ).fetchall()
        return [InitiativeRule(id=r["id"], org_id=r["org_id"],
                               initiative_id=r["initiative_id"],
                               kind=r["kind"], pattern=r["pattern"])
                for r in rows]

    def link_pr_initiative(self, org_id: int, pr_id: int,
                           initiative_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO pr_initiatives (org_id, pr_id, initiative_id)"
            " VALUES (?, ?, ?)",
            (org_id, pr_id, initiative_id),
        )
        self.conn.commit()

    def initiatives_for_pr(self, org_id: int, pr_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT initiative_id FROM pr_initiatives"
            " WHERE org_id = ? AND pr_id = ?",
            (org_id, pr_id),
        ).fetchall()
        return [r["initiative_id"] for r in rows]
