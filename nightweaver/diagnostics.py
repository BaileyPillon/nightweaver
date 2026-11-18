"""Model-free validation, isolated browser replay and runtime database checks."""

from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from uuid import uuid4

from . import context
from .engine import common_dir, git, git_bytes
from .process import execute
from .readiness import diagnose
from .safety import write_json
from .spec import artifact


def preflight(store, spec):
    report = diagnose(store, spec)
    base = report["source"]["base"]
    repository = Path(spec["repository"])
    pack = context.capture(spec, base)
    report["context"] = [{"path": f["path"], "sha256": f["sha256"]} for f in pack["files"]]
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        git(repository, "var", identity)
    browsers, baselines = set(), set()
    for milestone in spec["milestones"]:
        for gate in milestone["gates"]:
            if gate.get("kind") != "browser":
                continue
            config = gate["browser"]
            browsers.add(config.get("channel", "bundled"))
            for step in config["steps"]:
                if step["action"] == "visual_match":
                    contract = step["value"]
                    baselines.add((artifact(contract["baseline"]), contract["sha256"]))
    errors = []
    image_reader = None
    if baselines:
        try:
            from PIL import Image as image_reader
        except ImportError:
            errors.append("Install the browser extra to compare PNG baselines")
    for name, digest in sorted(baselines):
        try:
            entry = git_bytes(repository, "--literal-pathspecs", "ls-tree", "-z", base, "--", name)
            if not entry:
                errors.append(f"Missing baseline: {name}")
                continue
            mode, kind, oid = entry.partition(b"\t")[0].decode("ascii").split()
            if kind != "blob" or mode not in ("100644", "100755"):
                errors.append(f"Nonregular baseline: {name}")
                continue
            if int(git(repository, "cat-file", "-s", oid)) > 32 * 1024 * 1024:
                errors.append(f"Oversized baseline: {name}")
                continue
            data = git_bytes(repository, "cat-file", "blob", oid)
            if hashlib.sha256(data).hexdigest() != digest:
                errors.append(f"Baseline digest mismatch: {name}")
            elif image_reader is not None:
                try:
                    with image_reader.open(BytesIO(data), formats=("PNG",)) as reference:
                        if reference.width * reference.height > 16_000_000:
                            raise ValueError("reference exceeds 16 megapixels")
                        reference.verify()
                except (OSError, SyntaxError, ValueError, image_reader.DecompressionBombError) as error:
                    errors.append(f"Invalid PNG baseline {name}: {error}")
        except ValueError as error:
            errors.append(f"Unreadable baseline {name}: {error}")
    for channel in sorted(browsers):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as driver:
                browser = driver.chromium.launch(channel=None if channel == "bundled" else channel, timeout=10000)
                try:
                    page = browser.new_page()
                    page.set_content("<title>Nightweaver preflight</title>")
                    if page.title() != "Nightweaver preflight":
                        errors.append(f"Browser probe failed: {channel}")
                finally:
                    browser.close()
        except Exception as error:
            errors.append(f"{channel}: {error}")
    return {**report, "browser_channels": sorted(browsers), "errors": errors, "model_calls": 0,
            "ready": not errors and all(p["status"] != "blocked" for p in report["providers"])}


def replay_browser(store, run_id, number=None, gate_name=None):
    row = store.get(run_id)
    accepted = store.accepted_milestones(run_id)
    number = number if number is not None else max((item["step"] for item in accepted), default=0)
    event = next((item for item in accepted if item["step"] == number), None)
    if not event:
        raise ValueError("Select a milestone accepted by this run")
    gates = [g for g in row["spec"]["milestones"][number - 1]["gates"]
             if g.get("kind") == "browser" and (gate_name is None or gate_name == g["name"])]
    if not gates:
        raise ValueError("No matching browser gate for that milestone")
    repo = Path(row["spec"]["repository"])
    if common_dir(repo) != Path(row["spec"]["git_common_dir"]):
        raise ValueError("Registered repository identity changed")
    evidence = store.directory(run_id) / "replays" / uuid4().hex
    evidence.mkdir(parents=True)
    report = {"run_id": run_id, "milestone": number, "commit": event["commit"], "evidence": str(evidence), "gates": []}
    with TemporaryDirectory(prefix="nw-replay-") as temp:
        for idx, gate in enumerate(gates, 1):
            # Each server may write into its working directory. Give every gate its own checkout.
            copy = Path(temp) / str(idx)
            git(repo, "clone", "--local", "--no-hardlinks", "--no-checkout", "--", str(repo), str(copy))
            git(copy, "checkout", "--detach", event["commit"])
            config_path = evidence / f"{idx}.config.json"
            write_json(config_path, gate["browser"])
            out = evidence / str(idx)
            code = execute([sys.executable, str(Path(__file__).with_name("browser.py")), "--config-file",
                            str(config_path), "--output", str(out)], copy, evidence / f"{idx}.log", gate["timeout"])
            receipt = out / "browser.json"
            passed = code == 0 and receipt.is_file() and json.loads(receipt.read_text("utf-8")).get("passed") is True
            report["gates"].append({"gate": gate["name"], "passed": passed, "exit": code, "receipt": str(receipt)})
    report["passed"] = all(g["passed"] for g in report["gates"])
    write_json(evidence / "replay.json", report)
    return report


def _database_check(file):
    with closing(sqlite3.connect(file.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        problems = [msg for msg, in db.execute("PRAGMA integrity_check") if msg != "ok"]
        if not problems:
            checks = {
                "orphan events": "SELECT count(*) FROM events e LEFT JOIN runs r ON r.id=e.run_id WHERE r.id IS NULL",
                "malformed run JSON": "SELECT count(*) FROM runs WHERE NOT json_valid(spec) OR NOT json_valid(state)",
                "malformed event JSON": "SELECT count(*) FROM events WHERE NOT json_valid(details)",
            }
            for label, query in checks.items():
                count = db.execute(query).fetchone()[0]
                if count:
                    problems.append(f"{label}: {count}")
        return {"file": str(file), "ok": not problems, "problems": problems}


def database_check(file):
    try:
        return _database_check(file)
    except sqlite3.DatabaseError as error:
        return {"file": str(file), "ok": False, "problems": [str(error)]}


def database_backup(source, output):
    output = Path(output).absolute()
    source = Path(source).resolve(strict=True)
    if output.exists() or str(output.resolve()).casefold() in {
        str(source).casefold() + suffix for suffix in ("", "-wal", "-shm", "-journal")
    }:
        raise ValueError("Choose a new backup filename outside the live database files")
    with NamedTemporaryFile(dir=output.parent, prefix=".nw-backup-", delete=False) as stream:
        temp = Path(stream.name)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(temp)) as dest:
            src.backup(dest)
        result = database_check(temp)
        if not result["ok"]:
            raise ValueError(f"Backup failed integrity checks: {result['problems']}")
        # On Windows rename atomically fails if another process already claimed the destination.
        if os.name == "nt":
            os.rename(temp, output)
        else:
            os.link(temp, output)
        return {**result, "file": str(output), "scope": "runtime database", "bytes": output.stat().st_size}
    finally:
        temp.unlink(missing_ok=True)
