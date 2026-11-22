"""Goal pursuit: judge the finished build, plan further waves, and keep the best result."""

import hashlib
import json
from pathlib import Path
import shutil
from tempfile import mkdtemp

from . import context
from .checks import contract_hash, observed_pass, run_gate
from .engine import common_dir, git, tail, verify_registered_repository
from .insights import failure_excerpt
from .process import PausedRun
from .repair import describe
from .safety import redact, write_json
from .spec import IMAGE_SUFFIXES, fields, text
from .verdicts import conflict_notes, extract_object, extract_verdict

MAX_MILESTONES = 50


def judge_dir(run, wave):
    return run.root / "evidence" / "judge" / f"{wave:02d}"


def drop_worktree(run, state):
    path = run.root / "worktrees" / f"judge-{state.get('wave', 1):02d}"
    if not path.exists():
        return
    try:
        git(run.repository, "worktree", "remove", "--force", str(path))
    except ValueError as error:
        run.set("judge_worktree_cleanup_failed", {"path": str(path), "issue": str(error)})
        shutil.rmtree(path, ignore_errors=True)
        try:
            git(run.repository, "worktree", "prune")
        except ValueError:
            pass


def worktree(run, state, *, fresh=False):
    path = run.root / "worktrees" / f"judge-{state.get('wave', 1):02d}"
    if path.exists():
        try:
            if (path / ".git").is_file() and common_dir(path) == run.gitdir and git(path, "rev-parse", "HEAD") == state["accepted"]:
                if fresh:
                    git(path, "reset", "--hard", "-q")
                    git(path, "clean", "-fdxq")
                return path
        except ValueError:
            pass
        drop_worktree(run, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(run.repository, "worktree", "add", "--detach", str(path), state["accepted"])
    run.set("worktree_ready", {"path": str(path), "base": state["accepted"], "role": "judge"})
    return path


def slot(parent, number):
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / f"{number:02d}"
    try:
        folder.mkdir()
    except FileExistsError:
        folder = Path(mkdtemp(prefix=f"{number:02d}-recheck-", dir=parent))
    return folder


def ledger(run, state, path, folder, tree):
    """Re-run every distinct check from every milestone on the judged build."""
    seen, checks = set(), []
    for milestone in run.spec["milestones"]:
        for check in milestone["gates"]:
            if (digest := contract_hash(check)) not in seen:
                seen.add(digest)
                checks.append(check)
    base = state["base"]
    rows = []
    for number, check in enumerate(checks, 1):
        if run.paused():
            raise PausedRun()
        out = slot(folder, number)
        baseline = run.spec.get("proof_base", base) if "proof" in check else base
        receipt = run_gate(check, path, out, tree, paused=run.paused,
                           **({"baseline": baseline} if check.get("kind") == "catalog" or "proof" in check else {}),
                           max_log_bytes=run.spec.get("budgets", {}).get("max_log_bytes", 16 * 1024 * 1024),
                           heartbeat=lambda info: run.set("process_heartbeat", {**info, "role": "gate"}))
        receipt.update(wave=state.get("wave", 1), ledger_index=number)
        log = out / "gate.log"
        if log.is_file() or observed_pass(receipt) is not None:
            try:
                with log.open("rb") as stream:
                    receipt["log_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as error:
                receipt.update(execution_error=True, artifact_error=f"Cannot preserve gate log: {error}")
        if git(path, "status", "--porcelain", "--untracked-files=no"):
            receipt["source_error"] = "The check changed tracked files in the judge worktree."
            git(path, "reset", "--hard", "-q")
        if "proof" in receipt:
            receipt["proof"]["passed"] = observed_pass(receipt) is True
        run.set("judge_gate_finished", receipt)
        passed = observed_pass(receipt)
        row = {"name": check["name"], "passed": passed is True, "log": str(log)}
        if passed is not True:
            row["note"] = redact((receipt.get("artifact_error") or receipt.get("source_error") or "") if passed is None
                                 else failure_excerpt(log) or f"exit {receipt['exit']}")[:1200]
        rows.append(row)
    return rows


def screenshots(folder, limit=8):
    found = {}
    if folder.is_dir():
        files = []
        for file in folder.rglob("*"):
            try:
                if file.suffix.casefold() in IMAGE_SUFFIXES and file.is_file() and not file.is_symlink() and 0 < file.stat().st_size <= 20 * 1024 * 1024:
                    files.append((file.stat().st_mtime_ns, file))
            except OSError:
                continue
        for _, file in sorted(files, key=lambda item: item[0], reverse=True):
            found.setdefault(hashlib.sha256(file.read_bytes()).hexdigest(), file)
    return list(found.values())[:limit]


def reachable(run, state):
    """Commits of the current build. After a revert, a discarded wave's milestones are no longer part of it."""
    try:
        return set(git(run.repository, "rev-list", f"{state['base']}..{state['accepted']}").split())
    except ValueError:
        return None


def lineage(run, state):
    ours = reachable(run, state)
    return [f"- {run.spec['milestones'][item['step'] - 1]['title']} ({item['commit'][:12]})"
            for item in run.store.accepted_milestones(run.id) if ours is None or item["commit"] in ours]


def titles(gaps, count=3):
    return "; ".join(gap["title"] for gap in gaps[:count]) or "none recorded"


def compact(gaps):
    return [{"title": g["title"], "detail": g["detail"][:500]} for g in gaps[:8]]


def judged_lines(run):
    lines = []
    for event in run.store.events(run.id, limit=None, kinds=["goal_judged"]):
        d = event["details"]
        lines.append(f"- Wave {d['wave']}: {d['score']}/100 {d['verdict']} at {d['commit'][:12]}. Top gaps: {titles(d.get('gaps', []))}")
    return lines


def judge_prompt(run, state, results, images, references):
    pursue = run.spec["pursue"]
    criteria = pursue["criteria"] or run.spec.get("acceptance", [])
    history = judged_lines(run)
    summary = [f"- {'PASS' if r['passed'] else 'FAIL'} {r['name']}" + ("" if r["passed"] else f": {r['note'][:400]}") + f" (log: {r['log']})"
               for r in results]
    parts = [
        "Independently judge whether this software goal has been achieved by the current build. "
        "Read the actual source, the check logs and the images. Remain read-only; do not edit anything.",
        f"Goal\n{run.spec['objective']}",
        "Criteria that define done\n" + ("\n".join(f"- {c}" for c in criteria) if criteria else "- None declared; judge against the goal."),
        f"Accepted milestones in this build (wave {state.get('wave', 1)})\n" + ("\n".join(lineage(run, state)) or "None"),
        "Evidence ledger. The controller re-ran every check on this exact build just now\n" + ("\n".join(summary) or "No checks are configured."),
        "Earlier judgments\n" + ("\n".join(history) or "None; this is the first."),
    ]
    if images or references:
        parts.append(
            "Images\nOPEN each image file listed here with your file-reading tool and judge what you actually see, "
            "not what any log claims. " + ("Compare the screenshots with the reference images: layout, hierarchy, typography, color, spacing and polish.\n"
                                           if references else "Judge layout, hierarchy, typography, color, spacing and polish.\n")
            + ("Screenshots of the current build:\n" + "\n".join(f"- {p}" for p in images) + "\n" if images else "")
            + ("Reference images (the target):\n" + "\n".join(f"- {p}" for p in references) if references else ""))
    else:
        parts.append("No screenshots or reference images were produced. Judge from source and logs, and list a missing visual check as a gap if appearance matters.")
    if state.get("judge_attempts") and state.get("last_error"):
        parts.append(f"Your previous answer was rejected. {state['last_error']}")
    parts.append(
        f"Score 0-100 how completely and how well the current build achieves the goal; {pursue['target_score']}+ means a demanding owner "
        "would accept it as finished; be specific and evidence-based; do not reward claims, only what you can verify in source, logs and images. "
        "List one criteria entry per criterion above. Order gaps by importance; each gap must be something a code change can close.\n"
        'Return only JSON {"verdict":"MET" or "NOT_MET","score":0-100,"criteria":[{"criterion":"...","met":true,"note":"..."}],'
        '"gaps":[{"title":"...","detail":"..."}],"reason":"..."}')
    return "\n\n".join(parts)


def parse_judgment(answer):
    data = extract_verdict(answer, ("MET", "NOT_MET"))
    score = data.get("score")
    if type(score) is not int or not 0 <= score <= 100:
        raise ValueError("score must be an integer between 0 and 100")
    gaps, criteria = data.get("gaps", []), data.get("criteria", [])
    if not isinstance(gaps, list) or len(gaps) > 30 or not isinstance(criteria, list) or len(criteria) > 60:
        raise ValueError("gaps and criteria must be lists of reasonable length")
    if not isinstance(data.get("reason", ""), str):
        raise ValueError("reason must be text")
    for gap in gaps:
        if not isinstance(gap, dict) or not isinstance(gap.get("title"), str) or not gap["title"].strip() or not isinstance(gap.get("detail", ""), str):
            raise ValueError("each gap needs a title and a detail")
    for item in criteria:
        if (not isinstance(item, dict) or not isinstance(item.get("criterion"), str) or type(item.get("met")) is not bool
                or not isinstance(item.get("note", ""), str)):
            raise ValueError("each criterion needs a criterion, a boolean met and a note")
    return {"verdict": data["verdict"], "score": score, "reason": data.get("reason", "").strip()[:3000],
            "gaps": [{"title": g["title"].strip()[:200], "detail": g.get("detail", "").strip()[:1500]} for g in gaps],
            "criteria": [{"criterion": c["criterion"][:400], "met": c["met"], "note": c.get("note", "")[:600]} for c in criteria]}


def invalid(run, state, kind, error, log, code):
    wave = state.get("wave", 1)
    key = "judge_attempts" if kind == "judge" else "plan_attempts"
    attempts = state.get(key, 0) + 1
    phase = "judging" if kind == "judge" else "planning"
    issue = f"The {kind} returned no valid {'judgment' if kind == 'judge' else 'plan'} (attempt {attempts} of 2): {str(error)[:600]}"
    blocked = attempts >= 2
    run.set(f"{kind}_invalid", {"wave": wave, "attempt": attempts, "issue": issue, "exit": code, "log": str(log)},
            following_event=("blocked", {"issue": issue, "during": phase}) if blocked else None, last_error=issue, **{key: attempts},
            **({"phase": "blocked", "blocked_from": phase} if blocked else {}))


def revert(run, state, wave, current, best):
    verify_registered_repository(run.spec, common_dir(run.repository))
    discarded, number = f"{run.ref}-discarded-w{wave:02d}", 1
    while True:
        try:
            if git(run.repository, "rev-parse", "--verify", discarded) == current:
                break
        except ValueError:
            git(run.repository, "update-ref", discarded, current)
            break
        number += 1
        discarded = f"{run.ref}-discarded-w{wave:02d}-{number}"
    live = git(run.repository, "rev-parse", "--verify", run.ref)
    if live == current:
        git(run.repository, "update-ref", run.ref, best, current)
    elif live != best:
        raise ValueError("The result branch moved outside this run. Existing commits were preserved.")
    return discarded


def stopping(reason, wave, best_score, best_commit, best_gaps):
    """The message and event of a pursuit that stops without meeting its goal."""
    message = (f"Goal not met after {wave} wave{'s' * (wave != 1)} (best score {best_score}/100). "
               f"Top gaps: {titles(best_gaps)}")
    return message, ("pursuit_stopped", {"reason": reason, "best_score": best_score, "best_commit": best_commit, "waves": wave})


def judge(run, state):
    run.spec = run.store.get(run.id)["spec"]
    pursue, wave, commit = run.spec["pursue"], state.get("wave", 1), state["accepted"]
    path = worktree(run, state, fresh=True)
    folder = judge_dir(run, wave)
    results = None
    if state.get("judge_ledger") == commit:
        # The checks already ran on this build in this judgment; a retry or fallback only repeats the model call.
        try:
            saved = json.loads((folder / "ledger.json").read_text(encoding="utf-8"))
            results = saved["results"] if saved["commit"] == commit else None
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if results is None:
        results = ledger(run, state, path, folder / "ledger", git(path, "rev-parse", "HEAD^{tree}"))
        write_json(folder / "ledger.json", {"commit": commit, "results": results})
        run.set("judge_ledger_ready", {"wave": wave, "commit": commit, "failed": [r["name"] for r in results if not r["passed"]]},
                judge_ledger=commit)
    images = screenshots(folder / "ledger")
    references = pursue["references"]
    for reference in references:
        if not Path(reference).is_file():
            raise ValueError(f"The reference image is missing: {reference}")
    log = folder / f"judge-{state.get('provider_calls', 0) + 1:04d}.jsonl"
    config = run.route(state, None, "judge")
    run.independence(state, "judge", config, run.route(state, run.spec["milestones"][-1], "worker"))
    run.set("judge_started", {"adapter": config["kind"], "wave": wave, "commit": commit}, log=str(log), retry_at=None)
    code, _, answer = run.call_provider(config, "judge", path, log, judge_prompt(run, state, results, images, references),
                                        evidence=folder, images=[*images, *references])
    try:
        if code:
            raise ValueError(f"The judge exited {code}: {(answer or tail(log))[:300]}")
        verdict = parse_judgment(answer)
    except ValueError as error:
        invalid(run, state, "judge", error, log, code)
        return
    failed = [r for r in results if not r["passed"]]
    score, target = verdict["score"], pursue["target_score"]
    needs = [*verdict["gaps"], *({"title": f"Check failed: {r['name']}", "detail": r.get("note", "")} for r in failed)]
    needs = needs or [{"title": "The judge does not consider the goal met", "detail": verdict["reason"] or f"Score {score} against a target of {target}."}]
    best, best_commit = state.get("best_score"), state.get("best_commit")
    beat = best is None or score > best
    stale = 0 if beat else state.get("stale_judgments", 0) + 1
    regressed = not beat and score < best - pursue["regression_margin"]
    kept = {"best_score": score if beat else best, "best_commit": commit if beat else best_commit,
            "best_gaps": compact(needs) if beat else state.get("best_gaps", []), "stale_judgments": stale}
    result = {**kept, "judgments": [*state.get("judgments", []), {"wave": wave, "score": score, "verdict": verdict["verdict"], "commit": commit}],
              "judge_attempts": 0, "judge_route": 0, "judge_ledger": None, "transient_failures": 0}
    payload = {"wave": wave, "verdict": verdict["verdict"], "score": score, "gaps": verdict["gaps"], "criteria": verdict["criteria"],
               "reason": verdict["reason"], "ledger": [{k: r[k] for k in ("name", "passed", "note") if k in r} for r in results],
               "screenshots": [str(p) for p in images], "log": str(log), "commit": commit}
    write_json(folder / "judgment.json", redact(payload))
    if verdict["verdict"] == "MET" and score >= target and not failed:
        drop_worktree(run, state)
        run.set("goal_judged", payload, following_event=[("goal_met", {"wave": wave, "score": score, "commit": commit}),
                ("run_completed", {"branch": run.ref, "commit": commit})], phase="done", last_error="", **result)
        return
    stop = ("wave_limit" if wave - 1 >= pursue["max_waves"] else "plateau" if stale >= pursue["plateau"] else None)
    events, note = [], ""
    if (regressed or stop) and kept["best_commit"] != commit:
        discarded = revert(run, state, wave, commit, kept["best_commit"])
        events.append(("pursuit_reverted", {"wave": wave, "from": commit, "to": kept["best_commit"], "score": score,
                                            "best_score": kept["best_score"], "discarded_ref": discarded}))
        result["accepted"] = kept["best_commit"]
        note = (f"Wave {wave} scored {score}/100, below the best score of {kept['best_score']}/100, so its changes were discarded "
                f"(saved at {discarded}). Plan from the best build. That wave's own gaps: {titles(verdict['gaps'])}.")
    if stop == "plateau":
        from . import rescue
        if rescue.available(run, state):
            # Progress stalled on the best build: diagnose why and try divergent strategies before giving up.
            drop_worktree(run, state)
            started, fresh = rescue.begin(run, state, wave, kept)
            run.set("goal_judged", payload, following_event=[*events, ("rescue_started", started)], phase="rescuing", rescue=fresh,
                    last_error="", retry_at=None, planner_route=0, **result)
            return
    if stop:
        drop_worktree(run, state)
        message, stopped = stopping(stop, wave, kept["best_score"], kept["best_commit"], result["best_gaps"])
        events.append(stopped)
        run.set("goal_judged", payload, following_event=events, phase="blocked", blocked_from="judging", last_error=message, **result)
        return
    run.set("goal_judged", payload, following_event=events, phase="planning", plan_attempts=0, planner_route=0, last_error="",
            pursuit_gaps=result["best_gaps"] if note else compact(needs), pursuit_note=note,
            pursuit_failures=[] if note else [{"name": r["name"], "note": r.get("note", "")[:500]} for r in failed], **result)


def parse_plan(answer, room):
    return parse_wave(answer, room)[0]


def parse_wave(answer, room):
    data = extract_object(answer, "milestones")
    fields(data, "milestones conflicts", "planner response")
    items = data.get("milestones")
    if not isinstance(items, list) or not 1 <= len(items) <= room:
        raise ValueError(f"The plan must contain 1 to {room} milestones")
    planned = []
    for item in items:
        fields(item, "title task", "planned milestone")
        title, task = text(item, "title"), text(item, "task")
        if len(title) > 200 or len(task) > 12000:
            raise ValueError("A planned milestone title or task is too long")
        planned.append({"title": title, "task": task})
    return planned, conflict_notes(data.get("conflicts"))


def plan_prompt(run, state, room):
    pursue, wave = run.spec["pursue"], state.get("wave", 1)
    last = run.spec["milestones"][-1]
    latest = next(iter(run.store.events(run.id, limit=1, kinds=["goal_judged"])), None)
    judged = latest["details"] if latest else {}
    criteria = pursue["criteria"] or run.spec.get("acceptance", [])
    gaps = "\n".join(f"- {g['title']}: {g['detail']}" for g in state.get("pursuit_gaps", [])) or "- None recorded"
    failures = "\n".join(f"- {f['name']}: {f['note']}" for f in state.get("pursuit_failures", [])) or "- None"
    unmet = "\n".join(f"- {c['criterion']}: {c['note']}" for c in judged.get("criteria", []) if not c["met"]) or "- None reported"
    return "\n\n".join([
        *([f"Your previous answer was rejected. {state['last_error']}"] if state.get("plan_attempts") and state.get("last_error") else []),
        "You plan the next wave of milestones for an autonomous coding run that is pursuing a goal. "
        "Read the repository and the supplied evidence as needed. Remain read-only; do not edit anything.",
        f"Goal\n{run.spec['objective']}",
        "Criteria that define done\n" + ("\n".join(f"- {c}" for c in criteria) if criteria else "- None declared; judge against the goal."),
        f"Accepted milestones in this build\n" + ("\n".join(lineage(run, state)) or "None"),
        f"Latest independent judgment: wave {judged.get('wave', wave)} scored {judged.get('score', '?')}/100, best {state.get('best_score', '?')}/100, "
        f"target {pursue['target_score']}\nUnmet criteria\n{unmet}",
        (state.get("pursuit_note") + "\n\n" if state.get("pursuit_note") else "") + f"Gaps to close, most important first\n{gaps}",
        f"Checks that failed on the current build\n{failures}",
        "Checks every new milestone must pass; the controller runs these exact checks\n" + "\n".join(
            "- " + g["name"] + ": " + json.dumps(g, ensure_ascii=False) for g in last["gates"]),
        context.brief(run.store, run.id),
        f"Plan the next 1..{room} milestones that close the most important gaps first. Each must make a useful tracked change and pass these checks. "
        'Do not plan work that is already done. Return only JSON {"milestones":[{"title":"...","task":"..."}]}. '
        "Give each a short title and a detailed, self-contained task with a testable accepted result. "
        'If any requirements contradict each other or cannot be satisfied as stated, list them as an additional "conflicts":["..."] '
        "array next to milestones; otherwise omit it."])


def plan_wave(run, state):
    run.spec = run.store.get(run.id)["spec"]
    wave = state.get("wave", 1)
    room = min(run.spec["pursue"]["wave_size"], MAX_MILESTONES - len(run.spec["milestones"]))
    if room < 1:
        drop_worktree(run, state)
        best = state.get("best_score")
        message = f"Goal not met: the run reached the {MAX_MILESTONES} milestone limit (best score {best}/100). Top gaps: {titles(state.get('pursuit_gaps', []))}"
        run.set("pursuit_stopped", {"reason": "milestone_limit", "best_score": best, "best_commit": state.get("best_commit"), "waves": wave},
                phase="blocked", blocked_from="judging", last_error=message)
        return
    path = worktree(run, state)
    folder = judge_dir(run, wave)
    log = folder / f"plan-{state.get('provider_calls', 0) + 1:04d}.jsonl"
    config = run.route(state, None, "planner")
    run.set("planner_started", {"adapter": config["kind"], "wave": wave}, log=str(log), retry_at=None)
    code, _, answer = run.call_provider(config, "planner", path, log, plan_prompt(run, state, room), evidence=folder, mode="replanner")
    try:
        if code:
            raise ValueError(f"The planner exited {code}: {(answer or tail(log))[:300]}")
        planned, conflicts = parse_wave(answer, room)
    except ValueError as error:
        invalid(run, state, "plan", error, log, code)
        return
    last = run.spec["milestones"][-1]
    steps = [{**item, "gates": last["gates"], "paths": last["paths"], "worker": last["worker"],
              "worker_fallbacks": last["worker_fallbacks"]} for item in planned]
    drop_worktree(run, state)
    run.store.extend_plan(run.id, steps, {"wave": wave + 1, "titles": [s["title"] for s in steps], "planner": describe(config),
                                          **({"conflicts": conflicts} if conflicts else {})},
                          phase="ready", wave=wave + 1, attempt=0, attempts_used=0, worker_session=None, worker_route=0,
                          reviewer_route=0, last_error="", candidate=None, candidate_tree=None, retry_at=None, log=None,
                          plan_attempts=0, planner_route=0, failure_count=0, failure_fingerprint=None, route_rejections=0,
                          transient_failures=0, verification_batch=None, pursuit_conflicts=conflicts)
    run.spec = run.store.get(run.id)["spec"]


def worker_brief(spec, state):
    judged = state.get("judgments")
    if not judged or not spec.get("pursue"):
        return ""
    pursue = spec["pursue"]
    return (f"Pursuit\nThis run keeps iterating until its goal is met: this is wave {state.get('wave', 1)} of at most {pursue['max_waves'] + 1}. "
            f"The last independent judgment scored {judged[-1]['score']}/100 (best {state.get('best_score')}/100, target {pursue['target_score']}). "
            f"Open gaps: {titles(state.get('pursuit_gaps', []), 4)}.\n")


def report(store, run_id):
    row = store.get(run_id)
    spec, state = row["spec"], row["state"]
    pursue = spec.get("pursue")
    if not pursue:
        raise ValueError("This run does not pursue a goal")
    events = store.events(run_id, limit=None, kinds=["wave_planned", "milestone_accepted", "goal_judged", "pursuit_reverted",
                                                      "pursuit_stopped", "pursuit_extended", "goal_met"])
    planned = [e["details"] for e in events if e["kind"] == "wave_planned"]
    from .rescue import report as rescue_report
    rescued = rescue_report(store, run_id)
    firsts = [1, *(d["first_step"] for d in planned)]
    accepted = {e["details"]["step"]: e["details"] for e in events if e["kind"] == "milestone_accepted"}
    waves = []
    for index, first in enumerate(firsts):
        last = firsts[index + 1] - 1 if index + 1 < len(firsts) else len(spec["milestones"])
        number = index + 1
        judged = next((e["details"] for e in reversed(events) if e["kind"] == "goal_judged" and e["details"]["wave"] == number), None)
        reverted = next((e["details"] for e in reversed(events) if e["kind"] == "pursuit_reverted" and e["details"]["wave"] == number), None)
        waves.append({"wave": number, "planned_by": planned[index - 1]["planner"] if index else None,
                      **({"rescue_strategy": planned[index - 1]["strategy"]} if index and planned[index - 1].get("rescue") else {}),
                      **({"rescue": found} if (found := next((r for r in rescued if r["wave"] == number), None)) else {}),
                      "milestones": [{"step": n, "title": spec["milestones"][n - 1]["title"], "accepted": n in accepted,
                                      "commit": accepted.get(n, {}).get("commit")} for n in range(first, last + 1)],
                      "judgment": None if judged is None else {k: judged[k] for k in ("verdict", "score", "commit", "gaps", "reason")}
                      | {"criteria_met": sum(c["met"] for c in judged["criteria"]), "criteria_total": len(judged["criteria"]),
                         "failed_checks": [c["name"] for c in judged["ledger"] if not c["passed"]]},
                      "reverted": reverted})
    stopped = next((e["details"] for e in reversed(events) if e["kind"] == "pursuit_stopped"), None)
    from .explore import report as exploration
    explored = exploration(store, run_id) if pursue.get("explore") else None
    return redact({"run_id": run_id, "title": spec["title"], "objective": spec["objective"], "phase": state["phase"], "paused": row["pause"],
                   **({"exploration": explored} if explored else {}),
                   "wave": state.get("wave", 1), "max_waves": pursue["max_waves"], "target_score": pursue["target_score"],
                   "best_score": state.get("best_score"), "best_commit": state.get("best_commit"),
                   "result_branch": state.get("result_ref", "").removeprefix("refs/heads/"), "accepted": state["accepted"],
                   "met": any(e["kind"] == "goal_met" for e in events),
                   "stopped": stopped if state["phase"] == "blocked" and state.get("blocked_from") == "judging" else None,
                   "last_error": state.get("last_error") or None, "extensions": sum(e["kind"] == "pursuit_extended" for e in events),
                   "waves": waves})


def format_report(data):
    lines = [f"Run {data['run_id']} - {data['title']}",
             f"Phase {data['phase']}{' (paused)' if data['paused'] else ''} - wave {data['wave']} of at most {data['max_waves'] + 1} - "
             f"best score {data['best_score'] if data['best_score'] is not None else 'none yet'} (target {data['target_score']}) - result {data['result_branch']}",
             f"Goal: {data['objective']}"]
    if data.get("exploration"):
        from .explore import format_lines
        lines += format_lines(data["exploration"])
    for wave in data["waves"]:
        lines += ["", f"Wave {wave['wave']}" + (f" (rescue: {wave['rescue_strategy']})" if wave.get("rescue_strategy") else
                                                 f" (planned by {wave['planned_by'].get('kind')})" if wave["planned_by"] else " (initial plan)")]
        lines += [f"  {m['step']}. {m['title']} - " + (f"accepted {m['commit'][:12]}" if m["accepted"] else "not accepted") for m in wave["milestones"]]
        if judgment := wave["judgment"]:
            lines.append(f"  Judged {judgment['verdict']} {judgment['score']}/100 at {judgment['commit'][:12]}, "
                         f"criteria {judgment['criteria_met']}/{judgment['criteria_total']}")
            lines += [f"    gap: {g['title']}" for g in judgment["gaps"][:3]]
            lines += [f"    failed check: {name}" for name in judgment["failed_checks"]]
        if wave["reverted"]:
            lines.append(f"  Reverted to {wave['reverted']['to'][:12]} (score {wave['reverted']['score']} against best {wave['reverted']['best_score']}); "
                         f"discarded work kept at {wave['reverted']['discarded_ref']}")
        if wave.get("rescue"):
            from .rescue import format_lines as rescue_lines
            lines += rescue_lines(wave["rescue"])
    if data["met"]:
        lines += ["", "Goal met."]
    elif data["stopped"]:
        lines += ["", f"Stopped: {data['stopped']['reason']}. {data['last_error'] or ''}".rstrip(),
                  f"Best build {data['best_commit'][:12] if data['best_commit'] else 'none'}; extend with: nightweaver pursuit {data['run_id']} --add-waves N"]
    elif data["last_error"]:
        lines += ["", f"Blocked: {data['last_error']}"]
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")
