"""Advance approved milestones through independent, content-addressed checks."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
import subprocess
import time

from .process import BusyRun, PausedRun, run_lock
from .providers import ProviderUnavailable, advisory, describe_provider, invoke, json_response, metadata, usage
from .checks import contract_hash, evidence_folder, observed_pass, run_gate, verify_inputs, verify_saved_sample
from .insights import budget_status, failure_excerpt
from . import context
from .safety import atomic_write, redact, write_json
from .store import timestamp


class BudgetExceeded(ValueError):
    pass


def git_bytes(repo, *args, attributes_from=None):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    if attributes_from is not None:
        env["GIT_ATTR_SOURCE"] = attributes_from
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=90,
                            creationflags=subprocess.CREATE_NO_WINDOW, env=env)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or f"Git {args[0]} failed")
    return result.stdout


def git(repo, *args):
    return git_bytes(repo, *args).decode("utf-8", "replace").rstrip("\r\n")


def common_dir(path):
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)


def pending_dependencies(store, state):
    keys = state.get("after", [])
    phases = store.dependency_phases(keys)
    return [item for item in keys if phases.get(item) != "done"]


def checkout(path):
    root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    if root != path:
        raise ValueError(f"Choose the repository root: {root}")
    return root, common_dir(root)


def verify_project_binding(store, spec, root, gitdir):
    if spec.get("project"):
        bound = store.project(spec["project"])
        if Path(bound["repository"]).resolve() != root.resolve() or Path(bound["git_common_dir"]).resolve() != gitdir:
            raise ValueError(f"The project binding changed. The checkout no longer matches {bound['name']!r}")


def directory_identity(path):
    info = Path(path).stat()
    return f"{info.st_dev:x}:{info.st_ino:x}" if info.st_ino else None


def verify_registered_repository(spec, gitdir):
    if Path(spec.get("git_common_dir", gitdir)) != gitdir:
        raise ValueError("The source checkout now points to a different Git repository")
    expected = spec.get("git_directory_identity")
    if expected and directory_identity(gitdir) != expected:
        raise ValueError("The registered Git directory was replaced at its pinned location")


def source_info(store, spec):
    root, gitdir = checkout(Path(spec["repository"]))
    if store.root.is_relative_to(root) or store.root.is_relative_to(gitdir):
        raise ValueError("The Nightweaver data directory must be outside the source checkout")
    verify_project_binding(store, spec, root, gitdir)
    base = git(root, "rev-parse", "HEAD^{commit}")
    if str(spec.get("expected_base", base)).lower() != base:
        raise ValueError(f"The chosen checkout is at {base}, expected {spec['expected_base']}")
    return {"repository": str(root), "git_common_dir": str(gitdir), "base": base}


def create_run(store, spec, *, enqueue=False, after=()):
    source = source_info(store, spec)
    if spec.get("git_directory_identity"):
        verify_registered_repository(spec, Path(source["git_common_dir"]))
    if after and not enqueue:
        raise ValueError("Run dependencies require --enqueue")
    for dependency in after:
        store.directory(dependency)
        store.get(dependency)
    pack = context.capture(spec, source["base"])
    run_id = store.create({**spec, "git_common_dir": source["git_common_dir"],
                           "git_directory_identity": directory_identity(source["git_common_dir"])}, source["base"],
                          enqueue=enqueue, after=dict.fromkeys(after), context_pack=pack)
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
    spec = {**original["spec"], "expected_base": base, "milestones": remaining, "title": original["spec"]["title"] + " (fork)"}
    if any("proof" in gate for step in remaining for gate in step["gates"]):
        spec["proof_base"] = original["spec"].get("proof_base", original["state"]["base"])
    repo = Path(spec["repository"])
    gitdir = common_dir(repo)
    verify_registered_repository(spec, gitdir)
    spec["git_directory_identity"] = directory_identity(gitdir)
    verify_project_binding(store, spec, repo, gitdir)
    if worker:
        spec["milestones"] = [{**step, "worker": worker, "worker_fallbacks": []} for step in remaining]
    pack = context.capture(spec, base)
    new_id = store.create(spec, base, context_pack=pack)
    store.transition(new_id, "checkpoint_forked", {"source_run": run_id, "milestone": milestone, "base": base})
    return new_id


def tail(path, count=3600):
    if not path.exists():
        return ""
    with path.open("rb") as data:
        data.seek(max(0, path.stat().st_size - count))
        return data.read().decode("utf-8", "replace").strip()


def tree(path, base, allowed, protected):
    from .contracts import under
    if git(path, "rev-parse", "HEAD") != base:
        raise ValueError("Worktree HEAD changed. Keep agent edits uncommitted.")
    git(path, "add", "-A", "--", ".")
    names = git(path, "diff", "--cached", "--no-renames", "--name-only", "-z", base).split("\0")
    names = [name for name in names if name]
    if not names:
        raise ValueError("The milestone produced no tracked changes")
    forbidden = [name for name in names if under(name, protected)]
    if forbidden:
        raise ValueError("Changes to protected verification files: " + ", ".join(forbidden[:16]))
    if allowed:
        outside = [name for name in names if not under(name, allowed)]
        if outside:
            raise ValueError("Changes outside the milestone paths: " + ", ".join(outside[:16]))
    return git(path, "write-tree")


def artifact_stamp(path, name, max_bytes=32 * 1024 * 1024):
    file = (path / name).resolve()
    if not file.is_relative_to(path.resolve()):
        raise ValueError(f"Artifact escapes the worktree: {name}")
    if not file.exists():
        return None
    stat = file.stat()
    digest = None
    if file.is_file() and stat.st_size <= max_bytes:
        with file.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) <= max_bytes:
            digest = hashlib.sha256(content).hexdigest()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino, digest


def preserve_artifacts(path, output, names, before, max_bytes=32 * 1024 * 1024):
    recorded = []
    for name in names:
        original = (path / name).resolve(strict=True)
        if not original.is_relative_to(path.resolve()) or not original.is_file():
            raise ValueError(f"Invalid verification artifact: {name}")
        stat = original.stat()
        if stat.st_size > max_bytes:
            raise ValueError(f"Verification artifact exceeds {max_bytes} bytes: {name}")
        target = output / "artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with original.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError(f"Verification artifact grew beyond {max_bytes} bytes: {name}")
        digest = hashlib.sha256(content).hexdigest()
        if (stat.st_mtime_ns, stat.st_size, stat.st_ino, digest) == before[name]:
            raise ValueError(f"Verification did not write fresh evidence: {name}")
        atomic_write(target, content)
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
        pursue = self.spec.get("pursue")
        routes = ([step["worker"], *step.get("worker_fallbacks", [])] if role == "worker" else
                  [pursue["judge"], *pursue["judge_fallbacks"]] if role == "judge" else
                  [pursue["planner"]] if role == "planner" else
                  [self.spec["reviewer"], *self.spec.get("reviewer_fallbacks", [])])
        return routes[min(state.get(role + "_route", 0), len(routes) - 1)]

    def finish_call(self, pending, *, recovered=False):
        receipt = usage(Path(pending["log"]), pending["provider"])
        state = self.store.get(self.id)["state"]
        self.set("provider_call_finished", {**pending, **receipt, "recovered": recovered,
                 "seconds": None if recovered else max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(pending["started"])).total_seconds())},
                 pending_call=None, observed_tokens=state.get("observed_tokens", 0) + (receipt["observed_tokens"] or 0),
                 usage_unknown_calls=state.get("usage_unknown_calls", 0) + int(receipt["observed_tokens"] is None))

    def call_provider(self, config, role, path, log, prompt, session=None, evidence=None, images=(), mode=None):
        self.check_budget(call=True)
        state = self.store.get(self.id)["state"]
        milestone = role in ("worker", "reviewer")
        pending = {"call": state.get("provider_calls", 0) + 1, "role": role, "provider": config["kind"],
                   "model": config.get("model"), "started": timestamp(), "log": str(log),
                   "step": state["step"] + int(milestone), "attempt": state["attempt"]}
        if not milestone:
            pending["wave"] = state.get("wave", 1)
        if "effort" in config:
            pending["effort"] = config["effort"]
        self.set("provider_call_started", pending, pending_call=pending, provider_calls=pending["call"])
        limits = self.spec.get("budgets", {})
        health = {"max_log_bytes": limits.get("max_log_bytes", 16 * 1024 * 1024),
                  "heartbeat": lambda info: self.set("process_heartbeat", {**info, "role": role})}
        if "idle_timeout" in limits:
            health["idle_timeout"] = limits["idle_timeout"]
        try:
            return invoke(config, mode or role, path, log, prompt, self.spec["agent_timeout"], self.paused,
                          session, evidence=evidence, health=health, **({"images": list(images)} if images else {}))
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

    def reject(self, reason, phase, *, exit_code=None, failed_gates=(), repairable=True, **state):
        from .repair import describe, next_worker
        current = self.store.get(self.id)["state"]
        reason = redact(reason)
        path = self.root / "worktrees" / f"{current['step'] + 1:02d}"
        changed = []
        try:
            git(path, "add", "-A", "--", ".")
            signature = git(path, "write-tree")
            names = git(path, "diff", "--cached", "--no-renames", "--name-only", "-z", current["accepted"], "--")
            changed = [n for n in names.split("\0") if n]
        except (ValueError, OSError):
            signature = "unknown"
        fingerprint = hashlib.sha256(f"{phase}\n{reason}\n{signature}".encode()).hexdigest()
        count = current.get("failure_count", 0) + 1 if fingerprint == current.get("failure_fingerprint") else 1
        limit = self.spec.get("retry", {}).get("repeated_failure_limit", 3)
        step = self.spec["milestones"][current["step"]]
        active = current.get("worker_route", 0)
        used = current.get("attempts_used", current["attempt"])
        repair = self.spec.get("repair", {})
        eligible = repairable and phase in ("worker", "snapshot", "check", "review")
        streak = current.get("route_rejections", 0) + int(eligible)
        target = next_worker(step, active) if eligible and repair.get("switch_after") and streak >= repair["switch_after"] else None
        handoff = target is not None and used < self.spec["attempts"]
        repeated = bool(limit and count >= limit and not handoff)
        exhausted = used >= self.spec["attempts"]
        blocked = repeated or exhausted
        location = {"step": current["step"] + 1, "attempt": current["attempt"]}
        event = None
        if handoff:
            routes = [step["worker"], *step["worker_fallbacks"]]
            event = ("repair_handoff", {**location, "from": describe(routes[active]), "to": describe(routes[target]),
                     "from_route": active, "to_route": target, "tree": signature, "phase": phase, "failed_gates": list(failed_gates)})
            state.update(worker_route=target, worker_session=None)
        elif repeated:
            event = ("repeated_failure_stopped", {"issue": reason, "count": count})
        elif exhausted:
            event = ("blocked", {"issue": reason})
        elif eligible and repair.get("fresh_session_after") and streak >= repair["fresh_session_after"]:
            state["worker_session"] = None
        delay = min(self.spec.get("retry", {}).get("max_seconds", 60),
                    self.spec.get("retry", {}).get("initial_seconds", 2) * 2 ** min(current.get("attempts_used", 1) - 1, 10))
        self.set("attempt_rejected", {**location, "phase": phase, "issue": reason, "same_failure_count": count, "exit": exit_code,
                 "tree": signature, "route": active, "failed_gates": list(failed_gates), "worker": describe(self.route(current, step, "worker")),
                 "changed_paths": changed[:12], "changed_path_count": len(changed), "diagnostic_log": current.get("log")},
                 following_event=event, phase="blocked" if blocked else "ready", last_error=reason,
                 failure_fingerprint=None if handoff else fingerprint, route_rejections=0 if handoff else streak,
                 failure_count=0 if handoff else count, retry_at=None if blocked else (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), **state)

    def worker_prompt(self, state, step):
        paths = ", ".join(step["paths"]) or "the repository files needed for this milestone"
        checks = "\n".join("- " + g["name"] + ": " + json.dumps(g, ensure_ascii=False) for g in step["gates"])
        following = self.spec["milestones"][state["step"] + 1:state["step"] + 2]
        next_task = (f"Next milestone after acceptance: {following[0]['title']} — {following[0]['task'][:800]}"
                     if following else "This is the last planned milestone. The goal is judged afterwards and more milestones may follow."
                     if self.spec.get("pursue") else "This is the final milestone.")
        prior = []
        from .pursuit import reachable
        ours = reachable(self, state) if state.get("judgments") else None
        for item in self.store.accepted_milestones(self.id):
            if item["step"] <= state["step"] and (ours is None or item["commit"] in ours):
                line = f"- {self.spec['milestones'][item['step'] - 1]['title']} ({item['commit']})"
                if item.get("verified_checks"):
                    line += " · Passed: " + ", ".join(item["verified_checks"])
                prior.append(line)
        prior_summary = "\n".join(prior) or "None yet"
        from .repair import failure_brief
        history = failure_brief(self.store, self.id, state, self.spec.get("repair", {}).get("history", 0))
        from .pursuit import worker_brief
        pursuit = worker_brief(self.spec, state)
        return (
            f"Project goal\n{self.spec['objective']}\n{self.acceptance_brief()}{chr(10) + pursuit if pursuit else ''}\n"
            f"Accepted milestones in this checkout\n{prior_summary}\n\n"
            f"Milestone {state['step'] + 1}/{len(self.spec['milestones'])}: {step['title']}\n{step['task']}\n\n"
            f"{next_task}\n\n"
            f"Work in this supplied Git worktree. Keep edits within {paths}. "
            f"Protected paths: {', '.join(self.spec.get('protected_paths', [])) or 'none declared'}. "
            "Preserve unrelated behavior. Do not commit, push, merge, edit verification rules "
            "to hide failures, or touch another worktree. Return a concise summary and stop. "
            "The controller independently runs these exact checks and decides whether to advance.\n"
            f"{checks}\n\n{context.brief(self.store, self.id)}\n\n"
            f"Budget remaining before this call: {budget_status(self.spec, state)['remaining']}\n"
            f"Operator updates\n{self.store.feedback_text(self.id) or 'None'}\n\n"
            f"{history}Last observed failure (if any)\n{state['last_error'] or 'None'}\n"
            f"Previous diagnostic log: {state.get('log') if state.get('last_error') else 'None'}"
        )

    def acceptance_brief(self):
        criteria = self.spec.get("acceptance", [])
        return "Project acceptance requirements\n" + "\n".join(f"- {item}" for item in criteria) + "\n" if criteria else ""

    def work(self, state, step, path):
        used = state.get("attempts_used", state["attempt"])
        if used >= self.spec["attempts"]:
            self.set("blocked", {"issue": state["last_error"]}, phase="blocked")
            return
        self.check_budget(call=True)
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
            self.reject(f"Worker exit {label}\n{answer or tail(log)}", "worker", exit_code=code, worker_session=None)
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
        batch = None
        if any(check.get("stability_runs", 1) > 1 for check in step["gates"]):
            identity = [state["step"], state["attempt"], snapshot]
            saved = state.get("verification_batch") or {}
            if saved.get("identity") != identity:
                saved = {"id": uuid4().hex, "identity": identity}
                self.set("verification_batch_started", saved, verification_batch=saved)
            batch = saved["id"]
        outputs = []
        failures = []
        for index, check in enumerate(step["gates"], 1):
            receipts = self.verify_gate(path, state, step, index, check, snapshot, batch, failures)
            if receipts is None:
                return
            outputs.extend(receipts)
        if failures:
            from .repair import combined_failure
            self.reject(combined_failure(failures), "check", failed_gates=[f["gate"] for f in failures])
            return
        if self.snapshot(path, state, step) != snapshot:
            self.reject("A check changed tracked evidence or implementation files after the snapshot was taken.", "snapshot")
            return
        write_json(evidence / "checks.json", outputs)
        digest = hashlib.sha256((evidence / "checks.json").read_bytes()).hexdigest()
        self.set("checks_passed", {"tree": snapshot, "checks": outputs}, phase="reviewing",
                 candidate_tree=snapshot, verification_batch=None, checks_sha256=digest)

    def verify_gate(self, path, state, step, index, check, snapshot, batch, failures=None):
        count = check.get("stability_runs", 1)
        digest = contract_hash(check)
        evidence = self.evidence_dir(state)
        location = {"step": state["step"] + 1, "attempt": state["attempt"], "gate_index": index}
        receipts = []
        if count > 1:
            if self.snapshot(path, state, step) != snapshot:
                self.reject("A previous check changed the candidate before stability verification.", "snapshot")
                return None
            receipts = self.store.gate_samples(self.id, batch, index)
            if receipts:
                verify_inputs(check, path)
                for sample, receipt in enumerate(receipts, 1):
                    if (receipt["tree"], receipt["contract_sha256"], receipt.get("sample")) != (snapshot, digest, sample):
                        raise ValueError("Saved gate samples do not match the active verification")
                    if observed_pass(receipt) is not None:
                        verify_saved_sample(receipt, evidence)
            location.update(verification_batch=batch, stability_runs=count)

        # A mixed first round earns one whole confirmation round on the same candidate.
        for round_index in range(2 if count > 1 else 1):
            for number in range(round_index * count + 1, (round_index + 1) * count + 1):
                if self.paused():
                    raise PausedRun()
                if number <= len(receipts):
                    receipt = receipts[number - 1]
                else:
                    sample_location = {**location, "sample": number} if count > 1 else location
                    receipt = self.observe_gate(check, path, state, step, snapshot, sample_location)
                    receipts.append(receipt)
                if observed_pass(receipt) is None:
                    self.invalid_gate(check, receipt)
                    return None
            outcomes = [observed_pass(r) for r in receipts[round_index * count:(round_index + 1) * count]]
            if all(outcomes):
                accepted = receipts[round_index * count:(round_index + 1) * count]
                if round_index:
                    accepted[0] = {**accepted[0], "prior_mixed_round": receipts[:count]}
                return accepted
            if not any(outcomes):
                failed = receipts[round_index * count:(round_index + 1) * count]
                last = failed[-1]
                messages = []
                for observation in failed:
                    if observation.get("junit", {}).get("issues"):
                        message = "\n".join(observation["junit"]["issues"])
                    elif catalog := observation.get("catalog"):
                        parts = [catalog["diagnostic"]] if catalog.get("diagnostic") else []
                        parts.append("Catalog violations " + json.dumps(catalog["violations"], ensure_ascii=True))
                        parts.extend(json.dumps(ex, ensure_ascii=True) for ex in catalog.get("examples", []))
                        message = "\n".join(parts)
                    elif observation.get("browser_failures"):
                        message = json.dumps(observation["browser_failures"], ensure_ascii=False)
                    else:
                        message = failure_excerpt(observation["log"])
                    if message not in messages:
                        messages.append(message)
                diagnostic = "\n".join(m[:3600 // len(messages)] for m in messages)
                if count > 1:
                    diagnostic = f"All {count} samples in this round failed.\n{diagnostic}"
                verdict = f"failed its JUnit policy (command exit {last['exit']})" if last.get("junit", {}).get("passed") is False else f"exited {last['exit']}"
                if failures is not None and self.spec.get("repair", {}).get("collect_failures"):
                    failures.append({"gate": check["name"], "diagnostic": f"{verdict}\n{diagnostic}"})
                    return []
                self.reject(f"{check['name']} {verdict}\n{diagnostic}", "check", failed_gates=[check["name"]])
                return None
        reason = f"{check['name']} produced both passes and failures in two rounds on tree {snapshot[:12]}."
        self.set("verification_inconsistent", {**location, "name": check["name"], "tree": snapshot,
                 "issue": reason, "logs": [r["log"] for r in receipts]}, phase="blocked",
                 blocked_from="checking", last_error=reason)
        return None

    def observe_gate(self, check, path, state, step, snapshot, location):
        folder = evidence_folder(self.evidence_dir(state), location["gate_index"])
        log = folder / "gate.log"
        self.set("gate_started", {**location, "name": check["name"], "tree": snapshot,
                 "contract_sha256": contract_hash(check), "log": str(log)}, log=str(log))
        baseline = self.spec.get("proof_base", state["base"]) if "proof" in check else state["base"]
        receipt = run_gate(check, path, folder, snapshot, paused=self.paused,
                           **({"baseline": baseline} if check.get("kind") == "catalog" or "proof" in check else {}),
                           max_log_bytes=self.spec.get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024),
                           heartbeat=lambda info: self.set("process_heartbeat", {**info, "role": "gate"}))
        receipt.update(location)
        if log.is_file() or observed_pass(receipt) is not None:
            try:
                with log.open("rb") as stream:
                    receipt["log_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                receipt.update(execution_error=True, artifact_error=f"Cannot preserve gate log: {error}")
        if observed_pass(receipt) is not None:
            try:
                if self.snapshot(path, state, step) != snapshot:
                    receipt["source_error"] = "The gate changed tracked candidate files during verification."
            except ValueError as error:
                receipt["source_error"] = str(error)
        if "proof" in receipt:
            receipt["proof"]["passed"] = observed_pass(receipt) is True
        self.set("gate_finished", receipt)
        return receipt

    def invalid_gate(self, check, receipt):
        if receipt.get("source_error"):
            self.reject(receipt["source_error"], "snapshot", repairable=False)
        elif receipt.get("input_error") or receipt.get("execution_error"):
            self.set("blocked", {"during": "checking", "issue": receipt["artifact_error"]},
                     phase="blocked", blocked_from="checking", last_error=receipt["artifact_error"])
        else:
            self.reject(f"{check['name']} exited {receipt['exit']} and could not collect its required evidence: "
                        f"{receipt.get('artifact_error')}\n{failure_excerpt(receipt['log'])}", "artifact")

    def verified_checks(self, state):
        evidence = self.evidence_dir(state)
        try:
            raw = (evidence / "checks.json").read_bytes()
            if state.get("checks_sha256") and hashlib.sha256(raw).hexdigest() != state["checks_sha256"]:
                raise ValueError("The accepted check manifest changed")
            checks = json.loads(raw)
            if not state.get("checks_sha256"):
                saved = next((e["details"] for e in reversed(self.store.events(self.id, limit=None))
                              if e["kind"] == "checks_passed"), {})
                if saved.get("tree") != state["candidate_tree"] or saved.get("checks") != redact(checks):
                    raise ValueError("The legacy check manifest does not match its recorded verification")
            for receipt in checks:
                for sample in [receipt, *receipt.get("prior_mixed_round", [])]:
                    verify_saved_sample(sample, evidence)
            return raw.decode("utf-8")
        except (ValueError, OSError, KeyError) as error:
            self.set("blocked", {"during": "checking", "issue": str(error)}, phase="blocked",
                     blocked_from="checking", last_error=str(error), verification_batch=None)
            return None

    def review(self, state, step, path):
        if state.get("feedback_version", 0) != state.get("worker_feedback_version", 0):
            self.reject("New operator feedback arrived. Apply the saved update before acceptance.", "feedback", worker_session=None)
            return
        snapshot = state["candidate_tree"]
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        evidence = self.evidence_dir(state)
        checks = self.verified_checks(state)
        if checks is None:
            return
        diff = git(path, "diff", "--cached", "--stat", state["accepted"])
        decision = (f"Goal: {self.spec['objective']}\n{self.acceptance_brief()}"
                    f"Budget remaining before review: {budget_status(self.spec, state)['remaining']}\nTask: {step['task']}\nChanges:\n{diff}\nChecks:\n{checks}\n"
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
            if state["step"] + 1 == len(self.spec["milestones"]) and not self.spec.get("pursue"):
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
                       "step": state["step"] + 1, "attempt": state["attempt"],
                       "log": str(evidence / f"advice-{current.get('provider_calls', 0) + 1:04d}.json")}
            if "think" in self.spec["advisory_ollama"]:
                pending["think"] = self.spec["advisory_ollama"]["think"]
            self.set("provider_call_started", pending, pending_call=pending, provider_calls=pending["call"])
            try:
                config = dict(self.spec["advisory_ollama"])
                if self.spec.get("budgets", {}).get("max_seconds"):
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(current["started_at"])).total_seconds()
                    config["timeout"] = max(.1, min(config.get("timeout", 90), self.spec["budgets"]["max_seconds"] - elapsed))
                summary = ", ".join(f"{check['name']} exit {check['exit']}" for check in json.loads(checks))
                inference = {}
                note = advisory(config,
                                f"In one sentence, suggest any missing validation.\n"
                                f"Task: {step['task']}\nChanges: {diff}\nChecks: {summary}", receipt=inference)
                self.set("local_advice", {"note": note, "tree": snapshot, "inference": inference})
                write_json(Path(pending["log"]), {"note": redact(note), "inference": inference})
            except (OSError, ValueError) as error:
                self.set("local_advice_unavailable", {"reason": str(error)})
                if self.spec["advisory_ollama"].get("required"):
                    self.set("local_advice_required", {"model": self.spec["advisory_ollama"]["model"]},
                             phase="blocked", blocked_from="reviewing", last_error=f"Required local review failed: {error}")
                    return
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
        verify_registered_repository(self.spec, common_dir(self.repository))
        try:
            current = git(self.repository, "rev-parse", "--verify", self.ref)
        except ValueError:
            current = None
        if current != candidate:
            if current != old and (current is not None or state["step"] != 0):
                raise ValueError("The result branch moved outside this run. Existing commits were preserved.")
            if self.verified_checks(state) is None:
                return
            git(self.repository, "update-ref", self.ref, candidate, current or "0" * len(candidate))
        number = state["step"] + 1
        finished = number == len(self.spec["milestones"])
        passed = [gate["name"] for gate in self.spec["milestones"][number - 1]["gates"]]
        pursuing = finished and bool(self.spec.get("pursue"))
        wave = state.get("wave", 1)
        completion = (("wave_completed", {"wave": wave, "commit": candidate, "branch": self.ref}) if pursuing else
                      ("run_completed", {"branch": self.ref, "commit": candidate}) if finished else None)
        self.set("milestone_accepted", {"step": number, "commit": candidate, "branch": self.ref,
                 "tree": state.get("candidate_tree"), "verified_checks": passed},
                 following_event=completion, **({"wave": wave, "judge_attempts": 0, "judge_route": 0} if pursuing else {}),
                 phase="judging" if pursuing else "done" if finished else "ready", step=number, accepted=candidate,
                 attempt=0, attempts_used=0, candidate=None, candidate_tree=None,
                 worker_session=None, last_error="", log=None, retry_at=None, failure_count=0,
                 failure_fingerprint=None, worker_route=0, reviewer_route=0, transient_failures=0, route_rejections=0)

    def run(self, *, retry=False, defer_retries=False, only_operational=False):
        with run_lock(self.root / "runner.lock"), ExitStack() as project_lock:
            row = self.store.get(self.id)
            state = row["state"]
            if only_operational:
                from .reports import metrics
                if (row["pause"] or state["phase"] != "blocked" or metrics(self.store, self.id)["outcome"]
                        not in {"provider_blocked", "execution_blocked", "verification_inconsistent"}):
                    return "skipped"
            if state["phase"] == "done" or (state["phase"] == "blocked" and not retry):
                return state["phase"]
            if row["pause"]:
                return "paused"
            pending = pending_dependencies(self.store, state)
            if pending:
                raise BusyRun("Waiting for prerequisite runs: " + ", ".join(pending))
            # Acquire ownership before resetting retries, finalizing an interrupted
            # call or starting the elapsed-time clock. Contention is read-only.
            try:
                self.gitdir = common_dir(self.repository)
                verify_registered_repository(self.spec, self.gitdir)
                project_lock.enter_context(run_lock(self.gitdir / "nightweaver.lock",
                                                    "Another Nightweaver run owns this Git repository"))
            except BusyRun:
                raise
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self.set("blocked", {"issue": str(error), "during": state["phase"]},
                         phase="blocked", blocked_from=state.get("blocked_from") or state["phase"], last_error=str(error))
                return "blocked"
            if state["phase"] == "blocked" and retry:
                self.set("blocked_run_retried", {}, phase=state.get("blocked_from") or "ready",
                         attempts_used=0, blocked_from=None, provider_issue=None, retry_at=None, verification_batch=None,
                         transient_failures=0, failure_count=0, failure_fingerprint=None, route_rejections=0,
                         **({"judge_attempts": 0, "plan_attempts": 0, "judge_ledger": None} if self.spec.get("pursue") else {}))
            if state.get("pending_call"):
                self.finish_call(state["pending_call"], recovered=True)
            if not state.get("started_at"):
                self.set("run_started", {}, started_at=timestamp())
            context_checked = False
            while True:
                state = self.store.get(self.id)["state"]
                phase = state["phase"]
                if phase in ("done", "blocked"):
                    return phase
                if self.store.get(self.id)["pause"]:
                    self.set("runner_stopped", {"phase": phase})
                    return "paused"
                if phase in ("judging", "planning") and not self.spec.get("pursue"):
                    raise ValueError(f"Unrecognized run state: {phase}")
                step = None if phase in ("judging", "planning") else self.spec["milestones"][state["step"]]
                try:
                    self.check_budget()
                    if state.get("retry_at") and datetime.fromisoformat(state["retry_at"]) > datetime.now(timezone.utc):
                        if defer_retries:
                            return "waiting"
                        time.sleep(.2)
                        continue
                    if not context_checked:
                        if self.spec.get("context_sha256"):
                            context.load_pack(self.store, self.id)
                        if not (self.root / "context.json").is_file() and (self.spec.get("context_files") or self.spec.get("repository_map")):
                            context.save(self.store, self.id, context.capture(self.spec, state["base"]))
                        context_checked = True
                    if phase == "promoting":
                        self.promote(state)
                        continue
                    if phase in ("judging", "planning"):
                        from . import pursuit
                        (pursuit.judge if phase == "judging" else pursuit.plan_wave)(self, state)
                        continue
                    path = self.worktree(state)
                    if phase == "working":
                        log = Path(state["log"])
                        session, _ = metadata(log, self.route(state, step, "worker")["kind"])
                        interrupted = "The prior process stopped before verification. Inspect the existing files and continue."
                        previous = state.get("last_error") or ""
                        if previous and not previous.startswith(interrupted):
                            interrupted += "\nPrevious diagnostic: " + previous
                        elif previous:
                            interrupted = previous
                        self.set("interrupted_worker_recovered", {"log": str(log), "session": session},
                                 phase="ready", worker_session=session or state.get("worker_session"),
                                 attempts_used=max(0, state.get("attempts_used", state["attempt"]) - 1),
                                 last_error=interrupted)
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
                    role = {"reviewing": "reviewer", "judging": "judge", "planning": "planner"}.get(actual, "worker")
                    config = self.route(current, step, role)
                    provider = {**describe_provider(config), "role": role, "reason": error.kind}
                    pursue = self.spec.get("pursue", {})
                    routes = {"worker": (step or {}).get("worker_fallbacks", []), "reviewer": self.spec.get("reviewer_fallbacks", []),
                              "judge": pursue.get("judge_fallbacks", []), "planner": []}[role]
                    index = current.get(role + "_route", 0)
                    next_phase = "ready" if actual == "working" else actual
                    refund = {"attempts_used": max(0, current.get("attempts_used", 1) - 1) , "worker_session": None} if role == "worker" else {}
                    if index < len(routes):
                        self.set("provider_fallback", {"from": provider, "to": describe_provider(routes[index])},
                                 phase=next_phase, **{role + "_route": index + 1}, **({"route_rejections": 0} if role == "worker" else {}),
                                 last_error=str(error), transient_failures=0, retry_at=None, **refund)
                        continue
                    failures = current.get("transient_failures", 0)
                    policy = self.spec.get("retry", {})
                    if error.kind in ("usage_limit", "transient") and failures < policy.get("transient_attempts", 2):
                        delay = min(policy.get("max_seconds", 60), policy.get("initial_seconds", 2) * 2 ** failures)
                        self.set("provider_retry_scheduled", {"issue": str(error), "seconds": delay, **provider},
                                 phase=next_phase, transient_failures=failures + 1, **refund,
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
