"""Durable run records and evidence indexes."""

from contextlib import contextmanager
from copy import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
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


# Events that end a pursuit stop: the operator extended the run, retried it or it blocked again for another reason.
STOP_ENDED = ("pursuit_extended", "blocked_run_retried", "blocked", "judge_invalid", "plan_invalid", "provider_unavailable", "budget_exhausted",
              "repeated_failure_stopped", "verification_inconsistent", "dispatcher_run_failed", "rescue_diagnosis_invalid")


def current_stop(events):
    """The details of the pursuit_stopped event that still explains why a run is blocked, or None once a later event
    (an extension, a retry or another block) has superseded it. `events` are oldest first and carry their kinds."""
    stop = None
    for event in events:
        if event["kind"] == "pursuit_stopped":
            stop = event["details"]
        elif event["kind"] in STOP_ENDED:
            stop = None
    return stop


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
        if getattr(self, "_reader", None) is not None:
            yield self._reader
            return
        db = sqlite3.connect(self.root / "runs.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def snapshot(self):
        """Read state, metrics and evidence from the same SQLite WAL revision."""
        if getattr(self, "_reader", None) is not None:
            yield self
            return
        with self.connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            reader = copy(self)
            reader._reader = db
            yield reader

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

    def run_summaries(self):
        """Read the sidebar's fields without materializing prompts, gate plans or provider configs."""
        query = """SELECT id, updated, pause, state, json_extract(spec,'$.title') AS title,
                   json_extract(spec,'$.project') AS project, json_array_length(spec,'$.milestones') AS total,
                   json_type(spec,'$.pursue')='object' AS pursuit,
                   json_extract(spec,'$.milestones[' || json_extract(state,'$.step') || '].title') AS current
                   FROM runs ORDER BY updated DESC"""
        keys = ("phase", "step", "accepted", "result_ref", "dispatch", "after", "retry_at", "last_error", "provider_calls",
                "wave", "best_score")
        with self.connect() as db:
            result = []
            for row in db.execute(query):
                state = json.loads(row["state"])
                summary = {k: state[k] for k in keys if k in state}
                if issue := state.get("provider_issue"):
                    summary["provider_issue"] = {k: issue[k] for k in ("provider", "role", "profile_dir") if k in issue}
                if row["pursuit"]:  # a goal pursuit's milestone count includes the waves a revert discarded
                    summary.update(pursuit=True, discarded=len(state.get("discarded_commits", ())))
                spec = {"title": row["title"]}
                if row["project"]:
                    spec["project"] = row["project"]
                result.append({"id": row["id"], "updated": row["updated"], "pause": bool(row["pause"]),
                               "spec": spec, "state": summary, "total_milestones": row["total"],
                               "current_milestone": row["current"]})
            return result

    def dispatch_runs(self):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM runs WHERE pause=0
                              AND json_extract(state, '$.dispatch')=1
                              AND json_extract(state, '$.phase') NOT IN ('done', 'blocked')
                              AND (json_extract(state, '$.retry_at') IS NULL OR json_extract(state, '$.retry_at') <= ?)
                              ORDER BY rowid""", (timestamp(),))
            return [self.decode(row) for row in rows]

    def guarded_runs(self):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM runs WHERE pause=0
                              AND json_type(state, '$.guard_limit')='integer'
                              AND COALESCE(json_extract(state, '$.dispatch'),0)=0
                              AND json_extract(state, '$.phase') NOT IN ('done','blocked') ORDER BY rowid""")
            return [self.decode(row) for row in rows]

    def dependency_phases(self, ids):
        keys = list(dict.fromkeys(ids))
        phases = {}
        if keys:
            with self.connect() as db:
                for i in range(0, len(keys), 500):
                    batch = keys[i:i+500]
                    slots = ",".join("?" for _ in batch)
                    phases.update(db.execute(f"SELECT id, json_extract(state, '$.phase') FROM runs WHERE id IN ({slots})", batch))
        return phases

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
            for extra in [following_event] if isinstance(following_event, tuple) else following_event or ():
                self._event(db, run_id, *extra)
        return row["state"]

    def extend_plan(self, run_id, milestones, details, **changes):
        """Append a planned wave, its state and its event in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            first = len(row["spec"]["milestones"]) + 1
            if first - 1 + len(milestones) > 50:
                raise ValueError("A run supports at most 50 milestones")
            row["spec"]["milestones"] = [*row["spec"]["milestones"], *milestones]
            row["state"].update(changes)
            db.execute("UPDATE runs SET spec=?,state=?,updated=? WHERE id=?",
                       (json.dumps(row["spec"]), json.dumps(row["state"]), timestamp(), run_id))
            self._event(db, run_id, "wave_planned", {**details, "first_step": first})
        return row

    def extend_pursuit(self, run_id, waves):
        """Grant more waves. A pursuit that stopped on a judged build resumes at the planner with that build's gaps and a fresh plateau
        count, so the unchanged build is not judged again; any other block at the judge resumes at the judge."""
        if type(waves) is not int or not 1 <= waves <= 20:
            raise ValueError("Added waves must be an integer between 1 and 20")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not row["spec"].get("pursue"):
                raise ValueError("This run does not pursue a goal")
            state = row["state"]
            if state["phase"] == "done":
                raise ValueError("The goal was already met")
            blocked = state["phase"] == "blocked"
            stop = None
            if blocked:
                kinds = ("pursuit_stopped", *STOP_ENDED)
                stop = current_stop([{"kind": r["kind"], "details": json.loads(r["details"])} for r in db.execute(
                    "SELECT kind, details FROM events WHERE run_id=? AND kind IN (" + ",".join("?" for _ in kinds) + ") ORDER BY seq", (run_id, *kinds))])
            if stop and stop.get("reason") == "milestone_limit":
                raise ValueError("This run stopped at the 50-milestone limit, which more waves cannot lift. Start a new run from the best build to keep going.")
            row["spec"]["pursue"]["max_waves"] += waves
            resumed = blocked and state.get("blocked_from") == "judging"
            if resumed:
                state.update(phase="judging", blocked_from=None, last_error="", provider_issue=None, retry_at=None,
                             judge_attempts=0, judge_ledger=None, stale_judgments=0, transient_failures=0)
                judged = {item.get("commit") for item in state.get("judgments", [])}
                if stop and state.get("accepted") in judged:
                    # The stop followed a judgment of this very build, so judging it again would add no evidence: plan from its gaps.
                    gaps = state.get("best_gaps") if state.get("accepted") == state.get("best_commit") else state.get("pursuit_gaps")
                    last = (state.get("judgments") or [{}])[-1]
                    # A stop that discarded the last build planned from an older one: say so, as the planner is told after any other discard.
                    note = (f"Wave {last.get('wave')} scored {last.get('score')}/100, below the best score of {state.get('best_score')}/100, so its changes "
                            "were discarded. Plan from the best build." if last.get("commit") and last["commit"] != state.get("accepted") else "")
                    state.update(phase="planning", judge_stage=None, plan_attempts=0, planner_route=0, pursuit_note=note, pursuit_failures=[],
                                 pursuit_gaps=gaps or [{"title": "The judge does not consider the goal met", "detail": "See the last judgment."}])
            db.execute("UPDATE runs SET spec=?,state=?,updated=? WHERE id=?",
                       (json.dumps(row["spec"]), json.dumps(state), timestamp(), run_id))
            self._event(db, run_id, "pursuit_extended", {"waves": waves, "max_waves": row["spec"]["pursue"]["max_waves"],
                                                          "resumed": resumed, "phase": state["phase"]})
        return {"max_waves": row["spec"]["pursue"]["max_waves"], "resumed": resumed, "phase": state["phase"]}

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

    def accepted_milestones(self, run_id, *, discarded=False):
        """The milestones of the current build, oldest first. A pursuit that reverted to a better build no longer counts the milestones
        of the discarded waves; discarded=True lists every milestone the run ever accepted."""
        with self.connect() as db:
            rows = db.execute("SELECT details FROM events WHERE run_id=? AND kind='milestone_accepted' ORDER BY seq",
                              (run_id,))
            entries = [json.loads(row["details"]) for row in rows]
            if discarded:
                return entries
        gone = set(self.discarded_commits(run_id))
        return [item for item in entries if item.get("commit") not in gone] if gone else entries

    def discarded_commits(self, run_id):
        """Commits that a pursuit's reverts threw away. A run that reverted before its state kept them is read from its pursuit_reverted
        events: the commits each reverted build holds beyond the build the result branch went back to."""
        with self.connect() as db:
            held = db.execute("SELECT state, json_extract(spec,'$.repository') AS repository FROM runs WHERE id=?", (run_id,)).fetchone()
            state = json.loads(held["state"]) if held else {}
            if "discarded_commits" in state or not held:
                return list(state.get("discarded_commits", ()))
            reverts = [json.loads(row["details"]) for row in db.execute(
                "SELECT details FROM events WHERE run_id=? AND kind='pursuit_reverted' ORDER BY seq", (run_id,))]
        found = []
        if reverts and held["repository"]:
            from .engine import git
            for details in reverts:
                try:
                    found += [c for c in git(held["repository"], "rev-list", f"{details['to']}..{details['from']}").split() if c not in found]
                except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                    continue
        return found

    @staticmethod
    def _event(db, run_id, kind, details):
        db.execute("INSERT INTO events(run_id,time,kind,details) VALUES(?,?,?,?)",
                   (run_id, timestamp(), kind, json.dumps(redact(details))))
