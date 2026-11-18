"""Advance approved milestones through independent, content-addressed checks."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import time

from .process import BusyRun, PausedRun, execute, run_lock
from .providers import ProviderUnavailable, advisory, describe_provider, invoke, json_response, metadata, usage
from . import context
from .safety import atomic_write, redact, write_json
from .store import timestamp


class BudgetExceeded(ValueError):
    pass


def git_bytes(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=90,
                            creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or f"Git {args[0]} failed")
    return result.stdout


def git(repo, *args):
    return git_bytes(repo, *args).decode("utf-8", "replace").rstrip("\r\n")


def common_dir(path):
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)


def pending_dependencies(store, state):
    return [item for item in state.get("after", []) if store.get(item)["state"]["phase"] != "done"]


def checkout(path):
    root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    if root != path:
        raise ValueError(f"Choose the repository root: {root}")
    return root, common_dir(root)


def source_info(store, spec):
    root, gitdir = checkout(Path(spec["repository"]))
    if store.root.is_relative_to(root) or store.root.is_relative_to(gitdir):
        raise ValueError("The Nightweaver data directory must be outside the source checkout")
    if "project" in spec:
        project = store.project(spec["project"])
        if Path(project["repository"]) != root or Path(project["git_common_dir"]) != gitdir:
            raise ValueError(f"The checkout no longer matches the binding for project {project['name']!r}")
    base = git(root, "rev-parse", "HEAD^{commit}")
    if str(spec.get("expected_base", base)).lower() != base:
        raise ValueError(f"The chosen checkout is at {base}, expected {spec['expected_base']}")
    return {"repository": str(root), "git_common_dir": str(gitdir), "base": base}


def create_run(store, spec, *, enqueue=False, after=()):
    source = source_info(store, spec)
    if after and not enqueue:
        raise ValueError("Run dependencies require --enqueue")
    for dependency in after:
        store.directory(dependency)
        store.get(dependency)
    pack = context.capture(spec, source["base"])
    run_id = store.create({**spec, "git_common_dir": source["git_common_dir"]}, source["base"],
                          enqueue=enqueue, after=dict.fromkeys(after))
    context.save(store, run_id, pack)
    return run_id


def fork_run(store, run_id, milestone, worker=None):
    original = store.get(run_id)
    accepted = store.accepted_milestones(run_id)
    if type(milestone) is not int or not 0 <= milestone <= len(accepted):
        raise ValueError("Choose an accepted milestone number, or 0 for the original baseline")
    remaining = original["spec"]["milestones"][milestone:]
    if not remaining:
        raise ValueError("No milestones remain after that checkpoint; choose an earlier checkpoint")
    base = original["state"]["base"] if milestone == 0 else accepted[milestone - 1]["commit"]
    spec = {**original["spec"], "milestones": remaining, "title": original["spec"]["title"] + " (fork)"}
    if worker:
        spec["milestones"] = [{**step, "worker": worker, "worker_fallbacks": []} for step in remaining]
    pack = context.capture(spec, base)
    new_id = store.create(spec, base)
    context.save(store, new_id, pack)
    store.transition(new_id, "checkpoint_forked", {"source_run": run_id, "milestone": milestone, "base": base})
    return new_id


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
    names = git(path, "diff", "--cached", "--no-renames", "--name-only", "-z", base).split("\0")
    names = [name for name in names if name]
    if not names:
        raise ValueError("The milestone produced no tracked changes")
    forbidden = [name for name in names if any(name.casefold() == p.casefold() or name.casefold().startswith(p.casefold() + "/") for p in protected)]
    if forbidden:
        raise ValueError("Changes to protected verification files: " + ", ".join(forbidden[:16]))
    if allowed:
        outside = [name for name in names if not any(name.casefold() == p.casefold() or name.casefold().startswith(p.casefold() + "/") for p in allowed)]
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


def preserve_artifacts(path, output, names, before, max_bytes=32 * 1024 * 1024):
    recorded = []
    for name in names:
        original = (path / name).resolve(strict=True)
        if not original.is_relative_to(path.resolve()) or not original.is_file():
            raise ValueError(f"Invalid verification artifact: {name}")
        if artifact_stamp(path, name) == before[name]:
            raise ValueError(f"Verification did not write fresh evidence: {name}")
        if original.stat().st_size > max_bytes:
            raise ValueError(f"Verification artifact exceeds {max_bytes} bytes: {name}")
        target = output / "artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with original.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError(f"Verification artifact grew beyond {max_bytes} bytes: {name}")
        atomic_write(target, content)
        with target.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        recorded.append({"name": name, "path": str(target), "sha256": digest})
    return recorded


class Run:
    def __init__(self, store, run_id):
        self.store = store
        self.id = run_id
        row = store.get(run_id)
        self.spec = row["spec"]
        self.root = store.directory(run_id)
        self.repository = Path(self.spec["repository"])
        self.ref = row["state"].get("result_ref", f"refs/heads/rookline/{run_id}")

    def set(self, kind, details=None, **state):
        return self.store.transition(self.id, kind, details, **state)

    def paused(self):
        self.check_budget()
        return self.store.get(self.id)["pause"]

    def check_budget(self, *, call=False):
        state = self.store.get(self.id)["state"]
        limits = self.spec.get("budgets", {})
        if "max_seconds" in limits and state.get("started_at"):
            if (datetime.now(timezone.utc) - datetime.fromisoformat(state["started_at"])).total_seconds() >= limits["max_seconds"]:
                raise BudgetExceeded("Run elapsed-time budget exhausted")
        if call and "max_calls" in limits and state.get("provider_calls", 0) >= limits["max_calls"]:
            raise BudgetExceeded("Run provider-call budget exhausted")
        if call and "max_observed_tokens" in limits and state.get("observed_tokens", 0) >= limits["max_observed_tokens"]:
            raise BudgetExceeded("Run observed-token budget exhausted; usage may overshoot by the last call")

    def route(self, state, step, role):
        routes = ([step["worker"], *step.get("worker_fallbacks", [])] if role == "worker" else
                  [self.spec["reviewer"], *self.spec.get("reviewer_fallbacks", [])])
        return routes[min(state.get(role + "_route", 0), len(routes) - 1)]

    def finish_call(self, pending, *, recovered=False):
        receipt = usage(Path(pending["log"]), pending["provider"])
        state = self.store.get(self.id)["state"]
        self.set("provider_call_finished", {**pending, **receipt, "recovered": recovered,
                 "seconds": max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(pending["started"])).total_seconds())},
                 pending_call=None, observed_tokens=state.get("observed_tokens", 0) + (receipt["observed_tokens"] or 0),
                 usage_unknown_calls=state.get("usage_unknown_calls", 0) + int(receipt["observed_tokens"] is None))

    def call_provider(self, config, role, path, log, prompt, session=None, evidence=None):
        self.check_budget(call=True)
        state = self.store.get(self.id)["state"]
        pending = {"call": state.get("provider_calls", 0) + 1, "role": role, "provider": config["kind"],
                   "model": config.get("model"), "started": timestamp(), "log": str(log)}
        self.set("provider_call_started", pending, pending_call=pending, provider_calls=pending["call"])
        limits = self.spec.get("budgets", {})
        health = {"max_log_bytes": limits.get("max_log_bytes", 16 * 1024 * 1024),
                  "heartbeat": lambda info: self.set("process_heartbeat", {**info, "role": role})}
        if "idle_timeout" in limits:
            health["idle_timeout"] = limits["idle_timeout"]
        try:
            return invoke(config, role, path, log, prompt, self.spec["agent_timeout"], self.paused,
                          session, evidence=evidence, health=health)
        finally:
            self.finish_call(pending)

    def worktree(self, state):
        path = self.root / "worktrees" / f"{state['step'] + 1:02d}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            git(self.repository, "worktree", "add", "--detach", str(path), state["accepted"])
            self.set("worktree_ready", {"path": str(path), "base": state["accepted"]})
        if not (path / ".git").is_file():
            raise ValueError(f"The run worktree is missing its Git link: {path}")
        if common_dir(path) != self.gitdir:
            raise ValueError("The worktree belongs to a different repository")
        return path

    def evidence_dir(self, state):
        return self.root / "evidence" / f"{state['step'] + 1:02d}" / f"{state['attempt']:02d}"

    def reject(self, reason, phase, **state):
        current = self.store.get(self.id)["state"]
        reason = redact(reason)
        path = self.root / "worktrees" / f"{current['step'] + 1:02d}"
        try:
            git(path, "add", "-A", "--", ".")
            signature = git(path, "write-tree")
        except (ValueError, OSError):
            signature = "unknown"
        fingerprint = hashlib.sha256(f"{phase}\n{reason}\n{signature}".encode()).hexdigest()
        count = current.get("failure_count", 0) + 1 if fingerprint == current.get("failure_fingerprint") else 1
        limit = self.spec.get("retry", {}).get("repeated_failure_limit", 3)
        blocked = bool(limit and count >= limit)
        delay = min(self.spec.get("retry", {}).get("max_seconds", 60),
                    self.spec.get("retry", {}).get("initial_seconds", 2) * 2 ** min(current.get("attempts_used", 1) - 1, 10))
        self.set("attempt_rejected", {"phase": phase, "issue": reason, "same_failure_count": count},
                 following_event=("repeated_failure_stopped", {"issue": reason, "count": count}) if blocked else None,
                 phase="blocked" if blocked else "ready", last_error=reason, failure_fingerprint=fingerprint,
                 failure_count=count, retry_at=None if blocked else (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), **state)

    def worker_prompt(self, state, step):
        paths = ", ".join(step["paths"]) or "the repository files needed for this milestone"
        checks = "\n".join("- " + g["name"] + ": " + json.dumps(g.get("argv", g.get("browser"))) for g in step["gates"])
        prior = []
        for item in self.store.accepted_milestones(self.id):
            if item["step"] <= state["step"]:
                line = f"- {self.spec['milestones'][item['step'] - 1]['title']} ({item['commit']})"
                if item.get("verified_checks"):
                    line += " · Passed: " + ", ".join(item["verified_checks"])
                prior.append(line)
        prior_summary = "\n".join(prior) or "None yet"
        return (
            f"Project goal\n{self.spec['objective']}\n{self.acceptance_brief()}\n"
            f"Accepted milestones in this checkout\n{prior_summary}\n\n"
            f"Milestone {state['step'] + 1}/{len(self.spec['milestones'])}: {step['title']}\n{step['task']}\n\n"
            f"Work in this supplied Git worktree. Keep edits within {paths}. "
            "Preserve unrelated behavior. Do not commit, push, merge, edit verification rules "
            "to hide failures, or touch another worktree. Return a concise summary and stop. "
            "The controller independently runs these exact checks and decides whether to advance.\n"
            f"{checks}\n\n{context.brief(self.store, self.id)}\n\n"
            f"Operator updates\n{self.store.feedback_text(self.id) or 'None'}\n\n"
            f"Last observed failure (if any)\n{state['last_error'] or 'None'}"
        )

    def acceptance_brief(self):
        criteria = self.spec.get("acceptance", [])
        return "Project acceptance requirements\n" + "\n".join(f"- {item}" for item in criteria) + "\n" if criteria else ""

    def work(self, state, step, path):
        used = state.get("attempts_used", state["attempt"])
        if used >= self.spec["attempts"]:
            self.set("blocked", {"issue": state["last_error"]}, phase="blocked")
            return
        next_state = {**state, "attempt": state["attempt"] + 1}
        log = self.evidence_dir(next_state) / "worker.jsonl"
        config = self.route(state, step, "worker")
        self.set("worker_started", {"adapter": config["kind"], "attempt": next_state["attempt"]},
                 phase="working", attempt=next_state["attempt"], attempts_used=used + 1, log=str(log), retry_at=None,
                 worker_feedback_version=state.get("feedback_version", 0))
        code, session, answer = self.call_provider(config, "worker", path, log, self.worker_prompt(state, step),
                                                   state.get("worker_session"))
        if code:
            label = {124: "time limit", 125: "idle timeout", 126: "output limit"}.get(code, str(code))
            self.reject(f"Worker exit {label}\n{answer or tail(log)}", "worker", worker_session=None)
            return
        self.set("worker_finished", {"exit": code, "session": session, "log": str(log)},
                 phase="checking", worker_session=session, transient_failures=0)

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
            env = {**os.environ, "NIGHTWEAVER_EVIDENCE": str(evidence), "NIGHTWEAVER_TREE": snapshot,
                   "ROOKLINE_EVIDENCE": str(evidence), "ROOKLINE_TREE": snapshot}
            browser_output = evidence / f"gate-{index:02d}" / "browser"
            argv = check.get("argv")
            if check.get("kind") == "browser":
                # A crash can restart this gate in the same attempt. Keep each capture distinct.
                if browser_output.exists():
                    browser_output = Path(mkdtemp(prefix="browser-replay-", dir=browser_output.parent))
                config_path = evidence / f"gate-{index:02d}.browser.json"
                write_json(config_path, check["browser"])
                argv = [sys.executable, str(Path(__file__).with_name("browser.py")), "--config-file",
                        str(config_path), "--output", str(browser_output)]
            code = execute(argv, path, log, check["timeout"], env=env, paused=self.paused,
                           max_log_bytes=self.spec.get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024),
                           heartbeat=lambda info: self.set("process_heartbeat", {**info, "role": "gate"}))
            receipt = {"name": check["name"], "exit": code, "log": str(log), "tree": snapshot}
            try:
                receipt["artifacts"] = preserve_artifacts(path, evidence / f"gate-{index:02d}", check["artifacts"], before,
                                                           check.get("max_artifact_bytes", 32 * 1024 * 1024))
                if check.get("kind") == "browser":
                    for file in sorted(browser_output.iterdir()):
                        if not file.is_file() or file.is_symlink() or file.stat().st_size > check["max_artifact_bytes"]:
                            raise ValueError(f"Invalid or oversized browser evidence: {file.name}")
                        with file.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        receipt["artifacts"].append({"name": file.name, "path": str(file), "sha256": digest})
                    report = json.loads((browser_output / "browser.json").read_text(encoding="utf-8"))
                    failures = [{"viewport": view.get("viewport"), "stage": view.get("failed_at"),
                                 "error": next(iter(view.get("errors") or []), "")[:600]}
                                for view in report.get("viewports", []) if not view.get("passed")]
                    if failures:
                        receipt["browser_failures"] = failures
                    if code == 0 and report.get("passed") is not True:
                        raise ValueError("Browser returned no passing receipt")
            except (OSError, ValueError) as error:
                receipt["artifact_error"] = str(error)
                self.set("gate_finished", receipt)
                self.reject(f"{check['name']} exited {code} and could not collect its required evidence: {error}\n{tail(log)}", "artifact")
                return
            self.set("gate_finished", receipt)
            if code:
                diagnostic = (json.dumps(receipt["browser_failures"], ensure_ascii=False)
                              if receipt.get("browser_failures") else tail(log))
                self.reject(f"{check['name']} exited {code}\n{diagnostic}", "check")
                return
            outputs.append(receipt)
        if self.snapshot(path, state, step) != snapshot:
            self.reject("A check changed tracked evidence or implementation files after the snapshot was taken.", "snapshot")
            return
        write_json(evidence / "checks.json", outputs)
        self.set("checks_passed", {"tree": snapshot, "checks": outputs}, phase="reviewing", candidate_tree=snapshot)

    def review(self, state, step, path):
        if state.get("feedback_version", 0) != state.get("worker_feedback_version", 0):
            self.reject("New operator feedback arrived. Apply the saved update before acceptance.", "feedback", worker_session=None)
            return
        snapshot = state["candidate_tree"]
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        evidence = self.evidence_dir(state)
        checks = (evidence / "checks.json").read_text(encoding="utf-8")
        diff = git(path, "diff", "--cached", "--stat", state["accepted"])
        decision = (f"Goal: {self.spec['objective']}\n{self.acceptance_brief()}"
                    f"Task: {step['task']}\nChanges:\n{diff}\nChecks:\n{checks}\n"
                    f"{context.brief(self.store, self.id)}\nOperator updates\n{self.store.feedback_text(self.id) or 'None'}")
        if self.spec.get("reviewer"):
            log = evidence / f"review-{state.get('provider_calls', 0) + 1:04d}.jsonl"
            config = self.route(state, step, "reviewer")
            self.set("review_started", {"adapter": config["kind"], "tree": snapshot}, log=str(log), retry_at=None)
            prompt = (
                "Independently review these uncommitted, already tested changes. Read actual source and review "
                "the supplied check logs and artifacts, including screenshots when UI is part of the task. "
                "If required evidence cannot be read or has not been inspected, return REVISE and explain. Remain read-only. "
                'Return only JSON {"verdict":"PASS" or "REVISE","reason":"specific findings"}.\n\n' + decision
            )
            if state["step"] + 1 == len(self.spec["milestones"]):
                prompt += ("\nThis is the final milestone. Also check the complete project objective against the current "
                           "worktree and supplied evidence. Return REVISE for any unfulfilled part of that objective.")
            code, _, answer = self.call_provider(config, "reviewer", path, log, prompt, evidence=evidence)
            try:
                review = json_response(answer) if code == 0 else {}
                accepted = review.get("verdict") == "PASS" and bool(review.get("reason", "").strip())
            except (ValueError, AttributeError):
                accepted, review = False, {}
            if not accepted:
                self.reject("Independent review requested changes or returned no valid PASS.\n"
                            + str(review.get("reason") or answer or tail(log))[:3600], "review")
                return
            self.set("review_passed", {"reason": review["reason"], "tree": snapshot, "log": str(log)})
        if self.spec.get("advisory_ollama"):
            self.check_budget(call=True)
            current = self.store.get(self.id)["state"]
            pending = {"call": current.get("provider_calls", 0) + 1, "role": "advisory", "provider": "ollama",
                       "model": self.spec["advisory_ollama"]["model"], "started": timestamp(),
                       "log": str(evidence / f"advice-{current.get('provider_calls', 0) + 1:04d}.json")}
            self.set("provider_call_started", pending, pending_call=pending, provider_calls=pending["call"])
            try:
                config = dict(self.spec["advisory_ollama"])
                if self.spec.get("budgets", {}).get("max_seconds"):
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(current["started_at"])).total_seconds()
                    config["timeout"] = max(.1, min(config.get("timeout", 90), self.spec["budgets"]["max_seconds"] - elapsed))
                summary = ", ".join(f"{check['name']} exit {check['exit']}" for check in json.loads(checks))
                note = advisory(config,
                                f"In one sentence, suggest any missing validation.\n"
                                f"Task: {step['task']}\nChanges: {diff}\nChecks: {summary}")
                self.set("local_advice", {"note": note, "tree": snapshot})
                write_json(Path(pending["log"]), {"note": redact(note)})
            except (OSError, ValueError) as error:
                self.set("local_advice_unavailable", {"reason": str(error)})
            finally:
                self.finish_call(pending)
            self.check_budget()
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        if self.store.get(self.id)["state"].get("feedback_version", 0) != state.get("feedback_version", 0):
            self.reject("Operator feedback changed during review. Apply the saved update.", "feedback", worker_session=None)
            return
        commit = git(path, "commit-tree", snapshot, "-p", state["accepted"], "-m", step["title"])
        self.set("promotion_prepared", {"commit": commit, "tree": snapshot},
                 phase="promoting", candidate=commit)

    def snapshot(self, path, state, step):
        return tree(path, state["accepted"], step["paths"], self.spec.get("protected_paths", []))

    def promote(self, state):
        if state.get("feedback_version", 0) != state.get("worker_feedback_version", 0):
            self.reject("New operator feedback arrived before promotion. Apply the saved update.", "feedback", worker_session=None)
            return
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
        passed = [gate["name"] for gate in self.spec["milestones"][number - 1]["gates"]]
        completion = ("run_completed", {"branch": self.ref, "commit": candidate}) if finished else None
        self.set("milestone_accepted", {"step": number, "commit": candidate, "branch": self.ref,
                 "tree": state.get("candidate_tree"), "verified_checks": passed},
                 following_event=completion,
                 phase="done" if finished else "ready", step=number, accepted=candidate,
                 attempt=0, attempts_used=0, candidate=None, candidate_tree=None,
                 worker_session=None, last_error="", log=None, retry_at=None, failure_count=0,
                 failure_fingerprint=None, worker_route=0, reviewer_route=0, transient_failures=0)

    def run(self, *, retry=False, defer_retries=False):
        with run_lock(self.root / "runner.lock"), ExitStack() as project_lock:
            state = self.store.get(self.id)["state"]
            if state["phase"] == "done" or (state["phase"] == "blocked" and not retry):
                return state["phase"]
            pending = pending_dependencies(self.store, state)
            if pending:
                raise BusyRun("Waiting for prerequisite runs: " + ", ".join(pending))
            if state["phase"] == "blocked" and retry:
                self.set("blocked_run_retried", {}, phase=state.get("blocked_from") or "ready",
                         attempts_used=0, blocked_from=None, provider_issue=None, retry_at=None,
                         transient_failures=0, failure_count=0, failure_fingerprint=None)
            if state.get("pending_call"):
                self.finish_call(state["pending_call"], recovered=True)
            if not state.get("started_at"):
                self.set("run_started", {}, started_at=timestamp())
            self.gitdir = None
            while True:
                state = self.store.get(self.id)["state"]
                phase = state["phase"]
                if phase in ("done", "blocked"):
                    return phase
                if self.store.get(self.id)["pause"]:
                    self.set("runner_stopped", {"phase": phase})
                    return "paused"
                step = self.spec["milestones"][state["step"]]
                try:
                    self.check_budget()
                    if state.get("retry_at") and datetime.fromisoformat(state["retry_at"]) > datetime.now(timezone.utc):
                        if defer_retries:
                            return "waiting"
                        time.sleep(.2)
                        continue
                    if self.gitdir is None:
                        gitdir = common_dir(self.repository)
                        if Path(self.spec.get("git_common_dir", gitdir)) != gitdir:
                            raise ValueError("The source checkout now points to a different Git repository")
                        project_lock.enter_context(run_lock(gitdir / "nightweaver.lock",
                                                            "Another Nightweaver run owns this Git repository"))
                        self.gitdir = gitdir
                        if not (self.root / "context.json").is_file() and (self.spec.get("context_files") or self.spec.get("repository_map")):
                            context.save(self.store, self.id, context.capture(self.spec, state["base"]))
                    if phase == "promoting":
                        self.promote(state)
                        continue
                    path = self.worktree(state)
                    if phase == "working":
                        log = Path(state["log"])
                        session, _ = metadata(log, self.route(state, step, "worker")["kind"])
                        self.set("interrupted_worker_recovered", {"log": str(log), "session": session},
                                 phase="ready", worker_session=session or state.get("worker_session"),
                                 attempts_used=max(0, state.get("attempts_used", state["attempt"]) - 1),
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
                except BusyRun:
                    raise
                except BudgetExceeded as error:
                    actual = self.store.get(self.id)["state"]["phase"]
                    self.set("budget_exhausted", {"issue": str(error)}, phase="blocked", last_error=str(error),
                             blocked_from="ready" if actual == "working" else actual)
                    return "blocked"
                except ProviderUnavailable as error:
                    current = self.store.get(self.id)["state"]
                    actual = current["phase"]
                    role = "reviewer" if actual == "reviewing" else "worker"
                    config = self.route(current, step, role)
                    provider = {**describe_provider(config), "role": role, "reason": error.kind}
                    routes = step.get("worker_fallbacks", []) if role == "worker" else self.spec.get("reviewer_fallbacks", [])
                    index = current.get(role + "_route", 0)
                    next_phase = "ready" if actual == "working" else actual
                    refund = {"attempts_used": max(0, current.get("attempts_used", 1) - 1)} if role == "worker" else {}
                    if index < len(routes):
                        self.set("provider_fallback", {"from": provider, "to": describe_provider(routes[index])},
                                 phase=next_phase, **{role + "_route": index + 1}, worker_session=None,
                                 last_error=str(error), transient_failures=0, retry_at=None, **refund)
                        continue
                    failures = current.get("transient_failures", 0)
                    policy = self.spec.get("retry", {})
                    if error.kind in ("usage_limit", "transient") and failures < policy.get("transient_attempts", 2):
                        delay = min(policy.get("max_seconds", 60), policy.get("initial_seconds", 2) * 2 ** failures)
                        self.set("provider_retry_scheduled", {"issue": str(error), "seconds": delay, **provider},
                                 phase=next_phase, transient_failures=failures + 1, worker_session=None, **refund,
                                 retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
                        continue
                    self.set("provider_unavailable", {"issue": str(error), **provider}, phase="blocked",
                             blocked_from="ready" if actual == "working" else actual, last_error=str(error),
                             provider_issue=provider)
                    return "blocked"
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    actual = self.store.get(self.id)["state"]["phase"]
                    self.set("blocked", {"issue": str(error), "during": actual}, phase="blocked",
                             blocked_from=actual, last_error=str(error))
                    return "blocked"
