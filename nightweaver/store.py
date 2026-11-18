"""Durable run records and evidence indexes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .safety import redact


def default_home():
    configured = os.getenv("NIGHTWEAVER_HOME") or os.getenv("ROOKLINE_HOME")
    if configured:
        return Path(configured)
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    legacy = local / "Rookline"
    # Keep worktree links, provider sessions, and evidence paths valid on upgrade.
    return legacy if (legacy / "runs.sqlite3").is_file() else local / "Nightweaver"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, root=None):
        self.root = Path(root or default_home()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL,
                    updated TEXT NOT NULL, pause INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    time TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_for_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS projects (
                    name TEXT PRIMARY KEY COLLATE NOCASE, repository TEXT NOT NULL,
                    git_common_dir TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "runs.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("Run was not found")
        return {"id": row["id"], "spec": json.loads(row["spec"]), "state": json.loads(row["state"]),
                "updated": row["updated"], "pause": bool(row["pause"])}

    def directory(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{12}", run_id):
            raise ValueError("Invalid run ID")
        return self.root / "runs" / run_id

    def projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")]

    def project(self, name):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"Project {name!r} is not registered in this Nightweaver runtime")
            return dict(row)

    def bind_project(self, name, repository, gitdir, replace=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("Project names must be 1 to 64 letters, numbers, dots, underscores or hyphens")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
            if old and not replace and (Path(old["repository"]) != repository or Path(old["git_common_dir"]) != gitdir):
                raise ValueError(f"Project {name!r} is bound to {old['repository']}. Use --replace to change it.")
            db.execute("""INSERT INTO projects VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE
                          SET repository=excluded.repository, git_common_dir=excluded.git_common_dir""",
                       (name, str(repository), str(gitdir)))
        return self.project(name)

    def create(self, spec, base, *, enqueue=False, after=()):
        run_id = uuid4().hex[:12]
        state = {"phase": "queued", "step": 0, "attempt": 0, "base": base, "accepted": base,
                 "candidate": None, "last_error": "", "log": None,
                 "result_ref": f"refs/heads/nightweaver/{run_id}"}
        if enqueue:
            state.update(dispatch=True, after=list(after))
        self.directory(run_id).mkdir(parents=True)
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,spec,state,updated) VALUES(?,?,?,?)",
                       (run_id, json.dumps(spec), json.dumps(state), timestamp()))
            self._event(db, run_id, "queued", {"base": base})
        return run_id

    def get(self, run_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def runs(self):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM runs ORDER BY updated DESC")]

    def dispatch_runs(self):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM runs WHERE pause=0
                              AND json_extract(state, '$.dispatch')=1
                              AND json_extract(state, '$.phase') NOT IN ('done', 'blocked')
                              AND (json_extract(state, '$.retry_at') IS NULL OR json_extract(state, '$.retry_at') <= ?)
                              ORDER BY rowid""", (timestamp(),))
            return [self.decode(row) for row in rows]

    def transition(self, run_id, kind, details=None, **changes):
        if "last_error" in changes:
            changes["last_error"] = redact(changes["last_error"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            row["state"].update(changes)
            db.execute("UPDATE runs SET state=?,updated=? WHERE id=?",
                       (json.dumps(row["state"]), timestamp(), run_id))
            self._event(db, run_id, kind, details or {})
        return row["state"]

    def pause(self, run_id, value):
        with self.connect() as db:
            if not db.execute("UPDATE runs SET pause=? WHERE id=?", (int(value), run_id)).rowcount:
                raise ValueError("Run was not found")
            self._event(db, run_id, "pause_requested" if value else "resume_requested", {})

    def events(self, run_id, limit=80, after=None):
        self.directory(run_id)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError("Event page size must be 1 to 10000")
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError("Event cursor must be a nonnegative integer")
        with self.connect() as db:
            if after is not None or limit is None:
                rows = db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                  (run_id, after or 0, limit or -1)).fetchall()
            else:
                rows = list(reversed(db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq DESC LIMIT ?",
                                               (run_id, limit)).fetchall()))
        return [{"seq": row["seq"], "time": row["time"], "kind": row["kind"],
                 "details": json.loads(row["details"])} for row in rows]

    def feedback(self, run_id, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise ValueError("Feedback must contain 1 to 12000 characters")
        if self.get(run_id)["state"]["phase"] == "done":
            raise ValueError("Fork a completed run before adding new instructions")
        # Serialize updates with runner transitions so no correction is silently lost.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if row["state"]["phase"] == "done":
                raise ValueError("Fork a completed run before adding new instructions")
            row["state"]["feedback_version"] = row["state"].get("feedback_version", 0) + 1
            db.execute("UPDATE runs SET state=?,updated=? WHERE id=?", (json.dumps(row["state"]), timestamp(), run_id))
            self._event(db, run_id, "operator_feedback", {"message": message.strip()})

    def feedback_text(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT time,details FROM events WHERE run_id=? AND kind='operator_feedback' ORDER BY seq DESC LIMIT 12",
                              (run_id,)).fetchall()
        return "\n".join(f"{row['time']}: {json.loads(row['details'])['message']}" for row in reversed(rows))[-24000:]

    def accepted_milestones(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT details FROM events WHERE run_id=? AND kind='milestone_accepted' ORDER BY seq",
                              (run_id,))
            return [json.loads(row["details"]) for row in rows]

    @staticmethod
    def _event(db, run_id, kind, details):
        db.execute("INSERT INTO events(run_id,time,kind,details) VALUES(?,?,?,?)",
                   (run_id, timestamp(), kind, json.dumps(redact(details))))
