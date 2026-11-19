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
                CREATE INDEX IF NOT EXISTS events_for_kind ON events(run_id, kind, seq);
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
        repository, gitdir = Path(repository).resolve(), Path(gitdir).resolve()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("Project names must be 1 to 64 letters, numbers, dots, underscores or hyphens")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
            if old and not replace and (Path(old["repository"]).resolve() != repository or Path(old["git_common_dir"]).resolve() != gitdir):
                raise ValueError(f"Project {name!r} is bound to {old['repository']}. Use --replace to change it.")
            db.execute("""INSERT INTO projects VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE
                          SET repository=excluded.repository, git_common_dir=excluded.git_common_dir""",
                       (name, str(repository), str(gitdir)))
        return self.project(name)

    def create(self, spec, base, *, enqueue=False, after=(), context_pack=None):
        run_id = uuid4().hex[:12]
        state = {"phase": "queued", "step": 0, "attempt": 0, "base": base, "accepted": base,
                 "candidate": None, "last_error": "", "log": None,
                 "result_ref": f"refs/heads/nightweaver/{run_id}"}
        if enqueue:
            state.update(dispatch=True, after=list(after))
        self.directory(run_id).mkdir(parents=True)
        if context_pack is not None:
            from . import context
            spec = {**spec, "context_sha256": context.digest(context_pack)}
            context.save(self, run_id, context_pack)
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

    def transition(self, run_id, kind, details=None, *, following_event=None, **changes):
        if "last_error" in changes:
            changes["last_error"] = redact(changes["last_error"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            row["state"].update(changes)
            db.execute("UPDATE runs SET state=?,updated=? WHERE id=?",
                       (json.dumps(row["state"]), timestamp(), run_id))
            self._event(db, run_id, kind, details or {})
            if following_event is not None:
                self._event(db, run_id, *following_event)
        return row["state"]

    def pause(self, run_id, value):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if row["state"]["phase"] == "done" or row["pause"] == bool(value):
                return False
            db.execute("UPDATE runs SET pause=?,updated=? WHERE id=?", (int(value), timestamp(), run_id))
            self._event(db, run_id, "pause_requested" if value else "resume_requested", {})
            return True

    def events(self, run_id, limit=80, after=None, *, kinds=(), until=None):
        self.directory(run_id)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError("Event page size must be 1 to 10000")
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError("Event cursor must be a nonnegative integer")
        if until is not None and (type(until) is not int or until < (after or 0)):
            raise ValueError("End cursor must be at or after the start cursor")
        filters, values = ["run_id=?"], [run_id]
        if kinds:
            filters.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            values.extend(kinds)
        if after is not None:
            filters.append("seq>?")
            values.append(after)
        if until is not None:
            filters.append("seq<=?")
            values.append(until)
        latest = after is None and limit is not None
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise ValueError("Run was not found")
            query = "SELECT * FROM events WHERE " + " AND ".join(filters) + " ORDER BY seq "
            rows = db.execute(query + ("DESC" if latest else "ASC") + " LIMIT ?", [*values, limit or -1]).fetchall()
            if latest:
                rows.reverse()
        return [{"seq": row["seq"], "time": row["time"], "kind": row["kind"],
                 "details": json.loads(row["details"])} for row in rows]

    def gate_samples(self, run_id, batch, gate_index):
        with self.connect() as db:
            rows = db.execute("""
                SELECT details FROM events WHERE run_id=? AND kind='gate_finished'
                AND json_extract(details, '$.verification_batch')=?
                AND json_extract(details, '$.gate_index')=? ORDER BY seq
            """, (run_id, batch, gate_index))
            return [json.loads(row["details"]) for row in rows]

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
            rows = db.execute("SELECT time,details FROM events WHERE run_id=? AND kind='operator_feedback' ORDER BY seq DESC LIMIT 13",
                              (run_id,)).fetchall()
        selected, size = [], 0
        for row in rows[:12]:
            message = f"{row['time']}: {json.loads(row['details'])['message']}"
            if size + len(message) + 1 > 23900:
                break
            selected.append(message)
            size += len(message) + 1
        if len(rows) > len(selected):
            selected.append("[Earlier operator updates are available in the run events.]")
        return "\n".join(reversed(selected))

    def accepted_milestones(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT details FROM events WHERE run_id=? AND kind='milestone_accepted' ORDER BY seq",
                              (run_id,))
            return [json.loads(row["details"]) for row in rows]

    @staticmethod
    def _event(db, run_id, kind, details):
        db.execute("INSERT INTO events(run_id,time,kind,details) VALUES(?,?,?,?)",
                   (run_id, timestamp(), kind, json.dumps(redact(details))))
