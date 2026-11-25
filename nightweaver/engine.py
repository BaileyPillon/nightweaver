"""Advance approved milestones through independent, content-addressed checks."""

from contextlib import ExitStack
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
import shutil
import subprocess
import tempfile
import time

from .process import BusyRun, ExtendedLimits, PausedRun, WindowsJob, run_lock
from .providers import ProviderUnavailable, advisory, describe_provider, invoke, json_response, metadata, reset_after, same_model, served_models, usage
from .checks import contract_hash, evidence_folder, observed_pass, run_gate, verify_inputs, verify_saved_sample
from .insights import budget_status, failure_excerpt
from . import context, notify
from .safety import atomic_write, redact, write_json
from .store import run_clock, timestamp


class BudgetExceeded(ValueError):
    pass


GIT_TIMEOUT = 90
WORKTREE_ADD_TIMEOUT = 900
SETTLE_POLL, SETTLE_QUIET = 1.0, 5  # seconds between looks at an unfinished checkout; looks with no index lock before git is given up on
IDLE_DEFAULT = 900
GATE_TIMEOUT_CAP = 14400  # a gate's retried timeout never exceeds 4 hours
GATE_TIMEOUT = "Gate exceeded its wall-clock timeout"  # what process.execute reports when it ends a gate at its limit; test_release_0191_runtime holds the two together
BASELINE_TIMEOUT = "Baseline did not establish the requested proof: " + GATE_TIMEOUT  # a proof gate's baseline run ended at the limit (proof.apply_proof)
PARK_MARGIN, PARK_UNPARSED, PARK_UNPARSED_MAX = 90, 1800, 7200


def use_git_timeout(spec):
    """The run's git_timeout is the default for every git call this process makes until another run sets its own."""
    global GIT_TIMEOUT
    GIT_TIMEOUT = spec.get("git_timeout", 90)


class SurvivingJob(WindowsJob):
    """A Job that ends its processes when terminate() says so and not when its handle closes. A runner that dies during a checkout
    (a killed console, a crash) leaves git to finish it and release its lock, rather than cutting it in half."""

    def __enter__(self):
        super().__enter__()
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(ExtendedLimits()), ctypes.sizeof(ExtendedLimits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.kernel.CloseHandle(self.handle)
            raise error
        return self


def run_owned(argv, limit, env):
    """subprocess.run for a git command that starts children, such as `worktree add`, which runs `reset --hard`.

    The command and what it starts live in one Job. A timeout ends them all and returns at once, where subprocess.run
    kills only the parent and then waits for an orphaned child to release the pipes, which can take as long as the checkout."""
    with tempfile.TemporaryDirectory(prefix="nw-git-", ignore_cleanup_errors=True) as temp, SurvivingJob() as job:
        out, err = Path(temp, "out"), Path(temp, "err")
        with out.open("wb") as stdout, err.open("wb") as stderr:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW, env=env)
            try:
                try:
                    job.attach(process.pid)
                except OSError:
                    pass  # the kill below still ends the parent
                process.wait(timeout=limit)
            finally:
                if process.poll() is None:
                    try:
                        job.terminate()
                    except OSError:
                        pass
                    process.kill()
                    process.wait(timeout=30)
        return subprocess.CompletedProcess(argv, process.returncode, out.read_bytes(), err.read_bytes())


def abandon_worktree(repo, args):
    """Take a half-made `git worktree add` back out so that --retry starts clean. Git holds the entry locked while the
    checkout runs and `prune` skips locked entries, so it is unlocked and removed first. Never raises."""
    folders = [arg for arg in args[2:] if not arg.startswith("-")]
    if not folders:
        return
    path = folders[0]
    for step in (("unlock", path), ("remove", "--force", "--force", path)):
        try:
            git(repo, "worktree", *step, timeout=60)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pass
    shutil.rmtree(path, ignore_errors=True)
    try:
        git(repo, "worktree", "prune", "--expire", "now", timeout=60)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass


def initializing(path):
    """The admin folder of a worktree whose `git worktree add` has not finished (git keeps it locked as 'initializing'), else None."""
    try:
        link = Path(path, ".git").read_text(encoding="utf-8").strip()
        admin = Path(link.partition(":")[2].strip())
        return admin if link.startswith("gitdir:") and (admin / "locked").read_text(encoding="utf-8").strip() == "initializing" else None
    except OSError:
        return None


def settle_worktree(repo, path, wait=None):
    """Whether `path`, if anything is there, is a finished checkout that can be reused. False means it was cleared away (or never
    existed) and the caller makes it again.

    A runner that died during `git worktree add` leaves the entry locked while git, which outlives it, finishes the checkout: wait for
    that. If git died too (a reboot, a killed process tree) nothing ever releases the lock or its index.lock: the checkout is taken out
    for good once no process holds the index lock, or after `wait` seconds (the add timeout by default)."""
    path = Path(path)
    if not path.exists():
        return False
    if not (path / ".git").is_file():
        if path.is_dir() and not any(path.iterdir()):  # git died before it linked the checkout
            abandon_worktree(repo, ("worktree", "add", str(path)))
            return False
        return True  # not ours to judge: the caller reports the missing link
    admin = initializing(path)
    if admin is None:
        return True
    deadline = time.monotonic() + (WORKTREE_ADD_TIMEOUT if wait is None else wait)
    unheld = 0
    while admin is not None:
        lock = admin / "index.lock"
        stale = False
        if lock.exists():
            unheld = 0
            try:
                lock.unlink()  # a running git has it open and Windows refuses; a dead one's lock is just a file
                stale = True
            except OSError:
                pass
        else:
            unheld += 1
            stale = unheld >= SETTLE_QUIET
        if stale or time.monotonic() >= deadline:
            abandon_worktree(repo, ("worktree", "add", str(path)))
            return False
        time.sleep(SETTLE_POLL)
        admin = initializing(path)
    return True


def git_bytes(repo, *args, attributes_from=None, timeout=None, env=None):
    from .safety import scrub_env  # repository hooks and filters run inside git, so no provider credential reaches it

    env = {**scrub_env(os.environ), "GIT_OPTIONAL_LOCKS": "0", **(env or {})}
    if attributes_from is not None:
        env["GIT_ATTR_SOURCE"] = attributes_from
    limit = timeout or GIT_TIMEOUT
    adding = args[:2] == ("worktree", "add")
    if adding:
        limit = max(limit, WORKTREE_ADD_TIMEOUT)  # a checkout of a very large repository takes minutes
    argv = ["git", "-C", str(repo), *args]
    try:
        result = (run_owned(argv, limit, env) if adding else
                  subprocess.run(argv, capture_output=True, timeout=limit, creationflags=subprocess.CREATE_NO_WINDOW, env=env))
    except subprocess.TimeoutExpired:
        if adding:
            abandon_worktree(repo, args)
        raise
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or f"Git {args[0]} failed")
    return result.stdout


def git(repo, *args, timeout=None, env=None):
    return git_bytes(repo, *args, timeout=timeout, env=env).decode("utf-8", "replace").rstrip("\r\n")


FALLBACK_IDENTITY = ("Nightweaver", "nightweaver@localhost")


def complaint(error):
    """The first line git said, without the advice that follows it."""
    return next((line.strip() for line in str(error).splitlines() if line.strip()), "identity unknown")


def missing_identity(repo):
    """{role: git's first complaint} for the commit identities (author, committer) git cannot resolve in `repo`; empty when both resolve
    or when `repo` is not a repository (later steps report that themselves)."""
    try:
        git(repo, "rev-parse", "--git-dir")
    except (ValueError, OSError):
        return {}
    found = {}
    for role in ("author", "committer"):
        try:
            git(repo, "var", f"GIT_{role.upper()}_IDENT")
        except ValueError as error:
            found[role] = complaint(error)
    return found


def quoted(path, powershell=False):
    """`path` as one command-line argument: bare when neither cmd nor PowerShell reads anything special in it, else in double quotes (cmd keeps
    & ( ) ' inside them). PowerShell also expands $ and the backtick there and ends the string at a curly double quote, so for it a path holding
    one of those is single-quoted instead, with any single quote (straight or curly) doubled."""
    text = str(path)
    if re.fullmatch(r"[\w.:\\/@+=,-]+", text):
        return text
    if powershell and re.search("[$`\u201c\u201d\u201e]", text):
        return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", text) + "'"
    return f'"{text}"'


def identity_message(repo, found):
    """What to tell the operator when git has no identity for `repo`: which role is missing and the exact commands that fix it, quoted so that
    they can be pasted into cmd or PowerShell (a PowerShell form follows when the path needs one of its own)."""
    def fix(where):
        return f'git -C {where} config user.name "Your Name" and git -C {where} config user.email "you@example.com"'
    where, shell = quoted(repo), quoted(repo, True)
    return (f"Git has no {' or '.join(found)} identity in {repo} ({'; '.join(found.values())}). Nightweaver creates the accepted commits there, "
            f"so set one first: {fix(where)}" + (f" (in PowerShell: {fix(shell)})" if shell != where else "")
            + " (or use git config --global to set it for every repository).")


def identity_error(repo):
    """None when git can author commits in `repo`, else a message that names the exact fix."""
    found = missing_identity(repo)
    return identity_message(repo, found) if found else None


def require_identity(repo):
    if message := identity_error(repo):
        raise ValueError(message)


def commit_tree(path, tree, parent, message):
    """The commit of `tree` on `parent`. A repository that resolves no identity still gets the commit, under FALLBACK_IDENTITY for the
    role git cannot fill, so a run started before the identity checks existed (or one whose config changed) is not lost at promotion.
    Only the missing side is replaced: a configured identity is never overridden."""
    env = {f"GIT_{role.upper()}_{key}": value for role in missing_identity(path) for key, value in zip(("NAME", "EMAIL"), FALLBACK_IDENTITY)}
    return git(path, "commit-tree", tree, "-p", parent, "-m", message, env=env or None)


def identity_fallback(repo, commit):
    """{"name", "email", "roles"} when `commit` carries FALLBACK_IDENTITY as its author or committer, else None."""
    try:
        author, committer = (line.split("\t") for line in git(repo, "show", "-s", "--format=%an\t%ae%n%cn\t%ce", commit).splitlines()[:2])
    except ValueError:
        return None
    roles = [role for role, who in (("author", author), ("committer", committer)) if tuple(who) == FALLBACK_IDENTITY]
    return {"name": FALLBACK_IDENTITY[0], "email": FALLBACK_IDENTITY[1], "roles": roles} if roles else None


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
    # A pursuit that reverted holds milestones of discarded waves: a checkpoint is a milestone of the current build, and the
    # discarded work is not carried into the fork.
    cut = 0 if milestone == 0 else accepted[milestone - 1].get("step", milestone)
    skipped = {item.get("step") for item in store.accepted_milestones(run_id, discarded=True)} - {item.get("step") for item in accepted}
    remaining = [step for number, step in enumerate(original["spec"]["milestones"], 1) if number > cut and number not in skipped]
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
    if milestone and spec.get("pursue", {}).get("explore"):
        spec["pursue"] = {k: v for k, v in spec["pursue"].items() if k != "explore"}  # the tournament picks a first milestone's direction
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


def tool_dropping(name):
    """Files that browsers and agent tooling leave behind, never part of a milestone's work."""
    *folders, leaf = name.split("/")
    leaf = leaf.casefold()
    return leaf in ("debug.log", "chrome_debug.log") or leaf.endswith(".dmp") or ".playwright-mcp" in (f.casefold() for f in folders)


def stage(path, base, keep=()):
    """Stage the worktree, then take tool droppings back out of the index, so the index holds what the candidate commit holds.

    Returns (changes, dropped): every (status, name) the worktree differs from base by, and the new files left out.
    tree(), reject() and the no-progress check all take their identity from here, so they always agree on the build.
    Only a new file can be a dropping, and never one that is a deliverable: a path inside what the milestone names in `keep`, or
    one that tracked content moved to (a rename git can see, or the file name of a tracked dropping-shaped file the milestone deleted)."""
    from .contracts import under
    git(path, "add", "-A", "--", ".")
    fields = git(path, "diff", "--cached", "--no-renames", "--name-status", "-z", base).split("\0")
    changes = list(zip(fields[0::2], fields[1::2]))
    dropped = [name for status, name in changes if status == "A" and tool_dropping(name) and not under(name, keep)]
    if dropped:
        # The content of a tracked file that moved is never lost: it is a rename, or it keeps the name of the dropping-shaped file it left.
        renamed = set(git(path, "diff", "--cached", "-M", "--diff-filter=R", "--name-only", "-z", base).split("\0"))
        left = {name.rpartition("/")[2].casefold() for status, name in changes if status == "D" and tool_dropping(name)}
        dropped = [name for name in dropped if name not in renamed and name.rpartition("/")[2].casefold() not in left]
    for start in range(0, len(dropped), 100):
        git(path, "--literal-pathspecs", "reset", "-q", "--", *dropped[start:start + 100])
    return changes, dropped


def remove_droppings(path, names):
    """Delete untracked droppings from the worktree: a gate must never see a file the accepted commit will not contain."""
    root = os.path.realpath(path)
    for name in names:
        target = Path(path, *name.split("/"))
        if os.path.commonpath([root, os.path.realpath(target.parent)]) != root:
            raise ValueError(f"A tool left a file outside the worktree: {name}")
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            raise ValueError(f"A tool left a file that cannot be removed: {name} ({error.strerror or error})") from error
        for folder in target.parents:
            if os.path.realpath(folder) == root:
                break
            try:
                folder.rmdir()
            except OSError:
                break


def tree(path, base, allowed, protected, removed=None):
    from .contracts import under
    if git(path, "rev-parse", "HEAD") != base:
        raise ValueError("Worktree HEAD changed. Keep agent edits uncommitted.")
    changes, dropped = stage(path, base, allowed)
    remove_droppings(path, dropped)
    if removed is not None:
        removed.extend(dropped)
    skipped = set(dropped)
    names = [name for _, name in changes if name not in skipped]
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


def counted_usage(receipt, log, kind, state):
    """Codex reports usage cumulatively across a resumed thread, so count only what this call added.

    Returns the receipt to record (observed_tokens is the delta, reported_tokens the raw figure) and the state to keep."""
    session = metadata(Path(log), kind)[0] if kind == "codex" and receipt["observed_tokens"] is not None else None
    if not session:
        return receipt, {}
    sessions, channels = state.get("session_usage", {}), state.get("session_channels", {})
    seen = channels.get(session, {})

    def added(total, before):
        return total if before is None or total < before else total - before

    raw = receipt["observed_tokens"]
    counted = {**receipt, "observed_tokens": added(raw, sessions.get(session)), "reported_tokens": raw, "session": session,
               "token_channels": {key: added(value, seen.get(key)) for key, value in receipt["token_channels"].items()}}
    return counted, {"session_usage": {**sessions, session: raw}, "session_channels": {**channels, session: receipt["token_channels"]}}


def park_plan(spec, state, error, now, kind):
    """Decide whether a usage limit is waited out or blocks the run, and until when."""
    policy = spec.get("retry", {})
    number = state.get("parks", 0) + 1
    reset = error.reset_at or reset_after(str(error), now)
    parsed = reset is not None
    until = (max(reset, now) + timedelta(seconds=PARK_MARGIN) if parsed else
             now + timedelta(seconds=min(PARK_UNPARSED_MAX, PARK_UNPARSED * 2 ** (number - 1))))
    wait = (until - now).total_seconds()
    name = {"codex": "Codex", "claude": "Claude", "deepseek": "DeepSeek", "gemini": "Gemini"}.get(kind, kind)
    when = (reset if parsed else until).astimezone()
    clock = when.strftime("%H:%M" if when.date() == now.astimezone().date() else "%b %d %H:%M")
    limits = spec.get("budgets", {})
    # The wait counts against the budget (parked time is active time), so the deadline is the time the budget has left from now.
    deadline = (now + timedelta(seconds=limits["max_seconds"] - run_clock(state, now)[1])
                if limits.get("max_seconds") and state.get("started_at") else None)
    allowed, longest = policy.get("max_parks", 8), policy.get("max_park_seconds", 21600)
    if number > allowed:
        block = (f"{name} usage limit reached and waiting is disabled (retry.max_parks is 0)" if not allowed else
                 f"{name} usage limit was hit again after {number - 1} waits, the most this run allows (retry.max_parks {allowed})")
    elif wait > longest:
        block = (f"{name} usage limit {'resets at ' + clock if parsed else 'gave no reset time and the next retry is at ' + clock}, "
                 f"{wait / 3600:.1f} hours away, longer than this run waits (retry.max_park_seconds {longest})")
    elif deadline and until > deadline:
        block = (f"{name} usage limit resets at {clock}, after this run's time budget" if parsed else
                 f"{name} usage limit gave no reset time and the next retry at {clock} is after this run's time budget")
    else:
        block = None
    return {"block": block, "park": number, "parsed": parsed, "until": until.isoformat(timespec="milliseconds"),
            "reset": reset.isoformat(timespec="seconds") if parsed else None, "seconds": round(wait)}


def call_served(run, pending, state):
    """The model ids the provider's log reports for a finished call. A log that cannot be read never fails the call."""
    try:
        config = {"kind": pending["provider"]}
        if pending["role"] in ("worker", "reviewer", "judge", "planner"):
            try:
                step = run.spec["milestones"][state["step"]] if pending["role"] in ("worker", "reviewer") else None
                from . import pursuit
                routed = (pursuit.active_route(run, state)[1] if pending["role"] == "judge" and run.spec.get("pursue") and state.get("phase") == "judging"
                          else run.route(state, step, pending["role"]))
                if routed.get("kind") == pending["provider"] and routed.get("model") == pending.get("model"):
                    config = routed  # carries the profile directory a Codex rollout is looked up in
            except (KeyError, IndexError, TypeError):
                pass
        return served_models(config, pending["log"])
    except Exception:
        return {"models": [], "source": None}


def model_drift(pending, served, seen):
    """model_mismatch events for reported ids that differ from the model a route asked for, once per route and id."""
    requested = pending.get("model")
    events, seen = [], list(seen)
    for name in served["models"] if requested else ():
        key = "|".join(str(part) for part in (pending.get("role"), pending["provider"], requested, name))
        if key in seen or same_model(requested, name):
            continue
        seen.append(key)
        events.append(("model_mismatch", {"provider": pending["provider"], "role": pending.get("role"), "requested": requested, "served": name,
                       "source": served["source"], "call": pending.get("call"),
                       "issue": f"{pending['provider']} {pending.get('role')} asked for {requested} but was served {name}"}))
    return events, seen


def gate_timed_out(receipt):
    """Whether the controller ended a gate at its wall-clock limit before it could deliver a verdict: the gate itself, or (a proof gate) its
    run on the baseline commit, which proof.apply_proof reports as the reason the baseline did not establish the proof."""
    if not receipt.get("execution_error"):
        return False
    error = receipt.get("artifact_error")
    return error == BASELINE_TIMEOUT or (receipt.get("exit") is None and error == GATE_TIMEOUT)


def timed_out_for_good(receipt):
    """Whether a receipt that Run.run_timed_gate returned is of a gate the controller ended at its limit for good: after its retries (the
    receipt says so in timeout_retried), or at the first stop when the plan allows none. Such a gate was not judged, so it is neither a pass
    nor a failure of the code it ran on."""
    return gate_timed_out(receipt) or bool((receipt.get("timeout_retried") or {}).get("timed_out"))


def timeout_issue(check, receipt):
    """What a run says when `check` timed out for good (timed_out_for_good): which gate and limits, that nothing was judged, what to do.
    The milestone path words its block the same way (invalid_gate); a judge's ledger and a candidate's checks use this one."""
    said = receipt["artifact_error"] if (receipt.get("timeout_retried") or {}).get("timed_out") else (
        f"Gate {check['name']!r} exceeded its wall-clock timeout of {check['timeout']} s, and the plan allows no retry. The worker's change was not judged.")
    return said + " " + timeout_advice(check["timeout"])


def timeout_advice(planned):
    """What an operator can do about a gate that timed out for good. A run keeps the plan it was created with (no command edits it), so more
    time means the edited plan added as a new run; a resume runs the gate again with the limits the plan has."""
    first = ('Raise "timeout" on this gate in the plan (it allows up to 7200 seconds) and add that plan as a new run: a run keeps the plan it was '
             'created with, so editing the plan file does not change this one.' if planned < 7200 else
             'This gate already has the longest "timeout" the plan allows (7200 seconds): make it faster or split it in the plan and add that plan '
             'as a new run (a run keeps the plan it was created with).')
    return first + " If the machine was only busy, resume this run with --retry to run the gate again."


class Run:
    def __init__(self, store, run_id):
        self.store = store
        self.id = run_id
        row = store.get(run_id)
        self.spec = row["spec"]
        self.root = store.directory(run_id)
        self.repository = Path(self.spec["repository"])
        self.ref = row["state"].get("result_ref", f"refs/heads/rookline/{run_id}")
        self.gate_limits = {}  # (tree, gate) -> the timeouts a gate has been run with on that tree, the plan's first: see run_timed_gate

    def set(self, kind, details=None, **state):
        result = self.store.transition(self.id, kind, details, **state)
        notify.on_transition(self.store, self.id, self.spec["title"], kind, details, state.get("following_event"), result)
        return result

    def paused(self):
        self.check_budget()
        return self.store.get(self.id)["pause"]

    def check_budget(self, *, call=False):
        """Raise BudgetExceeded when a budget is spent. budgets.max_seconds is active time: the time since the run started less the stretches
        it sat blocked (waiting for a person) or paused, which the state's ledger holds (see store.run_clock). Time parked on a usage limit
        still counts: the run is waiting on its own clock, by design, and park_plan refuses a wait that would outlast the budget."""
        state = self.store.get(self.id)["state"]
        limits = self.spec.get("budgets", {})
        if "max_seconds" in limits and state.get("started_at"):
            wall, active = run_clock(state, datetime.fromisoformat(timestamp()))
            if active >= limits["max_seconds"]:
                raise BudgetExceeded("Run elapsed-time budget exhausted" + (
                    f" ({active / 3600:.1f} h active; {(wall - active) / 3600:.1f} h blocked or paused is not counted)" if wall - active >= 60 else ""))
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

    def finish_call(self, pending, *, recovered=False, succeeded=False):
        state = self.store.get(self.id)["state"]
        receipt, sessions = counted_usage(usage(Path(pending["log"]), pending["provider"]), pending["log"], pending["provider"], state)
        served = call_served(self, pending, state)
        mismatches, seen = model_drift(pending, served, state.get("model_mismatches", []))
        self.set("provider_call_finished", {**pending, **receipt, "recovered": recovered,
                 "seconds": None if recovered else max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(pending["started"])).total_seconds()),
                 "served_model": served["models"][0] if served["models"] else None,
                 **({"served_models": served["models"]} if len(served["models"]) > 1 else {}),
                 **({"served_source": served["source"]} if served["source"] else {})},
                 following_event=mismatches or None, pending_call=None, observed_tokens=state.get("observed_tokens", 0) + (receipt["observed_tokens"] or 0),
                 usage_unknown_calls=state.get("usage_unknown_calls", 0) + int(receipt["observed_tokens"] is None), **sessions,
                 **({"parks": 0} if succeeded and state.get("parks") else {}), **({"model_mismatches": seen} if mismatches else {}))

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
        elif role in ("worker", "reviewer", "judge", "planner") and self.spec.get("idle_default"):
            # A silent stall otherwise runs to the full agent timeout. Only plans written by 0.19 carry the marker, so a saved
            # 0.18 plan keeps the behavior it was approved with.
            health["idle_timeout"] = IDLE_DEFAULT
        returned = False
        try:
            result = invoke(config, mode or role, path, log, prompt, self.spec["agent_timeout"], self.paused,
                            session, evidence=evidence, health=health, **({"images": list(images)} if images else {}))
            returned = True
            return result
        finally:
            self.finish_call(pending, succeeded=returned)

    def worktree(self, state):
        use_git_timeout(self.spec)
        path = self.root / "worktrees" / f"{state['step'] + 1:02d}"
        if not settle_worktree(self.repository, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                git(self.repository, "worktree", "add", "--detach", str(path), state["accepted"])
            except subprocess.TimeoutExpired as error:
                # git_bytes has already unlocked, removed and pruned the half-made checkout, so --retry starts clean.
                raise ValueError(f"Creating the run worktree took more than {int(error.timeout)} seconds on this repository. "
                                 "Raise git_timeout in the plan, then resume with --retry.") from error
            self.set("worktree_ready", {"path": str(path), "base": state["accepted"]})
        if not (path / ".git").is_file():
            raise ValueError(f"The run worktree is missing its Git link: {path}")
        if common_dir(path) != self.gitdir:
            raise ValueError("The worktree belongs to a different repository")
        return path

    def evidence_dir(self, state):
        return self.root / "evidence" / f"{state['step'] + 1:02d}" / f"{state['attempt']:02d}"

    def explores(self, state):
        """Whether milestone 1 starts with the design-direction tournament that this run has not yet held."""
        return bool(self.spec.get("pursue", {}).get("explore")) and state["step"] == 0 and not state.get("explored")

    def reject(self, reason, phase, *, exit_code=None, failed_gates=(), repairable=True, finding=None, seen=None, **state):
        """`seen` is the operator-feedback version the judgment of this build was made with (a reviewer's REVISE), which can be older
        than the one in the store when the operator spoke while the reviewer was working."""
        from .repair import blocked_message, cause_line, describe, droppings_note, fingerprint, next_worker
        current = self.store.get(self.id)["state"]
        reason = redact(reason)
        if phase in ("check", "snapshot", "artifact", "review"):
            reason = droppings_note(current.get("droppings"), current["step"] + 1, current["attempt"]) + reason
        path = self.root / "worktrees" / f"{current['step'] + 1:02d}"
        changed = []
        try:
            changes, dropped = stage(path, current["accepted"], self.spec["milestones"][current["step"]]["paths"])
            signature = git(path, "write-tree")
            left_out = set(dropped)
            changed = [name for _, name in changes if name not in left_out]
        except (ValueError, OSError):
            signature = "unknown"
        # The identity of a failure is what failed on which build, never how it was worded.
        identity = fingerprint(phase, signature, failed_gates, exit_code, reason, current.get("feedback_version", 0))
        count = current.get("failure_count", 0) + 1 if identity == current.get("failure_fingerprint") else 1
        limit = self.spec.get("retry", {}).get("repeated_failure_limit", 3)
        step = self.spec["milestones"][current["step"]]
        active = current.get("worker_route", 0)
        used = current.get("attempts_used", current["attempt"])
        repair = self.spec.get("repair", {})
        eligible = repairable and phase in ("worker", "snapshot", "check", "review", "no_progress")
        streak = current.get("route_rejections", 0) + int(eligible)
        target = next_worker(step, active) if eligible and repair.get("switch_after") and streak >= repair["switch_after"] else None
        handoff = target is not None and used < self.spec["attempts"]
        repeated = bool(limit and count >= limit and not handoff)
        exhausted = used >= self.spec["attempts"]
        blocked = repeated or exhausted
        location = {"step": current["step"] + 1, "attempt": current["attempt"]}
        event = cause = None
        message = reason
        if blocked:
            # An operator reads the cause first, then the last failure, then the worker's own account.
            cause = cause_line(phase, list(failed_gates), count, used, self.spec["attempts"], repeated)
            message = blocked_message(cause, reason, state.get("worker_summary", current.get("worker_summary", "")))
        if handoff:
            routes = [step["worker"], *step["worker_fallbacks"]]
            event = ("repair_handoff", {**location, "from": describe(routes[active]), "to": describe(routes[target]),
                     "from_route": active, "to_route": target, "tree": signature, "phase": phase, "failed_gates": list(failed_gates)})
            state.update(worker_route=target, worker_session=None)
        elif repeated:
            event = ("repeated_failure_stopped", {"issue": reason, "count": count, "cause": cause})
        elif exhausted:
            event = ("blocked", {"issue": reason, "cause": cause})
        elif eligible and repair.get("fresh_session_after") and streak >= repair["fresh_session_after"]:
            state["worker_session"] = None
        # Remember this milestone's rejected builds so work() can tell when the worker changed nothing.
        trees = current.get("rejected_trees", []) if current.get("rejected_step") == current["step"] else []
        remembered = trees[-1].get("issue", reason) if phase == "no_progress" and trees else reason
        # A review that gave no readable feedback is not feedback the worker could have ignored.
        kept = "review_unreadable" if phase == "review" and not finding else phase
        older = [{"tree": item["tree"], "phase": item["phase"], "feedback": item.get("feedback", 0)} for item in trees]
        # The feedback version a rejection answers is the one its judge worked from. A no-progress rejection judges nothing new.
        asked = (trees[-1].get("feedback", 0) if phase == "no_progress" and trees else
                 current.get("feedback_version", 0) if seen is None else seen)
        state.update(rejected_trees=[*older, {"tree": signature, "phase": kept, "issue": remembered[:3000], "feedback": asked}][-4:],
                     rejected_step=current["step"])
        delay = min(self.spec.get("retry", {}).get("max_seconds", 60),
                    self.spec.get("retry", {}).get("initial_seconds", 2) * 2 ** min(current.get("attempts_used", 1) - 1, 10))
        self.set("attempt_rejected", {**location, "phase": phase, "issue": reason, "same_failure_count": count, "exit": exit_code,
                 "tree": signature, "route": active, "failed_gates": list(failed_gates), "worker": describe(self.route(current, step, "worker")),
                 "changed_paths": changed[:12], "changed_path_count": len(changed), "diagnostic_log": current.get("log"),
                 **({"finding": redact(finding)[:3000]} if finding else {})},
                 following_event=event, phase="blocked" if blocked else "ready", last_error=message,
                 failure_fingerprint=None if handoff else identity, route_rejections=0 if handoff else streak,
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
        from .repair import conflict_brief
        notes = conflict_brief(state.get("pursuit_conflicts"))
        advice = state.get("advice_note")
        if advice and state.get("advice_step") in (state["step"], state["step"] - 1):
            which = "this milestone's previous review" if state.get("advice_step") == state["step"] else "the previous milestone"
            notes += f"{chr(10) * bool(notes)}Local reviewer note from {which}: {advice}"
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
            f"{notes + chr(10) + chr(10) if notes else ''}"
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
        summary = redact(answer or "").strip()[:1500]
        stalls = state.get("worker_stalls", 0)
        if code == 125 and stalls < 2:
            # A stalled worker is a transient, not a product failure: refund the attempt and start a fresh session.
            policy = self.spec.get("retry", {})
            delay = min(policy.get("max_seconds", 60), policy.get("initial_seconds", 2) * 2 ** stalls)
            self.set("worker_stalled", {"attempt": next_state["attempt"], "stall": stalls + 1, "stalls_allowed": 2, "exit": code,
                     "log": str(log), "seconds": delay}, phase="ready", attempts_used=used, worker_session=None,
                     worker_stalls=stalls + 1, log=state.get("log"),
                     retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
            return
        if code:
            label = {124: "time limit", 125: "idle timeout", 126: "output limit"}.get(code, str(code))
            self.reject(f"Worker exit {label}\n{answer or tail(log)}", "worker", exit_code=code, worker_session=None,
                        worker_stalls=0, worker_summary=summary)
            return
        self.set("worker_finished", {"exit": code, "session": session, "log": str(log)},
                 phase="checking", worker_session=session, transient_failures=0, worker_stalls=0, review_parse_failures=0,
                 worker_summary=summary)
        previous = self.unchanged_since_rejection(state, path)
        if previous:
            self.reject("The worker made no change after the previous rejection. The same build was already rejected, so the "
                        "checks and the reviewer were not run again.\nPrevious rejection:\n" + previous.get("issue", "")[:3000],
                        "no_progress")

    def unchanged_since_rejection(self, state, path):
        """The previous rejected build of this milestone if a reviewer turned it down and the worker left it untouched."""
        trees = state.get("rejected_trees") or []
        if state.get("rejected_step") != state["step"] or not trees or trees[-1].get("phase") not in ("review", "no_progress"):
            return None
        if trees[-1].get("feedback", 0) != self.store.get(self.id)["state"].get("feedback_version", 0):
            return None  # the operator has said something since (maybe during the call just made); the reviewer must be asked again
        try:
            stage(path, state["accepted"], self.spec["milestones"][state["step"]]["paths"])
            return trees[-1] if git(path, "write-tree") == trees[-1]["tree"] else None
        except (ValueError, OSError):
            return None

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
                    known = self.gate_limits.setdefault((snapshot, digest), [check["timeout"]])
                    known[:] = max(known, receipt.get("timeout_retried", {}).get("limits", known), key=len)  # a resumed verification starts at the limit its saved samples needed
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

    def run_timed_gate(self, check, path, folder, snapshot, location, fresh, base):
        """run_gate on `check` in `folder`; each time the controller ends it at its wall-clock limit (gate_timed_out) it is run again in `fresh()`,
        a new evidence folder, with twice the time (never above GATE_TIMEOUT_CAP), up to the plan's retry.gate_timeout_retries (0 stops at the
        first timeout, as 0.19 did). A gate that ran out of time has not failed: a repository that grew since the limit was set is the usual
        cause, so a timeout is never a rejection of the worker's change. A limit that had to be raised stays raised for the rest of that
        verification (the same gate on the same tree: its other stability samples, a confirmation round, an exploration's other samples), so the
        old limit is paid once, not once per sample, and the retries are those of the verification. -> (receipt, the folder it was made in).
        A receipt run above the plan's limit carries the plan's contract hash (saved samples are matched by it) and `timeout_retried`; one that
        timed out for good says which gate, what limits and that the change was not judged."""
        retries = self.spec.get("retry", {}).get("gate_timeout_retries", 1)
        limits = self.gate_limits.setdefault((snapshot, contract_hash(check)), [check["timeout"]])
        if len(self.gate_limits) > 64:
            del self.gate_limits[next(iter(self.gate_limits))]
        retried_here = False
        while True:
            receipt = run_gate({**check, "timeout": limits[-1]}, path, folder, snapshot, paused=self.paused,
                               **({"baseline": base} if check.get("kind") == "catalog" or "proof" in check else {}),
                               max_log_bytes=self.spec.get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024),
                               heartbeat=lambda info: self.set("process_heartbeat", {**info, "role": "gate"}))
            longer = min(limits[-1] * 2, GATE_TIMEOUT_CAP)
            if not gate_timed_out(receipt) or len(limits) > retries or longer <= limits[-1]:
                break
            next_folder = fresh()
            self.set("gate_timeout_retried", {**location, "name": check["name"], "old_timeout": limits[-1], "new_timeout": longer,
                     "retry": len(limits), "log": receipt["log"], "next_log": str(next_folder / "gate.log")}, log=str(next_folder / "gate.log"))
            limits.append(longer)
            folder, retried_here = next_folder, True
        if len(limits) > 1:
            receipt["contract_sha256"] = contract_hash(check)  # the plan's gate, not the one run with more time
            receipt["timeout_retried"] = {"limits": list(limits), **({} if retried_here else {"carried": True})}
            if gate_timed_out(receipt):
                again = (f"again at {limits[-1]} s with the limit doubled" if len(limits) == 2 else
                         f"on {len(limits) - 1} retries with the limit doubled each time, the last at {limits[-1]} s")
                on_baseline = " (its run on the proof baseline)" if receipt["artifact_error"] == BASELINE_TIMEOUT else ""
                receipt["artifact_error"] = (f"Gate {check['name']!r}{on_baseline} exceeded its wall-clock timeout of {limits[0]} s, and {again}. "
                                             f"The worker's change was not judged.")
                receipt["timeout_retried"]["timed_out"] = True
        return receipt, folder

    def observe_gate(self, check, path, state, step, snapshot, location, evidence=None):
        parent = evidence or self.evidence_dir(state)
        folder = evidence_folder(parent, location["gate_index"])
        log = folder / "gate.log"
        self.set("gate_started", {**location, "name": check["name"], "tree": snapshot,
                 "contract_sha256": contract_hash(check), "log": str(log)}, log=str(log))
        baseline = self.spec.get("proof_base", state["base"]) if "proof" in check else state["base"]
        receipt, folder = self.run_timed_gate(check, path, folder, snapshot, location, lambda: evidence_folder(parent, location["gate_index"]), baseline)
        log = folder / "gate.log"
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
            issue = receipt["artifact_error"]
            if (timed := receipt.get("timeout_retried")) and timed.get("timed_out"):
                issue += " " + timeout_advice(timed["limits"][0])
            self.set("blocked", {"during": "checking", "issue": issue}, phase="blocked", blocked_from="checking", last_error=issue)
        else:
            self.reject(f"{check['name']} exited {receipt['exit']} and could not collect its required evidence: "
                        f"{receipt.get('artifact_error')}\n{failure_excerpt(receipt['log'])}", "artifact")

    def verified_checks(self, state):
        evidence = self.evidence_dir(state)
        try:
            raw = (evidence / "checks.json").read_bytes()
            # The manifest of a direction or rescue strategy that won its tournament holds receipts from that candidate's evidence folder.
            digest = hashlib.sha256(raw).hexdigest()
            won = next((w for w in (state.get("exploration"), state.get("rescue")) if w and w.get("evidence") and w.get("checks_sha256") == digest
                        and w.get("step", 0) == state["step"]), None)
            root = Path(won["evidence"]) if won else evidence
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
                    verify_saved_sample(sample, root)
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
        note = None
        if self.spec.get("advisory_ollama"):
            if state.get("advice_tree") == snapshot and state.get("advice_step") == state["step"]:
                note = state.get("advice_note")  # asked about this build already: a retry, a fallback or a park wake-up does not ask again
            elif state.get("advice_failed_tree") != snapshot:
                # The local model speaks first so the reviewer can use what it noticed.
                note, stop = self.local_note(state, step, snapshot, evidence, diff, checks)
                if stop:
                    return
                self.check_budget()
        decision = (f"Goal: {self.spec['objective']}\n{self.acceptance_brief()}"
                    f"Budget remaining before review: {budget_status(self.spec, state)['remaining']}\nTask: {step['task']}\nChanges:\n{diff}\nChecks:\n{checks}\n"
                    f"{context.brief(self.store, self.id)}\nOperator updates\n{self.store.feedback_text(self.id) or 'None'}")
        if self.spec.get("reviewer"):
            from .repair import prior_findings
            from .verdicts import extract_verdict
            config = self.route(state, step, "reviewer")
            self.independence(state, "reviewer", config, self.route(state, step, "worker"))
            material, images = self.review_material(path, state, evidence)
            findings = prior_findings(self.store, self.id, state)
            prompt = (
                "Independently review these uncommitted, already tested changes. Read actual source and review "
                "the supplied check logs and artifacts, including screenshots when UI is part of the task. "
                "If required evidence cannot be read or has not been inspected, return REVISE and explain. Remain read-only. "
                'Return only JSON {"verdict":"PASS" or "REVISE","reason":"specific findings"}.\n\n' + decision + "\n\n" + material
            )
            if note:
                prompt += f"\n\nA local model suggested checking: {note}. Verify it if relevant."
            if findings:
                prompt += ("\n\nYour own earlier findings on this milestone\n" + "\n".join(findings)
                           + "\nVerify these earlier findings were addressed. Return REVISE only for defects that matter for the task, "
                           "the acceptance requirements or the goal; mention minor polish inside the reason of a PASS. "
                           "Do not raise new nitpicks about code that did not change.")
            if state["step"] + 1 == len(self.spec["milestones"]) and not self.spec.get("pursue"):
                prompt += ("\nThis is the final milestone. Also check the complete project objective against the current "
                           "worktree and supplied evidence. Return REVISE for any unfulfilled part of that objective.")
            failures = state.get("review_parse_failures", 0)
            while True:
                log = evidence / f"review-{self.store.get(self.id)['state'].get('provider_calls', 0) + 1:04d}.jsonl"
                self.set("review_started", {"adapter": config["kind"], "tree": snapshot}, log=str(log), retry_at=None)
                code, _, answer = self.call_provider(config, "reviewer", path, log, prompt, evidence=evidence, images=images)
                excerpt = redact(answer or tail(log)).strip()[:1200]
                if code:
                    label = {124: "time limit", 125: "idle timeout", 126: "output limit"}.get(code)
                    self.set("reviewer_failed", {"attempt": state["attempt"], "exit": code, "log": str(log), "excerpt": excerpt[:600]},
                             review_parse_failures=0)
                    self.reviewer_unavailable(f"the reviewer stopped at its {label}" if label else f"the reviewer exited with code {code}", excerpt)
                try:
                    review = extract_verdict(answer, ("PASS", "REVISE"))
                except ValueError:
                    review = None
                reason = review.get("reason") if review and isinstance(review.get("reason"), str) else ""
                if review and (review["verdict"] != "PASS" or reason.strip()):
                    break
                # The reviewer answered, but with no verdict we can read or a PASS that says nothing. That is not a rejection
                # of the work: ask once more, then treat the reviewer as unavailable instead of charging the worker.
                failures += 1
                self.set("review_unparseable", {"attempt": state["attempt"], "count": failures, "log": str(log), "excerpt": excerpt[:600],
                         "problem": "passed the build without a reason" if review else "gave no readable verdict"},
                         review_parse_failures=failures if failures < 2 else 0)
                if failures >= 2:
                    self.reviewer_unavailable("its answer could not be read after two tries", excerpt)
            if review["verdict"] != "PASS":
                text = str(reason.strip() or answer or tail(log))[:3600]
                self.reject("Independent review requested changes.\n" + text, "review", finding=text, review_parse_failures=0,
                            seen=state.get("feedback_version", 0))
                return
            self.set("review_passed", {"reason": reason, "tree": snapshot, "log": str(log)}, review_parse_failures=0)
        if self.snapshot(path, state, step) != snapshot:
            self.set("snapshot_changed", {"previous": snapshot}, phase="checking")
            return
        if self.store.get(self.id)["state"].get("feedback_version", 0) != state.get("feedback_version", 0):
            self.reject("Operator feedback changed during review. Apply the saved update.", "feedback", worker_session=None)
            return
        commit = commit_tree(path, snapshot, state["accepted"], step["title"])
        self.set("promotion_prepared", {"commit": commit, "tree": snapshot},
                 phase="promoting", candidate=commit)

    def reviewer_unavailable(self, problem, excerpt):
        """The reviewer failed, not the work. Nothing is charged to the worker: the run retries the review as a transient
        (the reviewer fallbacks first, then retry.transient_attempts) and, when those are spent, blocks naming the reviewer."""
        raise ProviderUnavailable(f"Reviewer unavailable: {problem}. The worker's attempt was not charged; resume with --retry once the reviewer works."
                                  + (f"\nLast output:\n{excerpt}" if excerpt else ""), "transient")

    def local_note(self, state, step, snapshot, evidence, diff, checks):
        """One sentence from the loopback model. Returns (note, stop); stop means a required local review failed."""
        self.check_budget(call=True)
        current = self.store.get(self.id)["state"]
        pending = {"call": current.get("provider_calls", 0) + 1, "role": "advisory", "provider": "ollama",
                   "model": self.spec["advisory_ollama"]["model"], "started": timestamp(),
                   "step": state["step"] + 1, "attempt": state["attempt"],
                   "log": str(evidence / f"advice-{current.get('provider_calls', 0) + 1:04d}.json")}
        if "think" in self.spec["advisory_ollama"]:
            pending["think"] = self.spec["advisory_ollama"]["think"]
        self.set("provider_call_started", pending, pending_call=pending, provider_calls=pending["call"])
        note = None
        try:
            config = dict(self.spec["advisory_ollama"])
            if self.spec.get("budgets", {}).get("max_seconds"):
                elapsed = run_clock(current, datetime.fromisoformat(timestamp()))[1]
                config["timeout"] = max(.1, min(config.get("timeout", 90), self.spec["budgets"]["max_seconds"] - elapsed))
            summary = ", ".join(f"{check['name']} exit {check['exit']}" for check in json.loads(checks))
            inference = {}
            note = advisory(config,
                            f"In one sentence, suggest any missing validation.\n"
                            f"Task: {step['task']}\nChanges: {diff}\nChecks: {summary}", receipt=inference)
            self.set("local_advice", {"note": note, "tree": snapshot, "inference": inference},
                     advice_note=redact(note)[:1500], advice_step=state["step"], advice_tree=snapshot)
            write_json(Path(pending["log"]), {"note": redact(note), "inference": inference})
        except (OSError, ValueError) as error:
            note = None
            required = self.spec["advisory_ollama"].get("required")
            # An optional advisory that failed is not asked again about this build; a required one is asked again on --retry.
            self.set("local_advice_unavailable", {"reason": str(error)}, **({} if required else {"advice_failed_tree": snapshot}))
            if required:
                self.set("local_advice_required", {"model": self.spec["advisory_ollama"]["model"]},
                         phase="blocked", blocked_from="reviewing", last_error=f"Required local review failed: {error}")
                return None, True
        finally:
            self.finish_call(pending)
        return note and redact(note)[:1500], False

    def review_material(self, path, state, evidence):
        """The real change as a saved patch with an inline excerpt, and the screenshots this attempt produced."""
        from .pursuit import image_block, screenshots
        raw = git_bytes(path, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", state["accepted"])
        cap = 400 * 1024
        body = raw[:cap]
        notice = f"\n[Diff truncated at 400 KB; {len(raw) - cap} bytes omitted.]\n".encode() if len(raw) > cap else b""
        patch = evidence / "review-diff.patch"
        atomic_write(patch, body + notice)
        excerpt = body[:16 * 1024].decode("utf-8", "replace")
        text = (f"Full change: the complete staged diff is saved at {patch} ({len(raw)} bytes"
                f"{'; truncated to 400 KB' if notice else ''}). Read that file for anything beyond the first 16 KB shown here.\n"
                + excerpt + ("\n[Excerpt truncated.]" if len(body) > 16 * 1024 else ""))
        images = screenshots(evidence)
        if images:
            text += ("\n\nScreenshots produced by this attempt's checks. OPEN each image file listed here with your file-reading tool "
                     "and judge what you actually see, not what any log claims:\n" + image_block(images))
        return text, images

    def independence(self, state, role, config, worker):
        """Warn once per role and pair when the checker is the same known vendor family as the worker. A command route has no
        known vendor, so it never warns."""
        from .repair import describe, family
        if family(config) is None or family(config) != family(worker):
            return
        pair = f"{role}:{json.dumps(describe(worker), sort_keys=True)}>{json.dumps(describe(config), sort_keys=True)}"
        warned = self.store.get(self.id)["state"].get("independence_warned", [])
        if pair not in warned:
            self.set("independence_degraded", {"role": role, "worker": {**describe(worker), "family": family(worker)},
                     "reviewer_or_judge": {**describe(config), "family": family(config)},
                     "issue": f"The {role} comes from the same vendor family as the worker, so its verdict is less independent."},
                     independence_warned=[*warned, pair])

    def snapshot(self, path, state, step):
        removed = []
        try:
            return tree(path, state["accepted"], step["paths"], self.spec.get("protected_paths", []), removed)
        finally:
            if removed:  # deleted from the worktree so that no check can pass on a file the accepted commit will not contain
                known = self.store.get(self.id)["state"].get("droppings") or {}
                kept = known["paths"] if (known.get("step"), known.get("attempt")) == (state["step"] + 1, state["attempt"]) else []
                paths = [*kept, *(name for name in removed if name not in kept)][:20]
                # Kept in the state too, so a rejection can tell the worker why a file its checks want is gone.
                self.set("tool_droppings_removed", {"step": state["step"] + 1, "attempt": state["attempt"], "count": len(removed),
                                                    "paths": removed[:20]},
                         droppings={"step": state["step"] + 1, "attempt": state["attempt"], "paths": paths})

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
        fallback = identity_fallback(self.repository, candidate)  # the safety net of commit_tree, said where the commit becomes the accepted one
        pursuing = finished and bool(self.spec.get("pursue"))
        wave = state.get("wave", 1)
        completion = (("wave_completed", {"wave": wave, "commit": candidate, "branch": self.ref}) if pursuing else
                      ("run_completed", {"branch": self.ref, "commit": candidate}) if finished else None)
        self.set("milestone_accepted", {"step": number, "commit": candidate, "branch": self.ref,
                 "tree": state.get("candidate_tree"), "verified_checks": passed, **({"identity_fallback": fallback} if fallback else {})},
                 following_event=completion, **({"wave": wave, "judge_attempts": 0, "judge_route": 0} if pursuing else {}),
                 phase="judging" if pursuing else "done" if finished else "ready", step=number, accepted=candidate,
                 attempt=0, attempts_used=0, candidate=None, candidate_tree=None,
                 worker_session=None, last_error="", log=None, retry_at=None, failure_count=0,
                 failure_fingerprint=None, worker_route=0, reviewer_route=0, transient_failures=0, route_rejections=0)

    def run(self, *, retry=False, defer_retries=False, only_operational=False):
        use_git_timeout(self.spec)  # before any git call: a fresh process, or a dispatcher that ran another plan, holds another run's value
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
            self.store.derive_clock(self.id)  # a run made by 0.19 keeps no active-time ledger: read it from its events before anything changes it
            if self.spec.get("pursue") and "final_checks" not in self.spec:
                from . import pursuit
                pursuit.settle_gates(self)  # a pursuit made by 0.19: its planned waves ask a wave's early milestones for the final-only checks
            if state["phase"] == "blocked" and retry:
                self.set("blocked_run_retried", {}, phase=state.get("blocked_from") or "ready",
                         attempts_used=0, blocked_from=None, provider_issue=None, retry_at=None, verification_batch=None,
                         transient_failures=0, failure_count=0, failure_fingerprint=None, route_rejections=0,
                         rejected_trees=[], review_parse_failures=0,  # a resumed run asks the reviewer again, even about an unchanged build
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
                use_git_timeout(self.spec)  # judging, planning and a wave plan reload the spec; every phase reads the plan's limit
                if phase in ("judging", "planning", "rescuing") and not self.spec.get("pursue"):
                    raise ValueError(f"Unrecognized run state: {phase}")
                step = None if phase in ("judging", "planning", "rescuing") else self.spec["milestones"][state["step"]]
                try:
                    self.check_budget()
                    if state.get("retry_at") and (wait := (datetime.fromisoformat(state["retry_at"]) - datetime.now(timezone.utc)).total_seconds()) > 0:
                        if defer_retries:
                            return "waiting"
                        time.sleep(min(2, max(.2, wait)))  # a parked run may wait hours; poll gently but still notice a pause
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
                    if phase == "rescuing":
                        from . import rescue
                        rescue.advance(self, state)
                        continue
                    if phase in ("queued", "ready", "exploring") and self.explores(state):
                        from . import explore
                        explore.advance(self, state)
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
                    if actual in ("exploring", "rescuing"):
                        from . import explore, rescue
                        role, config = (explore if actual == "exploring" else rescue).active_route(self, current)
                    elif actual == "judging" and self.spec.get("pursue"):
                        from . import pursuit
                        role, config = pursuit.active_route(self, current)  # the confirmation judgment has its own route
                    else:
                        config = self.route(current, step, role)
                    provider = {**describe_provider(config), "role": role, "reason": error.kind}
                    pursue = self.spec.get("pursue", {})
                    routes = {"worker": [] if actual in ("exploring", "rescuing") else (step or {}).get("worker_fallbacks", []),
                              "reviewer": self.spec.get("reviewer_fallbacks", []),
                              "judge": pursue.get("judge_fallbacks", []), "planner": []}[role]
                    index = current.get(role + "_route", 0)
                    next_phase = "ready" if actual == "working" else actual
                    refund = {"attempts_used": max(0, current.get("attempts_used", 1) - 1) , "worker_session": None} if role == "worker" else {}
                    if index < len(routes):
                        self.set("provider_fallback", {"from": provider, "to": describe_provider(routes[index])},
                                 phase=next_phase, **{role + "_route": index + 1}, **({"route_rejections": 0} if role == "worker" else {}),
                                 last_error=str(error), transient_failures=0, retry_at=None, **refund)
                        continue
                    if error.kind == "usage_limit":
                        plan = park_plan(self.spec, current, error, datetime.now(timezone.utc), config["kind"])
                        if plan["block"] is None:
                            self.set("provider_parked", {"issue": str(error), **{k: plan[k] for k in ("until", "reset", "seconds", "parsed", "park")},
                                     **provider}, phase=next_phase, parks=plan["park"], retry_at=plan["until"], **refund)
                            continue
                        self.set("provider_unavailable", {"issue": plan["block"], "provider_message": str(error), **provider}, phase="blocked",
                                 blocked_from="ready" if actual == "working" else actual, last_error=plan["block"],
                                 provider_issue=provider, parks=0)
                        return "blocked"
                    failures = current.get("transient_failures", 0)
                    policy = self.spec.get("retry", {})
                    if error.kind == "transient" and failures < policy.get("transient_attempts", 2):
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
