"""Design-direction tournament: build divergent candidates for milestone 1, compare them and promote the winner.

State machine, kept in a compact `exploration` dict so every stage resumes after a crash without repeating finished work:
  directions  one planner call names N divergent design directions (two invalid answers skip the tournament)
  build       each direction is built once in its own worktree, checked like a normal milestone, and committed on pass
  tournament  passed directions meet pairwise (both orders) or in one ranked call; the winner goes through Run.promote
The build, check, compare and promote helpers take a `names` mapping so the plateau rescue (rescue.py) runs the same machinery.
"""

from datetime import datetime, timezone
import hashlib
from itertools import combinations
from pathlib import Path
import re
import shutil
import subprocess

from .checks import observed_pass
from .engine import common_dir, git, tail, use_git_timeout, verify_registered_repository
from .insights import failure_excerpt
from .process import PausedRun
from .pursuit import screenshots
from .repair import describe
from .safety import atomic_write, redact, write_json
from .spec import text
from .store import timestamp
from .verdicts import extract_object, normalize

RANK_LIMIT, JUDGE_IMAGES = 12, 6
EVENTS = ["direction_planned", "direction_built", "direction_failed", "direction_compared", "direction_chosen",
          "exploration_skipped", "exploration_failed"]
# What a contest of builds calls its state key, events and worktrees; the rescue contest brings its own set.
EXPLORE = {"key": "exploration", "started": "direction_started", "worked": "direction_worker_finished",
           "passed": "direction_built", "failed": "direction_failed", "compared": "direction_compared", "invalid": "comparison_invalid",
           "then": "tournament"}


def options(run):
    return run.spec["pursue"]["explore"]


def folder(run, index=None):
    path = run.root / "evidence" / "explore"
    return path if index is None else path / f"{index:02d}"


def worker_for(run, index):
    workers = options(run)["workers"]
    return workers[(index - 1) % len(workers)]


def direction_ref(state, index):
    return f"{state['result_ref']}-direction-{index:02d}"


def owner_choice(run, ranking):
    """The direction number the owner picks from the two best, or None to leave the choice to the judge.

    A later change reads the phone inbox here. ranking lists {"index", "name", "score", "failed_checks"} best first;
    index is the direction's number (1 to N), the same one in the evidence folder and ref names."""
    return None


def active_route(run, state):
    """(role, provider config) of the exploration stage in progress, for fallbacks, parking and blocked states."""
    ex = state["exploration"]
    if ex["stage"] == "directions":
        return "planner", run.route(state, None, "planner")
    if ex["stage"] == "build":
        return "worker", worker_for(run, len(ex["builds"]) + 1)
    return "judge", run.route(state, None, "judge")


def advance(run, state):
    """Do one unit of exploration work; the run loop calls this again until the phase leaves exploring."""
    ex = state.get("exploration")
    if not ex:
        explore = options(run)
        run.set("exploration_started", {"directions": explore["directions"], "workers": [describe(w) for w in explore["workers"]],
                "comparisons": explore["comparisons"], **({"brief": explore["brief"]} if "brief" in explore else {})},
                phase="exploring", retry_at=None, last_error="",
                exploration={"stage": "directions", "attempts": 0, "builds": [], "comparisons": []})
    elif state["phase"] != "exploring":
        run.set("exploration_resumed", {"stage": ex["stage"]}, phase="exploring")
    elif ex["stage"] == "directions":
        plan_directions(run, state, ex)
    elif ex["stage"] == "build":
        build_direction(run, state, ex)
    else:
        tournament(run, state, ex)


# ---------------------------------------------------------------- worktrees

def drop(run, path, role="explore"):
    if not path.exists():
        return
    try:
        git(run.repository, "worktree", "remove", "--force", str(path))
        return
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        issue = str(error)
    shutil.rmtree(path, ignore_errors=True)
    try:
        git(run.repository, "worktree", "prune")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    if path.exists():
        run.set(f"{role}_worktree_cleanup_failed", {"path": str(path), "issue": issue[:300]})


def cleanup(run, role="explore"):
    """Remove every worktree of a contest. The refs keep the commits."""
    root = run.root / "worktrees"
    for path in sorted(root.glob(f"{role}-*")) if root.is_dir() else ():
        drop(run, path, role)


def checkout(run, base, name, *, keep=False, role="explore"):
    """A detached checkout of a base commit. A clean one is reused; a half-edited one is rebuilt unless kept."""
    use_git_timeout(run.spec)
    path = run.root / "worktrees" / name
    if path.exists():
        try:
            if ((path / ".git").is_file() and common_dir(path) == run.gitdir and git(path, "rev-parse", "HEAD") == base
                    and (keep or not git(path, "status", "--porcelain"))):
                return path
        except (ValueError, subprocess.TimeoutExpired):
            pass
        drop(run, path, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(run.repository, "worktree", "add", "--detach", str(path), base)
    except subprocess.TimeoutExpired as error:
        shutil.rmtree(path, ignore_errors=True)
        try:
            git(run.repository, "worktree", "prune")
        except (ValueError, subprocess.TimeoutExpired):
            pass
        raise ValueError(f"Creating {'an exploration' if role == 'explore' else 'a rescue'} worktree took more than {int(error.timeout)} seconds "
                         "on this repository. Raise git_timeout in the plan, then resume with --retry.") from error
    run.set("worktree_ready", {"path": str(path), "base": base, "role": role})
    return path


def worktree(run, state, index, *, keep=False):
    """The checkout of the base for one direction."""
    return checkout(run, state["accepted"], f"explore-{index:02d}", keep=keep)


# ---------------------------------------------------------------- stage 1: directions

def criteria_lines(run):
    pursue = run.spec["pursue"]
    criteria = pursue["criteria"] or run.spec.get("acceptance", [])
    return "\n".join(f"- {c}" for c in criteria) if criteria else "- None declared; judge against the goal."


def directions_prompt(run, state, step, count, ex):
    explore = options(run)
    return "\n\n".join([
        *([f"Your previous answer was rejected. {state['last_error']}"] if ex["attempts"] and state.get("last_error") else []),
        f"You are the art director of an autonomous coding run. {count} different builders will each build the first milestone of "
        "this goal in a different design direction, and an independent judge will compare the results. Read the repository as needed. "
        "Remain read-only; do not edit anything.",
        f"Goal\n{run.spec['objective']}",
        "Criteria that define done\n" + criteria_lines(run),
        f"Milestone 1: {step['title']}\n{step['task']}",
        *([f"Guidance on how the directions should differ\n{explore['brief']}"] if "brief" in explore else []),
        f"Propose exactly {count} genuinely divergent art directions for this milestone. Each must be a coherent, bold point of view "
        "that differs from the others in palette, typography, composition and motion language; none may be a generic or default look. "
        "Give each a short, distinctive name and a brief of three to six sentences concrete enough to build without further questions: "
        "palette with hex values, type, layout and composition, motion, and mood. "
        f'Return only JSON {{"directions":[{{"name":"...","brief":"..."}}]}} with exactly {count} entries.'])


def parse_directions(answer, count):
    data = extract_object(answer, "directions")
    items = data["directions"]
    if not isinstance(items, list) or len(items) != count:
        raise ValueError(f"The answer must list exactly {count} directions")
    found = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each direction must be an object with a name and a brief")
        found.append({"name": text(item, "name")[:80], "brief": text(item, "brief")[:3000]})
    if len({d["name"].casefold() for d in found}) != count:
        raise ValueError("direction names must be distinct")
    return found


def plan_directions(run, state, ex):
    count, step = options(run)["directions"], run.spec["milestones"][0]
    path = worktree(run, state, 1)  # the read-only planner reads the base here; direction 1 is then built in the same clean checkout
    log = folder(run) / f"directions-{state.get('provider_calls', 0) + 1:04d}.jsonl"
    config = run.route(state, None, "planner")
    run.set("directions_started", {"adapter": config["kind"], "attempt": ex["attempts"] + 1}, log=str(log), retry_at=None)
    code, _, answer = run.call_provider(config, "planner", path, log, directions_prompt(run, state, step, count, ex),
                                        evidence=folder(run), mode="directions")
    try:
        if code:
            raise ValueError(f"The planner exited {code}: {(answer or tail(log))[:300]}")
        directions = parse_directions(answer, count)
    except ValueError as error:
        attempts = ex["attempts"] + 1
        issue = f"The planner returned no valid directions (attempt {attempts} of 2): {str(error)[:600]}"
        if attempts < 2:
            run.set("directions_invalid", {"attempt": attempts, "issue": issue, "exit": code, "log": str(log)}, last_error=issue,
                    exploration={**ex, "attempts": attempts})
            return
        cleanup(run)
        run.set("exploration_skipped", {"reason": issue, "stage": "directions", "log": str(log)}, phase="ready", explored=True,
                exploration={"stage": "skipped"}, last_error="", retry_at=None, planner_route=0, transient_failures=0)
        return
    events = [("direction_planned", {"index": i, "count": count, "name": d["name"], "brief": d["brief"], "planner": describe(config)})
              for i, d in enumerate(directions[1:], 2)]
    run.set("direction_planned", {"index": 1, "count": count, "name": directions[0]["name"], "brief": directions[0]["brief"],
            "planner": describe(config)}, following_event=events, exploration={**ex, "stage": "build", "directions": directions},
            last_error="", retry_at=None, planner_route=0, transient_failures=0)


# ---------------------------------------------------------------- stage 2: build and check each direction

def gate(run, state, step, path, snapshot, out, index, number, check):
    """Run one check on a direction like the normal verify path (stability rounds included) -> (receipts, problem or None)."""
    return run_check(run, state, step, path, snapshot, out, number, check, {"step": 1, "attempt": 0, "explore": index})


def run_check(run, state, step, path, snapshot, out, number, check, where):
    count, receipts = check.get("stability_runs", 1), []
    for round_index in range(2 if count > 1 else 1):
        batch = []
        for sample in range(round_index * count + 1, (round_index + 1) * count + 1):
            if run.paused():
                raise PausedRun()
            location = {**where, "gate_index": number, **({"sample": sample, "stability_runs": count} if count > 1 else {})}
            batch.append(run.observe_gate(check, path, state, step, snapshot, location, evidence=out))
            if observed_pass(batch[-1]) is None:
                last = batch[-1]
                return [*receipts, *batch], f"{check['name']} could not be scored: {last.get('source_error') or last.get('artifact_error') or 'no result'}"
        outcomes = [observed_pass(r) for r in batch]
        if all(outcomes):
            if round_index:
                batch[0] = {**batch[0], "prior_mixed_round": receipts[:count]}
            return batch, None
        if not any(outcomes):
            last = batch[-1]
            return [*receipts, *batch], f"{check['name']} exited {last['exit']}\n{failure_excerpt(last['log'])}"
        receipts.extend(batch)
    return receipts, f"{check['name']} produced both passes and failures in two rounds."


def build_direction(run, state, ex):
    index, step = len(ex["builds"]) + 1, run.spec["milestones"][0]
    direction, config, out = ex["directions"][index - 1], worker_for(run, index), folder(run, index)
    current = ex.get("current") if (ex.get("current") or {}).get("index") == index else None
    path = worktree(run, state, index, keep=current is not None)
    detail = {"index": index, "count": len(ex["directions"]), "name": direction["name"], "brief": direction["brief"], "worker": describe(config),
              "evidence": str(out), "ref": None, "commit": None, "tree": None, "screenshots": [], "failed_checks": [], "checks": []}
    prompt = (run.worker_prompt(state, step) + f"\n\nDesign direction {direction['name']}: {direction['brief']}. "
              "Make this direction unmistakable; do not hedge toward a generic look.")
    build_one(run, state, ex, current, config=config, path=path, out=out, step=step, detail=detail, prompt=prompt,
              ref=direction_ref(state, index), gate_fn=gate, names=EXPLORE)


def build_one(run, state, ex, current, *, config, path, out, step, detail, prompt, ref, gate_fn, names):
    """One attempt at a candidate: the worker builds, every check of `step` runs, a pass is committed on the base and saved as `ref`.

    `current` is the interrupted attempt this call resumes, if any; `detail` holds the candidate's identity (index, count, name)."""
    index = detail["index"]
    if current is None:
        shutil.rmtree(out, ignore_errors=True)  # whatever an interrupted attempt left behind
        log = out / "worker.jsonl"
        feedback = run.store.get(run.id)["state"].get("feedback_version", 0)
        current = {"index": index, "started": timestamp(), "feedback": feedback, "summary": ""}
        run.set(names["started"], {"index": index, "count": detail["count"], "name": detail["name"], "worker": describe(config),
                "adapter": config["kind"]}, log=str(log), retry_at=None)
        code, _, answer = run.call_provider(config, "worker", path, log, prompt)
        current["summary"] = redact(answer or "").strip()[:1500]
        if code:
            label = {124: "time limit", 125: "idle timeout", 126: "output limit"}.get(code, str(code))
            return record(run, ex, {**detail, "passed": False, "reason": redact(f"Worker exit {label}\n{answer or tail(log)}")[:1500]}, current, names)
        ex = {**ex, "current": current}
        run.set(names["worked"], {"index": index, "log": str(log)}, **{names["key"]: ex})
    try:
        snapshot = run.snapshot(path, state, step)
    except ValueError as error:
        return record(run, ex, {**detail, "passed": False, "reason": redact(str(error))[:1500]}, current, names)
    for item in out.iterdir() if out.is_dir() else ():  # a verification interrupted earlier must not leave stale evidence
        if not item.name.startswith("worker."):
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    outputs, reason, failed = [], None, []
    for number, check in enumerate(step["gates"], 1):
        receipts, problem = gate_fn(run, state, step, path, snapshot, out, index, number, check)
        outputs.extend(receipts)
        if problem:
            reason, failed = problem, [check["name"]]
            break
    if reason is None and run.snapshot(path, state, step) != snapshot:
        reason = "A check changed tracked evidence or implementation files after the snapshot was taken."
    detail.update(tree=snapshot, failed_checks=failed, screenshots=[str(p) for p in screenshots(out, 8)],
                  checks=[{"name": r["name"], "exit": r["exit"], "passed": observed_pass(r)} for r in outputs])
    if reason is not None:
        return record(run, ex, {**detail, "passed": False, "reason": redact(reason)[:1500]}, current, names)
    write_json(out / "checks.json", outputs)
    commit = git(path, "commit-tree", snapshot, "-p", state["accepted"], "-m", step["title"])
    verify_registered_repository(run.spec, common_dir(run.repository))
    git(run.repository, "update-ref", ref, commit)
    record(run, ex, {**detail, "passed": True, "reason": None, "commit": commit, "ref": ref,
                     "checks_sha256": hashlib.sha256((out / "checks.json").read_bytes()).hexdigest()}, current, names)


def record(run, ex, detail, current, names=EXPLORE):
    """Save one finished candidate and move on to the next, or to the contest's last stage after the final one."""
    seconds = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(current["started"])).total_seconds())
    detail = {**detail, "seconds": round(seconds, 1), "summary": current["summary"], "feedback": current["feedback"]}
    kept = {k: detail[k] for k in ("index", "name", "worker", "passed", "tree", "commit", "evidence", "feedback", "failed_checks",
                                   "checks_sha256", "screenshots") if k in detail}
    kept["reason"] = (detail["reason"] or "")[:300]
    ex = {**ex, "builds": [*ex["builds"], kept], "current": None, "stage": "build" if detail["index"] < detail["count"] else names["then"]}
    run.set(names["passed"] if detail["passed"] else names["failed"], detail, **{names["key"]: ex}, last_error="", retry_at=None,
            transient_failures=0)


# ---------------------------------------------------------------- stage 3: tournament

def pairs(passed):
    return list(combinations([b["index"] for b in passed], 2))


def candidate_block(run, ex, label, build, images):
    direction = ex["directions"][build["index"] - 1]
    lines = [f'Candidate {label}: design direction "{direction["name"]}"', f"Brief: {direction['brief']}",
             f"Source folder: {run.root / 'worktrees' / ('explore-%02d' % build['index'])}"]
    lines += ["Screenshots:", *(f"- {p}" for p in images)] if images else ["Screenshots: none were produced; judge from the source."]
    return "\n".join(lines)


def comparison_prompt(run, state, ex, labelled, single):
    step = run.spec["milestones"][0]
    cap = JUDGE_IMAGES if not single else max(2, RANK_LIMIT // len(labelled))
    images = {label: [str(p) for p in build["screenshots"][:cap]] for label, build in labelled}
    blocks = [candidate_block(run, ex, label, build, images[label]) for label, build in labelled]
    return compose(run, state, ex, [label for label, _ in labelled], blocks, images, single,
                   f"Milestone 1: {step['title']}\n{step['task'][:1500]}")


def compose(run, state, ex, labels, blocks, images, single, task):
    """The judge's comparison prompt and the images to attach, for candidate blocks already written. `images` maps a label to its files."""
    names = " and ".join(labels)
    ask = ("Rank all candidates from best to worst. Which build better achieves the goal for a demanding owner — visual quality first, "
           "then completeness and polish? Judge only what you can see in the images and source. Every candidate passed the same "
           f'automated checks, so do not reward that. Return only JSON {{"ranking":["B","A"],"reason":"..."}} listing every candidate label ({names}) exactly once.'
           if single else
           "Which candidate better achieves the goal for a demanding owner — visual quality first, then completeness and polish? "
           "Judge only what you can see in the images and source. Both candidates passed the same automated checks, so do not reward that. "
           'Return only JSON {"winner":"A" or "B" or "tie","reason":"..."}.')
    return "\n\n".join([
        *([f"Your previous answer was rejected. {state['last_error']}"] if ex.get("judge_attempts") and state.get("last_error") else []),
        f"Independently compare {'these candidate builds' if single else 'two candidate builds'} of the same goal. Open the image files and read "
        "the source folders with your file-reading tools. Remain read-only; do not edit anything.",
        f"Goal\n{run.spec['objective']}", "Criteria that define done\n" + criteria_lines(run), task,
        *blocks,
        *(["Images\nOPEN each image file listed above with your file-reading tool and judge what you actually see. Candidate screenshots "
           "are listed under their own candidate; the images are attached in that order."] if any(images.values()) else []),
        ask]), [p for label in labels for p in images[label]]


def label_of(value):
    if isinstance(value, dict):
        value = value.get("candidate") or value.get("label") or value.get("name")
    return normalize(re.sub(r"^candidate[\s_-]*", "", value.strip(), flags=re.I) if isinstance(value, str) else None, ("A", "B", "C", "D", "TIE"))


def parse_comparison(answer):
    data = extract_object(answer, "winner")
    winner = label_of(data["winner"])
    if winner not in ("A", "B", "TIE"):
        raise ValueError('winner must be "A", "B" or "tie"')
    if not isinstance(data.get("reason", ""), str):
        raise ValueError("reason must be text")
    return winner, data.get("reason", "").strip()[:1500]


def parse_ranking(answer, labels):
    data = extract_object(answer, "ranking")
    ranking = data["ranking"]
    found = [label_of(item) for item in ranking] if isinstance(ranking, list) else None
    if found is None or sorted(map(str, found)) != sorted(labels):
        raise ValueError("ranking must list every candidate label exactly once: " + ", ".join(labels))
    if not isinstance(data.get("reason", ""), str):
        raise ValueError("reason must be text")
    return found, data.get("reason", "").strip()[:1500]


def tournament(run, state, ex):
    passed = [b for b in ex["builds"] if b["passed"]]
    single = options(run)["comparisons"] == "single"
    total = 1 if single else 2 * len(pairs(passed))
    if len(passed) < 2 or len(ex["comparisons"]) >= total:
        return conclude(run, state, ex, passed)
    number = len(ex["comparisons"])
    if single:
        labelled = list(zip("ABCD", passed))
    else:
        first, second = pairs(passed)[number // 2][::(1 if number % 2 == 0 else -1)]
        by_index = {b["index"]: b for b in passed}
        labelled = [("A", by_index[first]), ("B", by_index[second])]
    prompt, images = comparison_prompt(run, state, ex, labelled, single)
    ask(run, state, ex, labelled, prompt, images, single=single, total=total, workers=[worker_for(run, b["index"]) for _, b in labelled],
        names=EXPLORE, where=folder(run))


def ask(run, state, ex, labelled, prompt, images, *, single, total, workers, names, where, extra=None):
    """One judge call over `labelled` candidates ([(label, build)]); records the answer, or retries once and then an unscored tie."""
    number = len(ex["comparisons"])
    config = run.route(state, None, "judge")
    for worker in workers:
        run.independence(state, "judge", config, worker)
    log = where / f"compare-{number + 1:02d}-{state.get('provider_calls', 0) + 1:04d}.jsonl"
    (run.root / "worktrees").mkdir(parents=True, exist_ok=True)
    code, _, answer = run.call_provider(config, "judge", run.root / "worktrees", log, prompt, evidence=where, images=images,
                                        mode="ranking" if single else "comparison")
    order = [b["index"] for _, b in labelled]
    invalid = None
    try:
        if code:
            raise ValueError(f"The judge exited {code}: {(answer or tail(log))[:300]}")
        if single:
            found, reason = parse_ranking(answer, [label for label, _ in labelled])
            by_label = {label: b["index"] for label, b in labelled}
            result = {"mode": "single", "order": order, "ranking": [by_label[label] for label in found], "winner": by_label[found[0]]}
        else:
            verdict, reason = parse_comparison(answer)
            winner = None if verdict == "TIE" else order[0] if verdict == "A" else order[1]
            result = {"pair": sorted(order), "order": order, "result": "tie" if verdict == "TIE" else verdict, "winner": winner}
    except ValueError as error:
        attempts = ex.get("judge_attempts", 0) + 1
        invalid = f"The judge returned no valid comparison (attempt {attempts} of 2): {str(error)[:600]}"
        if attempts < 2:
            run.set(names["invalid"], {"attempt": attempts, "issue": invalid, "exit": code, "log": str(log)}, last_error=invalid,
                    **{names["key"]: {**ex, "judge_attempts": attempts}})
            return
        # Two unreadable answers: record an unscored tie so one bad judge call cannot wedge the run. Measurements skip it.
        reason = invalid
        result = ({"mode": "single", "order": order, "ranking": [], "winner": None} if single else
                  {"pair": sorted(order), "order": order, "result": "tie", "winner": None})
        result["invalid"] = True
    saved = {**result, "reason": reason[:400]}
    run.set(names["compared"], {**result, **(extra or {}), "number": number + 1, "of": total, "reason": reason, "log": str(log),
            **({"issue": invalid} if invalid else {})}, **{names["key"]: {**ex, "comparisons": [*ex["comparisons"], saved], "judge_attempts": 0}},
            log=str(log), last_error="", retry_at=None, transient_failures=0)


def rank(ex, passed):
    """(ranking best first, pairs whose two orders disagreed). A win scores 1 and a tie 0.5 per call; a single ranking scores n-1 .. 0."""
    scores = {b["index"]: 0.0 for b in passed}
    verdicts = {}
    for item in ex["comparisons"]:
        if item.get("invalid"):
            continue
        if item.get("mode") == "single":
            for position, index in enumerate(item["ranking"]):
                scores[index] += len(item["ranking"]) - 1 - position
        elif item["winner"] is None:
            for index in item["order"]:
                scores[index] += .5
        else:
            scores[item["winner"]] += 1
        if "pair" in item:
            verdicts.setdefault(tuple(item["pair"]), []).append(item["winner"])
    bias = [list(pair) for pair, outcomes in verdicts.items() if len(outcomes) == 2 and outcomes[0] != outcomes[1]]
    ranking = sorted(({"index": b["index"], "name": b["name"], "score": scores[b["index"]], "failed_checks": len(b.get("failed_checks", []))}
                      for b in passed), key=lambda r: (-r["score"], r["failed_checks"], r["index"]))
    return ranking, bias


def stage_manifest(run, state, build, what):
    """Put the winner's check manifest where Run.promote verifies a normal attempt 1 of the milestone it becomes."""
    manifest = (Path(build["evidence"]) / "checks.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != build["checks_sha256"]:
        raise ValueError(f"The chosen {what}'s check manifest changed after its verification")
    atomic_write(run.evidence_dir({**state, "attempt": 1}) / "checks.json", manifest)


def conclude(run, state, ex, passed):
    cleanup(run)
    if not passed:
        run.set("exploration_failed", {"directions": [{"index": b["index"], "name": b["name"], "reason": b["reason"]} for b in ex["builds"]]},
                phase="ready", explored=True, exploration={"stage": "failed"}, last_error="", retry_at=None, log=None,
                judge_route=0, judge_attempts=0)
        return
    ranking, bias = rank(ex, passed)
    pick = owner_choice(run, ranking) if len(ranking) > 1 else None
    by = "owner" if type(pick) is int and pick in [r["index"] for r in ranking[:2]] else "judge"
    winner = pick if by == "owner" else ranking[0]["index"]
    build = next(b for b in passed if b["index"] == winner)
    stage_manifest(run, state, build, "direction")
    valid = [c for c in ex["comparisons"] if not c.get("invalid")]
    done = {"stage": "done", "winner": winner, "chosen_by": by, "evidence": build["evidence"], "checks_sha256": build["checks_sha256"], "ranking": ranking,
            "builds": [{k: b[k] for k in ("index", "name", "passed", "commit")} for b in ex["builds"]]}
    run.set("direction_chosen", {"winner": winner, "name": build["name"], "commit": build["commit"], "ref": direction_ref(state, winner),
            "chosen_by": by, "mode": options(run)["comparisons"], "judged": len(passed) > 1, "ranking": ranking,
            "position_bias_pairs": bias, "comparisons": len(valid), "invalid_comparisons": len(ex["comparisons"]) - len(valid),
            "directions": len(ex["builds"]), "passed": len(passed)},
            phase="promoting", candidate=build["commit"], candidate_tree=build["tree"], checks_sha256=build["checks_sha256"],
            attempt=1, attempts_used=0, explored=True, worker_feedback_version=build["feedback"], worker_session=None,
            last_error="", retry_at=None, log=None, judge_route=0, judge_attempts=0, exploration=done)


# ---------------------------------------------------------------- reports

def report(store, run_id):
    """What the tournament did, read from the run's events; None when the run never explored."""
    events = store.events(run_id, limit=None, kinds=EVENTS)
    if not events:
        return None
    planned, built, compared, end = {}, {}, [], None
    for event in events:
        d, kind = event["details"], event["kind"]
        if kind == "direction_planned":
            planned[d["index"]] = d
        elif kind in ("direction_built", "direction_failed"):
            built[d["index"]] = d
        elif kind == "direction_compared":
            compared.append(d)
        else:
            end = {"kind": kind, **d}
    chosen = end if end and end["kind"] == "direction_chosen" else None
    scores = {r["index"]: r["score"] for r in (chosen or {}).get("ranking", [])}
    rows = []
    for index, plan in sorted(planned.items()):
        item = built.get(index)
        rows.append({"index": index, "name": plan["name"], "worker": item["worker"] if item else None,
                     "passed": item["passed"] if item else None, "reason": (item or {}).get("reason"),
                     "seconds": (item or {}).get("seconds"), "commit": (item or {}).get("commit"),
                     "score": scores.get(index), "winner": bool(chosen and chosen["winner"] == index)})
    status = {"direction_chosen": "chosen", "exploration_failed": "failed", "exploration_skipped": "skipped"}.get(end["kind"] if end else None, "in_progress")
    return {"status": status, "comparisons": chosen["mode"] if chosen else None, "winner": chosen["winner"] if chosen else None,
            "chosen_by": chosen["chosen_by"] if chosen else None, "position_bias_pairs": chosen["position_bias_pairs"] if chosen else [],
            "judge_calls": len(compared), "invalid_comparisons": sum(bool(c.get("invalid")) for c in compared),
            "reason": (end or {}).get("reason") if status == "skipped" else None, "directions": rows}


def format_lines(data):
    """Plain-text lines for the pursuit report."""
    names = {"chosen": "winner chosen by " + str(data["chosen_by"]), "failed": "no direction passed its checks",
             "skipped": "skipped: " + str(data["reason"] or ""), "in_progress": "in progress"}
    lines = ["", f"Design exploration - {names[data['status']]} ({data['judge_calls']} judge calls)"]
    for row in data["directions"]:
        first = str(row["reason"] or "").splitlines()
        state = ("not built yet" if row["passed"] is None else "passed" if row["passed"] else
                 "failed" + (": " + first[0][:120] if first else ""))
        lines.append(f"  {row['index']}. {row['name']} [{(row['worker'] or {}).get('kind', '?')}] {state}"
                     + (f", score {row['score']:g}" if row["score"] is not None else "") + (" - WINNER" if row["winner"] else ""))
    if data["position_bias_pairs"]:
        lines.append("  Position bias in pairs: " + ", ".join(f"{a} vs {b}" for a, b in data["position_bias_pairs"]))
    return lines
