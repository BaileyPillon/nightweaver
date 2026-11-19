"""Model-free validation, isolated browser replay and runtime database checks."""

from contextlib import closing
import hashlib
from io import BytesIO
import os
import shutil
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile, TemporaryDirectory
from uuid import uuid4

from . import context
from .checks import contract_hash, evidence_folder, observed_pass, run_gate, verify_inputs
from .engine import common_dir, git, git_bytes, verify_registered_repository
from .readiness import diagnose
from .safety import write_json
from .spec import artifact


def preflight(store, spec):
    report = diagnose(store, spec)
    base = report["source"]["base"]
    repository = Path(spec["repository"])
    errors = []
    try:
        pack = context.capture(spec, base)
        report["context"] = [{"path": f["path"], "sha256": f["sha256"]} for f in pack["files"]]
    except (OSError, ValueError) as error:
        report["context"] = []
        errors.append(f"Committed context: {error}")
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        try:
            git(repository, "var", identity)
        except ValueError as error:
            errors.append(f"{identity}: {error}")
    executables = {}
    browsers, baselines, verified_pins = set(), set(), set()
    from .journeys import browser_cases
    browser_work = []
    for number,milestone in enumerate(spec["milestones"],1):
        for gate in milestone["gates"]:
            try:
                verify_inputs(gate, repository, base=base)
                verified_pins.update((pin["path"],pin["sha256"]) for pin in gate.get("pinned_inputs", []))
            except (OSError, ValueError) as error:
                if str(error) not in errors:
                    errors.append(str(error))
            if gate.get("kind") == "catalog":
                from .catalog import check_catalog
                result = check_catalog(repository, base, base, gate["catalog"])
                if result.get("input_error"):
                    errors.append(f"{gate['name']}: {result['input_error']}")
                continue
            if gate.get("kind") != "browser":
                program = gate["argv"][0]
                if program not in executables:
                    path = Path(program)
                    if not path.is_absolute() and (path.parent != Path(".") or "/" in program or "\\" in program):
                        available = "candidate_relative"
                    elif not path.is_absolute() and (repository / path).is_file():
                        available = "candidate_relative"
                    else:
                        available = "available" if shutil.which(program) else "missing"
                    executables[program] = available
                    if available == "missing":
                        errors.append(f"Missing gate executable: {program}")
                continue
            config = gate["browser"]
            browsers.add(config.get("channel", "bundled"))
            cases = list(browser_cases(config))
            samples = gate.get("stability_runs",1)
            browser_work.append({"milestone":number,"gate":gate["name"],"cases_per_sample":len(cases),
                                 "contexts":len(cases)*samples,"steps":sum(len(steps) for _,_,steps in cases)*samples,
                                 "samples":samples})
            for step in [*config["steps"],*(s for scenario in config.get("scenarios",[]) for s in scenario.get("steps",[]))]:
                if step["action"] == "visual_match":
                    contract = step["value"]
                    baselines.add((artifact(contract["baseline"]), contract["sha256"]))
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
    errors.extend(probe_browser_channels(browsers))
    from .contracts import check_schedule
    return {**report, "check_schedule": check_schedule(spec), "browser_work":browser_work, "browser_channels": sorted(browsers), "gate_executables": executables, "errors": errors, "model_calls": 0,
            "verified_pins": [{"path": p, "sha256": h} for p,h in sorted(verified_pins)],
            "ready": not errors and all(p["status"] != "blocked" for p in report["providers"])}


def probe_browser_channels(channels):
    """Open an in-memory test page once per requested browser, independently of the project."""
    errors = []
    for channel in sorted(set(channels)):
        try:
            from .browser import browser_runtime
            with browser_runtime(None if channel == "bundled" else channel, timeout=10000) as browser:
                page = browser.new_page()
                page.set_content("<title>Nightweaver preflight</title>")
                if page.title() != "Nightweaver preflight":
                    errors.append(f"Browser probe failed: {channel}")
        except Exception as error:
            errors.append(f"{channel}: {error}")
    return errors


def replay_browser(store, run_id, number=None, gate_name=None):
    return replay_checks(store, run_id, number, gate_name, browser_only=True)


def replay_checks(store, run_id, number=None, gate_name=None, repeat=None, *, browser_only=False, through=False):
    if repeat is not None and (type(repeat) is not int or not 1 <= repeat <= 20):
        raise ValueError("Replay repetitions must be between 1 and 20")
    row = store.get(run_id)
    accepted = store.accepted_milestones(run_id)
    number = number if number is not None else max((item["step"] for item in accepted), default=0)
    event = next((item for item in accepted if item["step"] == number), None)
    if not event:
        raise ValueError("Select a milestone accepted by this run")
    contracts = {}
    origins = {}
    for stage in range(1 if through else number, number + 1):
        for gate in row["spec"]["milestones"][stage - 1]["gates"]:
            if (browser_only and gate.get("kind") != "browser") or (gate_name is not None and gate_name != gate["name"]):
                continue
            digest = contract_hash(gate)
            contracts[digest] = gate
            origins.setdefault(digest, []).append(stage)
    gates = list(contracts.values())
    if not gates:
        raise ValueError("No matching gate for that milestone")
    repo = Path(row["spec"]["repository"])
    verify_registered_repository(row["spec"], common_dir(repo))
    evidence = store.directory(run_id) / "replays" / uuid4().hex
    evidence.mkdir(parents=True)
    report = {"run_id": run_id, "milestone": number, "commit": event["commit"], "evidence": str(evidence),
              "scope": "through" if through else "milestone", "gates": []}
    with TemporaryDirectory(prefix="nw-replay-") as temp:
        repetitions = [repeat or gate.get("stability_runs", 1) for gate in gates]
        schedule = ((gate, sample) for sample in range(1, max(repetitions) + 1)
                    for gate, count in zip(gates, repetitions) if sample <= count)
        for idx, (gate, sample) in enumerate(schedule, 1):
            # Each server may write into its working directory. Give every gate its own checkout.
            copy = Path(temp) / str(idx)
            git(repo, "clone", "--local", "--no-hardlinks", "--no-checkout", "--", str(repo), str(copy))
            git(copy, "checkout", "--detach", event["commit"])
            tree = git(copy, "rev-parse", "HEAD^{tree}")
            record = run_gate(gate, copy, evidence_folder(evidence, idx), tree,
                              **({"baseline": row["state"]["base"]} if gate.get("kind") == "catalog" else {}),
                              max_log_bytes=row["spec"].get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024))
            if Path(record["log"]).is_file():
                with Path(record["log"]).open("rb") as stream:
                    record["log_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            git(copy, "add", "-A", "--", ".")
            changed = git(copy, "write-tree") != tree or git(copy, "rev-parse", "HEAD") != event["commit"]
            report["gates"].append({**record, "gate": gate["name"], "repeat": sample,
                                    "scheduled_milestones": origins[contract_hash(gate)],
                                    "changed_source": changed, "passed": observed_pass(record) is True and not changed})
    report["stability"] = {}
    for gate in gates:
        digest = contract_hash(gate)
        observations = [g for g in report["gates"] if g["contract_sha256"] == digest]
        outcomes = {g["passed"] for g in observations}
        if any(g.get("artifact_error") or g["changed_source"] for g in observations):
            status = "inconclusive"
        elif len(outcomes) > 1:
            status = "mixed"
        else:
            status = "all_passed" if True in outcomes else "all_failed"
        key = f"M{origins[digest][0]} / {gate['name']}" if through else gate["name"]
        report["stability"][key] = status
    report["model_calls"] = 0
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
            if not problems:
                semantic = {
                    "invalid run state": """SELECT count(*) FROM runs WHERE json_type(state)!='object'
                        OR json_type(spec)!='object' OR coalesce(json_type(state,'$.step'),'')!='integer'
                        OR coalesce(json_extract(state,'$.phase'),'') NOT IN
                        ('queued','ready','working','checking','reviewing','promoting','blocked','done')""",
                    "invalid milestone cursor": """SELECT count(*) FROM runs WHERE
                        coalesce(json_type(spec,'$.milestones'),'') != 'array'
                        OR json_array_length(spec,'$.milestones') < 1
                        OR json_extract(state,'$.step') < 0
                        OR CASE WHEN json_extract(state,'$.phase')='done'
                            THEN json_extract(state,'$.step') != json_array_length(spec,'$.milestones')
                            ELSE json_extract(state,'$.step') >= json_array_length(spec,'$.milestones') END""",
                    "missing prerequisite": """SELECT count(*) FROM runs r, json_each(r.state,'$.after') d
                        WHERE NOT EXISTS (SELECT 1 FROM runs p WHERE p.id=d.value)""",
                    "cyclic prerequisite": """WITH RECURSIVE edges(child,parent) AS (
                        SELECT r.id,d.value FROM runs r,json_each(r.state,'$.after') d
                    ), ancestors(child,parent) AS (
                        SELECT child,parent FROM edges UNION
                        SELECT a.child,e.parent FROM ancestors a JOIN edges e ON e.child=a.parent
                    ) SELECT count(*) FROM ancestors WHERE child=parent""",
                }
                for label, query in semantic.items():
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
