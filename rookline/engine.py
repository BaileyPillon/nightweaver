"""Advance approved milestones through independent, content-addressed checks."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from .process import PausedRun, execute, run_lock
from .providers import ProviderUnavailable, advisory, invoke, json_response, metadata


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=90,
                            creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or f"Git {args[0]} failed")
    return result.stdout.decode("utf-8", "replace").rstrip("\r\n")


def create_run(store, spec):
    path = Path(spec["repository"])
    root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    if root != path:
        raise ValueError(f"Choose the repository root: {root}")
    gitdir = Path(git(root, "rev-parse", "--absolute-git-dir")).resolve()
    if store.root.is_relative_to(root) or store.root.is_relative_to(gitdir):
        raise ValueError("The Rookline data directory must be outside the source checkout")
    return store.create(spec, git(root, "rev-parse", "HEAD^{commit}"))


def tail(path, count=3600):
    if not path.exists():
        return ""
    with path.open("rb") as data:
        data.seek(max(0, path.stat().st_size - count))
        return data.read().decode("utf-8", "replace").strip()


def tree(path, base, allowed, protected):
    if git(path, "rev-parse", "HEAD") != base:
        raise ValueError("Worktree HEAD changed. Keep agent edits uncommitted.")
    git(path, "add", "-A", "--", ".")
    names = git(path, "diff", "--cached", "--name-only", "-z", base).split("\0")
    names = [name for name in names if name]
    if not names:
        raise ValueError("The milestone produced no tracked changes")
    forbidden = [name for name in names if any(name == p or name.startswith(p + "/") for p in protected)]
    if forbidden:
        raise ValueError("Changes to protected verification files: " + ", ".join(forbidden[:16]))
    if allowed:
        outside = [name for name in names if not any(name == p or name.startswith(p + "/") for p in allowed)]
        if outside:
            raise ValueError("Changes outside the milestone paths: " + ", ".join(outside[:16]))
    return git(path, "write-tree")


def artifact_stamp(path, name):
    file = (path / name).resolve()
    if not file.is_relative_to(path.resolve()):
        raise ValueError(f"Artifact escapes the worktree: {name}")
    if not file.exists():
        return None
    stat = file.stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def preserve_artifacts(path, output, names, before):
    recorded = []
    for name in names:
        original = (path / name).resolve(strict=True)
        if not original.is_relative_to(path.resolve()) or not original.is_file():
            raise ValueError(f"Invalid verification artifact: {name}")
        if artifact_stamp(path, name) == before[name]:
            raise ValueError(f"Verification did not write fresh evidence: {name}")
        target = output / "artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        with target.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        recorded.append({"name": name, "path": str(target), "sha256": digest})
    return recorded


class Run:
    def __init__(self, store, run_id):
        self.store = store
        self.id = run_id
        self.spec = store.get(run_id)["spec"]
        self.root = store.directory(run_id)
        self.repository = Path(self.spec["repository"])
        self.ref = f"refs/heads/rookline/{run_id}"

    def set(self, kind, details=None, **state):
        return self.store.transition(self.id, kind, details, **state)

    def paused(self):
        return self.store.get(self.id)["pause"]

    def worktree(self, state):
        path = self.root / "worktrees" / f"{state['step'] + 1:02d}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            git(self.repository, "worktree", "add", "--detach", str(path), state["accepted"])
            self.set("worktree_ready", {"path": str(path), "base": state["accepted"]})
        if not (path / ".git").is_file():
            raise ValueError(f"The run worktree is missing its Git link: {path}")
        common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        expected = Path(git(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if common != expected:
            raise ValueError("The worktree belongs to a different repository")
        return path

    def evidence_dir(self, state):
        return self.root / "evidence" / f"{state['step'] + 1:02d}" / f"{state['attempt']:02d}"

    def reject(self, reason, phase, **state):
        self.set("attempt_rejected", {"phase": phase, "issue": reason}, phase="ready", last_error=reason, **state)

    def worker_prompt(self, state, step):
        paths = ", ".join(step["paths"]) or "the repository files needed for this milestone"
        checks = "\n".join("- " + g["name"] + ": " + json.dumps(g["argv"]) for g in step["gates"])
        return (
            f"Project goal\n{self.spec['objective']}\n\n"
            f"Milestone {state['step'] + 1}/{len(self.spec['milestones'])}: {step['title']}\n{step['task']}\n\n"
            f"Work in this supplied Git worktree. Keep edits within {paths}. "
            "Preserve unrelated behavior. Do not commit, push, merge, edit verification rules "
            "to hide failures, or touch another worktree. Return a concise summary and stop. "
            "The controller independently runs these exact checks and decides whether to advance.\n"
            f"{checks}\n\nLast observed failure (if any)\n{state['last_error'] or 'None'}"
        )

    def work(self, state, step, path):
        if state["attempt"] >= self.spec["attempts"]:
            self.set("blocked", {"issue": state["last_error"]}, phase="blocked")
            return
        next_state = {**state, "attempt": state["attempt"] + 1}
        log = self.evidence_dir(next_state) / "worker.jsonl"
        self.set("worker_started", {"adapter": step["worker"]["kind"], "attempt": next_state["attempt"]},
                 phase="working", attempt=next_state["attempt"], log=str(log))
        code, session, _ = invoke(step["worker"], "worker", path, log, self.worker_prompt(state, step),
                                  self.spec["agent_timeout"], self.paused, state.get("worker_session"))
        if code:
            self.reject(f"Worker exit {code}\n{tail(log)}", "worker", worker_session=None)
            return
        self.set("worker_finished", {"exit": code, "session": session, "log": str(log)},
                 phase="checking", worker_session=session)

    def verify(self, state, step, path):
        try:
            snapshot = self.snapshot(path, state, step)
        except ValueError as error:
            self.reject(str(error), "snapshot")
            return
        evidence = self.evidence_dir(state)
        outputs = []
        for index, check in enumerate(step["gates"], 1):
            log = evidence / f"gate-{index:02d}.log"
            before = {name: artifact_stamp(path, name) for name in check["artifacts"]}
            self.set("gate_started", {"name": check["name"], "tree": snapshot}, log=str(log))
            env = {**os.environ, "ROOKLINE_EVIDENCE": str(evidence), "ROOKLINE_TREE": snapshot}
            code = execute(check["argv"], path, log, check["timeout"], env=env, paused=self.paused)
            receipt = {"name": check["name"], "exit": code, "log": str(log), "tree": snapshot}
            self.set("gate_finished", receipt)
            if code:
                self.reject(f"{check['name']} exited {code}\n{tail(log)}", "check")
                return
            try:
                receipt["artifacts"] = preserve_artifacts(path, evidence / f"gate-{index:02d}", check["artifacts"], before)
            except (OSError, ValueError) as error:
                self.reject(f"{check['name']} could not collect its required evidence: {error}", "artifact")
                return
            outputs.append(receipt)
        if self.snapshot(path, state, step) != snapshot:
            self.reject("A check changed tracked evidence or implementation files after the snapshot was taken.", "snapshot")
            return
        (evidence / "checks.json").write_text(json.dumps(outputs, indent=2), encoding="utf-8")
        self.set("checks_passed", {"tree": snapshot, "checks": outputs}, phase="reviewing", candidate_tree=snapshot)

    def review(self, state, step, path):
        snapshot = state["candidate_tree"]
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        evidence = self.evidence_dir(state)
        checks = (evidence / "checks.json").read_text(encoding="utf-8")
        diff = git(path, "diff", "--cached", "--stat", state["accepted"])
        decision = f"Goal: {self.spec['objective']}\nTask: {step['task']}\nChanges:\n{diff}\nChecks:\n{checks}"
        if self.spec.get("reviewer"):
            log = evidence / "review.jsonl"
            self.set("review_started", {"adapter": self.spec["reviewer"]["kind"], "tree": snapshot}, log=str(log))
            prompt = (
                "Independently review these uncommitted, already tested changes. Read actual source and review "
                "the supplied check logs and artifacts, including screenshots when UI is part of the task. "
                "If required evidence cannot be read or has not been inspected, return REVISE and explain. Remain read-only. "
                'Return only JSON {"verdict":"PASS" or "REVISE","reason":"specific findings"}.\n\n' + decision
            )
            code, _, answer = invoke(self.spec["reviewer"], "reviewer", path, log, prompt,
                                     self.spec["agent_timeout"], self.paused, evidence=evidence)
            try:
                review = json_response(answer) if code == 0 else {}
                accepted = review.get("verdict") == "PASS" and bool(review.get("reason", "").strip())
            except (ValueError, AttributeError):
                accepted, review = False, {}
            if not accepted:
                self.reject("Independent review requested changes or returned no valid PASS.\n"
                            + str(review.get("reason") or tail(log))[:3600], "review")
                return
            self.set("review_passed", {"reason": review["reason"], "tree": snapshot, "log": str(log)})
        if self.spec.get("advisory_ollama"):
            try:
                summary = ", ".join(f"{check['name']} exit {check['exit']}" for check in json.loads(checks))
                note = advisory(self.spec["advisory_ollama"],
                                f"In one sentence, suggest any missing validation.\n"
                                f"Task: {step['task']}\nChanges: {diff}\nChecks: {summary}")
                self.set("local_advice", {"note": note, "tree": snapshot})
            except (OSError, ValueError) as error:
                self.set("local_advice_unavailable", {"reason": str(error)})
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        commit = git(path, "commit-tree", snapshot, "-p", state["accepted"], "-m", step["title"])
        self.set("promotion_prepared", {"commit": commit, "tree": snapshot},
                 phase="promoting", candidate=commit)

    def snapshot(self, path, state, step):
        return tree(path, state["accepted"], step["paths"], self.spec.get("protected_paths", []))

    def promote(self, state):
        candidate = state["candidate"]
        old = state["accepted"]
        try:
            current = git(self.repository, "rev-parse", "--verify", self.ref)
        except ValueError:
            current = None
        if current != candidate:
            if current != old and (current is not None or state["step"] != 0):
                raise ValueError("The result branch moved outside this run. Existing commits were preserved.")
            git(self.repository, "update-ref", self.ref, candidate, current or "0" * len(candidate))
        number = state["step"] + 1
        finished = number == len(self.spec["milestones"])
        self.set("milestone_accepted", {"step": number, "commit": candidate, "branch": self.ref},
                 phase="done" if finished else "ready", step=number, accepted=candidate,
                 attempt=0, candidate=None, candidate_tree=None, worker_session=None, last_error="", log=None)
        if finished:
            self.set("run_completed", {"branch": self.ref, "commit": candidate})

    def run(self, *, retry=False):
        with run_lock(self.root / "runner.lock"):
            state = self.store.get(self.id)["state"]
            if state["phase"] == "blocked" and retry:
                self.set("blocked_run_retried", {}, phase=state.get("blocked_from", "ready"),
                         attempt=0, blocked_from=None)
            while True:
                state = self.store.get(self.id)["state"]
                phase = state["phase"]
                if phase in ("done", "blocked"):
                    return phase
                if self.paused():
                    self.set("runner_stopped", {"phase": phase})
                    return "paused"
                step = self.spec["milestones"][state["step"]]
                try:
                    if phase == "promoting":
                        self.promote(state)
                        continue
                    path = self.worktree(state)
                    if phase == "working":
                        log = Path(state["log"])
                        session, _ = metadata(log, step["worker"]["kind"])
                        self.set("interrupted_worker_recovered", {"log": str(log), "session": session},
                                 phase="ready", worker_session=session or state.get("worker_session"),
                                 last_error="The prior process stopped before verification. Inspect the existing files and continue.")
                    elif phase in ("queued", "ready"):
                        self.work(state, step, path)
                    elif phase == "checking":
                        self.verify(state, step, path)
                    elif phase == "reviewing":
                        self.review(state, step, path)
                    else:
                        raise ValueError(f"Unrecognized run state: {phase}")
                except PausedRun:
                    self.set("runner_stopped", {"phase": phase})
                    return "paused"
                except ProviderUnavailable as error:
                    actual = self.store.get(self.id)["state"]["phase"]
                    self.set("provider_unavailable", {"issue": str(error)}, phase="blocked",
                             blocked_from="ready" if actual == "working" else actual, last_error=str(error))
                    return "blocked"
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    actual = self.store.get(self.id)["state"]["phase"]
                    self.set("blocked", {"issue": str(error), "during": actual}, phase="blocked",
                             blocked_from=actual, last_error=str(error))
                    return "blocked"
