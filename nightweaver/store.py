"""Durable run records and evidence indexes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


def default_home():
    configured = os.getenv("NIGHTWEAVER_HOME") or os.getenv("ROOKLINE_HOME")
    if configured:
        return Path(configured)
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    legacy = local / "Rookline"
    # Keep worktree links, provider sessions, and evidence paths valid on upgrade.
    return legacy if (legacy / "runs.sqlite3").is_file() else local / "Nightweaver"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                              ORDER BY rowid""")
            return [self.decode(row) for row in rows]

    def transition(self, run_id, kind, details=None, **changes):
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

    def events(self, run_id, limit=80):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq DESC LIMIT ?",
                              (run_id, limit)).fetchall()
        return [{"seq": row["seq"], "time": row["time"], "kind": row["kind"],
                 "details": json.loads(row["details"])} for row in reversed(rows)]

    def accepted_milestones(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT details FROM events WHERE run_id=? AND kind='milestone_accepted' ORDER BY seq",
                              (run_id,))
            return [json.loads(row["details"]) for row in rows]

    @staticmethod
    def _event(db, run_id, kind, details):
        db.execute("INSERT INTO events(run_id,time,kind,details) VALUES(?,?,?,?)",
                   (run_id, timestamp(), kind, json.dumps(details)))
