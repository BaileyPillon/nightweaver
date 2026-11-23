"""Evaluate real AGTX merges inside a disposable WSL project. Reads the task on stdin."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from threading import Thread


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=45, **kwargs).stdout


def claude_host_env(environ, profile=None):
    """The Claude coordinator's environment. A command route that inherits credentials hands over every one it has, so the other
    vendors' keys are dropped here, and an explicit profile keeps its own login instead of an inherited CLAUDE_CODE_OAUTH_TOKEN."""
    env = {name: value for name, value in environ.items() if not (
        name.upper().startswith(("OPENAI_", "CODEX_", "GEMINI_", "GOOGLE_", "DEEPSEEK_"))
        and re.search(r"(?:KEY|TOKEN|SECRET)(?:_FILE)?$", name, re.I))}
    if profile:
        env["CLAUDE_CONFIG_DIR"] = str(profile.resolve(strict=True))
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def host_errors(path):
    failures = []
    with path.open("rb") as log:
        for line in log:
            try:
                event = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("is_error") or event.get("type") in ("error", "turn.failed"):
                failures.append(str(event.get("result") or event.get("message") or event.get("error"))[:1000])
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "mcp_tool_call" and item.get("error"):
                failures.append(str(item["error"])[:1000])
    return " ".join(failures).lower()


def merged_result(repo, db, base, output):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT id,status,agent FROM tasks ORDER BY id").fetchall()
    tip = command(["git", "rev-parse", "main"], cwd=repo, text=True).strip()
    command(["git", "merge-base", "--is-ancestor", base, tip], cwd=repo)
    complete = bool(rows) and all(status == "done" for _, status, _ in rows) and tip != base
    if complete:
        patch = command(["git", "diff", "--binary", "--no-ext-diff", base, tip, "--", ".",
                         ":(exclude).agtx", ":(exclude)oneshot-state.md"], cwd=repo)
        Path(output).write_bytes(patch)
    return {"baseline": base, "tip": tip, "tasks": rows, "board_complete": complete}


BROWSER_HANDOFF = (
    "When the plan declares Nightweaver browser gates, the Windows evaluator runs them after the board merge. "
    "Implement the complete visual brief, run available noninteractive checks and commit through the board. "
    "Delegate those browser gates to that evaluator. Record any checks you defer. Never approve an interactive permission "
    "request on the operator's behalf or claim an unexecuted check passed."
)


def needs_approval(pane):
    # Require a live choice menu plus its footer. A transcript mentioning approval is insufficient.
    tail = "\n".join(pane.splitlines()[-16:]).lower()
    return (bool(re.search(r"1\.\s+yes, proceed", tail)) and
            "tell codex what to do differently" in tail and "esc" in tail)


def blocked_pane(env):
    prefix = ["tmux", "-L", "agtx"]
    listing = subprocess.run([*prefix, "list-panes", "-a", "-F", "#{pane_id}"], env=env,
                             capture_output=True, text=True, timeout=4)
    for pane in listing.stdout.splitlines():
        if not re.fullmatch(r"%\d+", pane):
            continue
        capture = subprocess.run([*prefix, "capture-pane", "-p", "-t", pane], env=env,
                                 capture_output=True, text=True, errors="replace", timeout=4)
        if capture.returncode == 0 and needs_approval(capture.stdout):
            return pane
    return None


def save_drafts(repo, base, staging):
    """Snapshot each unmerged working tree, using a private index to include new files."""
    paths = command(["git", "worktree", "list", "--porcelain", "-z"], cwd=repo)
    drafts = []
    for field in paths.split(b"\0"):
        if not field.startswith(b"worktree "):
            continue
        path = Path(os.fsdecode(field[9:]))
        if path == repo or not path.is_dir():
            continue
        with tempfile.TemporaryDirectory(dir=staging) as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            command(["git", "read-tree", "HEAD"], cwd=path, env=env)
            paths_to_save = [".", ":(exclude).agtx", ":(exclude)oneshot-state.md"]
            command(["git", "add", "-A", "--", *paths_to_save], cwd=path, env=env)
            data = command(["git", "diff", "--cached", "--binary", "--no-ext-diff", base,
                            "--", *paths_to_save], cwd=path, env=env)
        if data:
            name = f"draft-{len(drafts) + 1}.patch"
            (staging / name).write_bytes(data)
            drafts.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return drafts


def linux_board(bundle, output, seconds):
    """The stdin pipe owns this board. EOF, deadline and normal return close its tmux servers."""
    root = Path(tempfile.mkdtemp(prefix="nw-agtx-", dir="/tmp"))
    repo = root / "project"
    config = root / "config"
    config.mkdir()
    local_bin = Path.home() / ".local/bin"
    env = {**os.environ, "PATH": f"{local_bin}:/usr/local/bin:/usr/bin:/bin",
           "AGTX_CONFIG_DIR": str(config), "AGTX_DATA_DIR": str(root / "db"),
           "TMUX_TMPDIR": str(root), "TERM": "xterm-256color"}
    env.pop("TMUX", None)
    agtx = shutil.which("agtx", path=env["PATH"])
    if not agtx:
        raise RuntimeError("Install agtx in the selected WSL distribution")
    (config / "config.toml").write_text('default_agent = "codex"\nauto_trust = true\nagent_hooks = true\nupdate_check = false\n')
    command(["git", "init", "-q", "-b", "main", str(repo)])
    command(["git", "fetch", "-q", str(bundle), "HEAD"], cwd=repo)
    command(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo)
    worker = shutil.which("codex", path=env["PATH"])
    if not worker:
        raise RuntimeError("Install and sign in to Codex in the selected WSL distribution")
    # AGTX and Codex both inspect the project trust store before launching task
    # worktrees. Keep that store per trial and use the installed subscription.
    agent_home = root / "agent-home"
    agent_config = agent_home / ".codex"
    agent_config.mkdir(parents=True, mode=0o700)
    agent_config.joinpath("config.toml").write_text(f'[projects."{repo}"]\ntrust_level = "trusted"\n')
    agent_config.joinpath("AGENTS.md").write_text(BROWSER_HANDOFF + "\n")
    env.update(CODEX_HOME=str(agent_config), AGTX_AGENT_HOME=str(agent_home))
    (root / "coordinator").mkdir()
    command(["git", "config", "user.name", "Nightweaver Benchmark"], cwd=repo)
    command(["git", "config", "user.email", "benchmark@example.invalid"], cwd=repo)
    base = command(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if command(["git", "ls-files", "--", ".agtx", "oneshot-state.md"], cwd=repo):
        raise RuntimeError("Benchmark baseline reserves .agtx and oneshot-state.md for the board")
    with (repo / ".git/info/exclude").open("a") as stream:
        stream.write("\n/.agtx/\n/oneshot-state.md\n")
    db = root / "db/projects" / (hashlib.sha256(str(repo).encode()).hexdigest()[:16] + ".db")
    auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    auth_link = agent_config / "auth.json"
    if not auth.is_file():
        raise RuntimeError("This WSL adapter needs a file-backed Codex CLI login")
    try:
        auth_link.symlink_to(auth)
        command(["tmux", "-L", "board", "new-session", "-d", "-x", "140", "-y", "36", "-c", str(repo), agtx], env=env)
        until = time.monotonic() + 15
        while not db.exists():
            if time.monotonic() >= until:
                raise RuntimeError("AGTX did not register its isolated project database")
            time.sleep(.1)
        print(json.dumps({"root": str(root), "project": str(repo), "baseline": base, "agtx": agtx,
                          "version": command([agtx, "--version"], text=True).strip(),
                          "worker_version": command([worker, "--version"], text=True).strip(), "worker": worker}), flush=True)
        deadline = time.monotonic() + seconds + 45
        previous = None
        while time.monotonic() < deadline:
            if select.select([sys.stdin], [], [], 4)[0]:
                request = sys.stdin.readline().strip()
                if request not in {"export", "drafts"}:
                    return
                receipt = merged_result(repo, db, base, output)
                if request == "drafts" or not receipt["board_complete"]:
                    receipt["drafts"] = save_drafts(repo, base, Path(output).parent)
                print(json.dumps(receipt), flush=True)
                return
            current = blocked_pane(env)
            if current and previous == current:
                print(json.dumps({"event": "approval_required", "pane": current}), flush=True)
                previous = "reported:" + current
            elif previous != "reported:" + str(current):
                previous = current
    finally:
        auth_link.unlink(missing_ok=True)
        for socket_name in ("agtx", "board"):
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], env=env, capture_output=True, timeout=10)


def evaluate(args):
    wsl = ["wsl.exe", "-d", args.distro, "--exec"]
    evidence = Path(tempfile.mkdtemp(prefix="nightweaver-agtx-"))
    print("AGTX evidence:", evidence, flush=True)
    bundle = evidence / "source.bundle"
    patch = evidence / "merged.patch"
    command(["git", "bundle", "create", str(bundle), "HEAD"])
    base = command(["git", "rev-parse", "HEAD"], text=True).strip()
    instructions = args.skill.resolve(strict=True).read_text(encoding="utf-8")
    task = sys.stdin.read()
    # Windows evaluates the final diff. Workers can run equivalent checks using python3.
    task = task.replace(sys.executable.replace("\\", "\\\\"), "python3")
    staging = command([*wsl, "mktemp", "-d", "/tmp/nw-transfer-XXXXXXXX"], text=True).strip()
    # Use stdio so WSL does not need Windows interop, drive mounts or a UNC share.
    writer = "import shutil, sys; f=open(sys.argv[1],'xb'); shutil.copyfileobj(sys.stdin.buffer,f); f.close()"
    for src, name in ((Path(__file__), "bridge.py"), (bundle, "source.bundle")):
        with src.open("rb") as source:
            command([*wsl, "python3", "-c", writer, staging + "/" + name], stdin=source)
    prefix = [*wsl, "python3", staging + "/bridge.py", "--linux-board", staging + "/source.bundle",
              staging + "/merged.patch", str(args.seconds)]
    with (evidence / "board-errors.log").open("wb") as errors, subprocess.Popen(
            prefix, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8") as board:
        try:
            line = board.stdout.readline()
            if not line:
                raise RuntimeError("AGTX board setup failed. Inspect board-errors.log in the evidence folder.")
            ready = json.loads(line)
            if ready["baseline"] != base:
                raise RuntimeError("AGTX checkout differs from the evaluator baseline")
            ready["oneshot_skill_sha256"] = hashlib.sha256(args.skill.read_bytes()).hexdigest()
            ready["coordinator"] = args.host
            ready["coordinator_model"] = args.model or "CLI default"
            ready["coordinator_version"] = (ready["worker_version"] if args.host == "codex" else
                                            command(["claude", "--version"], text=True).strip())
            (evidence / "setup.json").write_text(json.dumps(ready, indent=2))
            root = ready["root"]
            server = [*wsl[1:], "env", f"AGTX_CONFIG_DIR={root}/config", f"AGTX_DATA_DIR={root}/db",
                      f"TMUX_TMPDIR={root}", ready["agtx"], "mcp-serve"]
            mcp = {"mcpServers": {"agtx": {"command": wsl[0], "args": server}}}
            (evidence / "mcp.json").write_text(json.dumps(mcp))
            prompt = ("Run the installed AGTX OneShot procedure below on the sole isolated board project "
                      + ready["project"] + ". The workers own implementation. Worktree workers must commit and "
                      "you must merge through the board. The evaluator's do-not-commit instruction applies only to "
                      "the outer Windows checkout. Keep oneshot-state.md in your private coordinator working directory. "
                      "This small benchmark needs only enough board tasks to meet the brief. On completion return.\n\n"
                      + instructions + "\n\nInclude this policy in each work and review task description. " + BROWSER_HANDOFF
                      + "\n\nBenchmark goal and independent checks:\n" + task)
            env = os.environ.copy()
            if args.host == "claude":
                env = claude_host_env(env, args.profile)
                argv = ["claude", "--print", "--strict-mcp-config", "--mcp-config", str(evidence / "mcp.json"),
                        "--permission-mode", "acceptEdits", "--tools", "Read,Write,Edit", "--allowedTools",
                        "mcp__agtx__*", "Read", "Write", "Edit", "--output-format", "stream-json", "--verbose"]
            else:
                argv = [*wsl, "timeout", "--kill-after=5", str(args.seconds), "env", f"AGTX_CONFIG_DIR={root}/config", f"AGTX_DATA_DIR={root}/db",
                        f"TMUX_TMPDIR={root}", ready["worker"], "exec", "--ignore-user-config", "--cd", root + "/coordinator",
                        "--skip-git-repo-check", "--sandbox", "workspace-write", "--json",
                        "-c", 'approval_policy="never"', "-c", "mcp_servers.agtx.command=" + json.dumps(ready["agtx"]),
                        "-c", 'mcp_servers.agtx.args=["mcp-serve"]',
                        "-c", 'mcp_servers.agtx.env_vars=["AGTX_DATA_DIR","AGTX_CONFIG_DIR","TMUX_TMPDIR"]',
                        "-c", "mcp_servers.agtx.tool_timeout_sec=180",
                        "-c", 'mcp_servers.agtx.default_tools_approval_mode="approve"',
                        "-c", "mcp_servers.agtx.enabled_tools=" + json.dumps([
                            "list_projects", "get_config", "list_tasks", "get_task", "create_tasks_batch",
                            "create_task", "move_task", "get_transition_status", "wait_for_board_change",
                            "read_pane_content", "send_to_task", "move_to_done_and_merge"])]
            if args.model:
                argv += ["--model", args.model]
            if args.host == "codex":
                argv.append("-")
            messages = queue.Queue()

            def read_board():
                try:
                    for message in board.stdout:
                        messages.put(json.loads(message))
                finally:
                    messages.put(None)

            Thread(target=read_board, daemon=True).start()
            deadline = time.monotonic() + args.seconds
            interruption = None
            with tempfile.TemporaryFile() as input_file, (evidence / "coordinator.jsonl").open("wb") as stream:
                input_file.write(prompt.encode("utf-8"))
                input_file.seek(0)
                with subprocess.Popen(argv, cwd=evidence, stdin=input_file, stdout=stream,
                                      stderr=subprocess.STDOUT, env=env) as result:
                    try:
                        while result.poll() is None:
                            if time.monotonic() >= deadline:
                                interruption = "deadline"
                                break
                            try:
                                signal = messages.get(timeout=1)
                            except queue.Empty:
                                continue
                            if signal is None:
                                raise RuntimeError("AGTX board closed early")
                            if signal.get("event") == "approval_required":
                                interruption = "approval_required"
                                break
                    finally:
                        if result.poll() is None:
                            result.kill()
                            result.wait(timeout=10)
            if result.returncode == 124 and not interruption:
                interruption = "deadline"
            board.stdin.write("drafts\n" if interruption else "export\n")
            board.stdin.flush()
            while True:
                try:
                    receipt = messages.get(timeout=45)
                except queue.Empty as error:
                    raise RuntimeError("AGTX board receipt timed out") from error
                if receipt is None:
                    raise RuntimeError("AGTX board closed without a receipt")
                if "event" not in receipt:
                    break
                interruption = interruption or receipt["event"]
            receipt["interruption"] = interruption
            for draft in receipt.get("drafts", []):
                name = draft["file"]
                if not re.fullmatch(r"draft-[1-9]\d*\.patch", name):
                    raise ValueError("Invalid draft filename")
                data = command([*wsl, "cat", staging + "/" + name])
                if hashlib.sha256(data).hexdigest() != draft["sha256"]:
                    raise ValueError("Draft transfer integrity failure")
                (evidence / name).write_bytes(data)
            (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2))
            print(json.dumps(receipt), flush=True)
            if interruption == "approval_required":
                raise RuntimeError("AGTX worker requires operator approval (see private receipt)")
            if interruption == "deadline":
                return 124
            details = host_errors(evidence / "coordinator.jsonl")
            if any(term in details for term in ("oauth", "not logged in", "authenticate", "subscription", "rate limit", "usage limit")):
                raise RuntimeError(f"{args.host} coordinator account or service denied the request (see private log)")
            if result.returncode:
                raise RuntimeError(f"{args.host} coordinator exited {result.returncode} (see private log)")
            if not receipt["board_complete"]:
                if "requires approval" in details or "transport closed" in details:
                    raise RuntimeError("AGTX tools unavailable to the coordinator (see private log)")
                return 1
            with patch.open("wb") as target:
                subprocess.run([*wsl, "cat", staging + "/merged.patch"], stdout=target, check=True, timeout=45)
            if not patch.stat().st_size:
                return 1
            command(["git", "apply", "--check", str(patch)])
            command(["git", "apply", str(patch)])
            return 0
        finally:
            board.stdin.close()
            board.wait(timeout=20)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--linux-board"]:
        linux_board(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--distro", required=True)
        parser.add_argument("--skill", required=True, type=Path)
        parser.add_argument("--host", choices=("claude", "codex"), default="claude")
        parser.add_argument("--model")
        parser.add_argument("--profile", type=Path)
        parser.add_argument("--seconds", type=int, default=600)
        try:
            sys.exit(evaluate(parser.parse_args()))
        except subprocess.TimeoutExpired:
            print("AGTX workflow exceeded its deadline", flush=True)
            sys.exit(124)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"AGTX benchmark environment: {error}", flush=True)
            sys.exit(78)
