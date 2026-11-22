"""Plateau rescue: when a pursuit stalls, diagnose why, fork divergent strategies from the best build and keep one only if it beats it.

State machine, kept in a compact `rescue` dict so every stage resumes after a crash without repeating finished work:
  diagnose  one read-only call says why progress stalled and names N genuinely different strategies (two invalid answers skip the rescue)
  build     each strategy gets one attempt in its own worktree at the best commit, is checked like a milestone and committed on pass
  select    the incumbent (the best build) meets each passing strategy pairwise in both orders; a strategy qualifies only if it beats
            the incumbent on aggregate, and qualifiers with the same score meet each other
  promote   the winner is appended as the milestone "Rescue: <name>" and promoted like any milestone; with no winner the pursuit
            stops as a plateau on the best build, which the result branch already holds
The contest machinery (worktrees, checks, judge calls, scoring) is explore.py's; the incumbent is candidate 0.
"""

from itertools import combinations
import json
from pathlib import Path

from . import context
from .engine import tail
from .explore import JUDGE_IMAGES, ask, build_one, checkout, cleanup, compose, criteria_lines, rank, run_check, stage_manifest
from .insights import budget_status
from .pursuit import MAX_MILESTONES, lineage, stopping, titles
from .repair import describe, family
from .spec import text
from .verdicts import extract_object

EVENTS = ["rescue_started", "rescue_diagnosed", "rescue_candidate_built", "rescue_candidate_failed", "rescue_compared",
          "rescue_chosen", "rescue_failed", "rescue_skipped"]
RESCUE = {"key": "rescue", "started": "rescue_candidate_started", "worked": "rescue_worker_finished", "passed": "rescue_candidate_built",
          "failed": "rescue_candidate_failed", "compared": "rescue_compared", "invalid": "rescue_comparison_invalid", "then": "select"}
INCUMBENT = "Current best build"
OUTCOMES = {"chosen": "won", "in_progress": "in progress"}


def options(run):
    return run.spec["pursue"]["rescue"]


def available(run, state):
    """Whether a plateau may start a rescue. A run planned before rescues existed has no rescue block and never rescues."""
    rescue = run.spec["pursue"].get("rescue")
    return bool(rescue) and state.get("rescues_used", 0) < rescue["max_rescues"] and len(run.spec["milestones"]) < MAX_MILESTONES


def begin(run, state, wave, kept):
    """(rescue_started details, the rescue state) for a plateau after judging `wave`."""
    rescue = options(run)
    used = state.get("rescues_used", 0)
    started = {"wave": wave, "best_score": kept["best_score"], "best_commit": kept["best_commit"], "stale_judgments": kept["stale_judgments"],
               "rescue": used + 1, "max_rescues": rescue["max_rescues"], "strategies": rescue["strategies"]}
    return started, {"stage": "diagnose", "wave": wave, "step": state["step"], "best_commit": kept["best_commit"], "best_score": kept["best_score"],
                     "stale": kept["stale_judgments"], "count": rescue["strategies"], "attempts": 0, "builds": [], "comparisons": [],
                     "current": None}


def folder(run, ex, index=None):
    path = run.root / "evidence" / "rescue" / f"w{ex['wave']:02d}"
    return path if index is None else path / f"{index:02d}"


def worker_for(run, index):
    workers = options(run).get("workers") or [run.spec["milestones"][-1]["worker"]]
    return workers[(index - 1) % len(workers)]


def strategy_ref(state, ex, index):
    return f"{state['result_ref']}-rescue-w{ex['wave']:02d}-{index:02d}"


def diagnoser(run, state):
    """(role, provider config) of the diagnosis: the planner, unless it comes from the worker's own vendor family, then the judge."""
    role = "planner" if family(run.spec["pursue"]["planner"]) != family(run.spec["milestones"][-1]["worker"]) else "judge"
    return role, run.route(state, None, role)


def active_route(run, state):
    """(role, provider config) of the rescue stage in progress, for fallbacks, parking and blocked states."""
    ex = state["rescue"]
    if ex["stage"] == "diagnose":
        return diagnoser(run, state)
    if ex["stage"] == "build":
        return "worker", worker_for(run, len(ex["builds"]) + 1)
    return "judge", run.route(state, None, "judge")


def advance(run, state):
    """Do one unit of rescue work; the run loop calls this again until the phase leaves rescuing."""
    run.spec = run.store.get(run.id)["spec"]
    ex = state["rescue"]
    {"diagnose": diagnose, "build": build_strategy, "select": select, "promote": promote}[ex["stage"]](run, state, ex)


def incumbent(run, ex):
    """The best build checked out read-only; the diagnosis and the judge read its source here."""
    return checkout(run, ex["best_commit"], "rescue-00", role="rescue")


def best_images(run, ex):
    """Screenshots of the best build, from its latest judgment's evidence ledger."""
    for event in reversed(run.store.events(run.id, limit=None, kinds=["goal_judged"])):
        if event["details"]["commit"] == ex["best_commit"]:
            return [p for p in event["details"].get("screenshots", []) if Path(p).is_file()][:JUDGE_IMAGES]
    return []


# ---------------------------------------------------------------- stage 1: diagnose

def diagnosis_prompt(run, state, ex, images):
    from .pursuit import report
    waves, lines = report(run.store, run.id)["waves"], []
    for wave in waves:
        line = f"- Wave {wave['wave']}: " + "; ".join(m["title"] + ("" if m["accepted"] else " (not accepted)") for m in wave["milestones"])
        if judged := wave["judgment"]:
            line += (f" -> {judged['verdict']} {judged['score']}/100 at {judged['commit'][:12]}, criteria {judged['criteria_met']}/{judged['criteria_total']}. "
                     f"Top gaps: {titles(judged['gaps'])}")
        lines.append(line + (" (discarded; the best build was restored)" if wave["reverted"] else ""))
    earlier = [f"- Wave {w['wave']}: " + ", ".join(f"{r['name']} ({'won' if r['winner'] else 'did not beat the best build'})" for r in w["rescue"]["strategies"])
               for w in waves if w.get("rescue") and w["rescue"]["strategies"]]
    rejected = [f"- Milestone {d['step']} attempt {d['attempt']} ({d['phase']}): " + " ".join(str(d["issue"]).split())[:300]
                for d in (e["details"] for e in run.store.events(run.id, limit=6, kinds=["attempt_rejected"]))]
    gaps = "\n".join(f"- {g['title']}: {g['detail']}" for g in state.get("best_gaps", [])) or "- None recorded"
    count = ex["count"]
    return "\n\n".join([
        *([f"Your previous answer was rejected. {state['last_error']}"] if ex["attempts"] and state.get("last_error") else []),
        f"You diagnose an autonomous coding run whose progress has stalled. {count} different builders will each get one attempt, starting from "
        "the best build so far, and an independent judge will keep a result only if it beats that build. Read the repository as needed. "
        "Remain read-only; do not edit anything.",
        f"Goal\n{run.spec['objective']}", "Criteria that define done\n" + criteria_lines(run),
        "Judgment history, with the milestones built in each wave\n" + "\n".join(lines),
        f"The best build scored {ex['best_score']}/100 at {ex['best_commit'][:12]}; the last {ex['stale']} judgments did not beat it.\n"
        f"Gaps in the best build, most important first\n{gaps}",
        "Recent rejection reasons\n" + ("\n".join(rejected) or "- None"),
        *(["Earlier rescue attempts on this run\n" + "\n".join(earlier)] if earlier else []),
        *(["Screenshots of the best build. OPEN each image file with your file-reading tool and judge what you actually see:\n"
           + "\n".join(f"- {p}" for p in images)] if images else []),
        "Say in a few sentences why progress stalled: what the repeated waves kept doing that did not move the score. "
        f"Then propose exactly {count} genuinely different strategies for getting past the plateau, for example rethink the layout and composition, "
        "change the rendering or implementation technique, or run a deep polish pass on the weakest criteria. At least one must be a bold rethink "
        "that abandons the approach that stalled; none may be a rephrasing of another or more of the same. Give each a short, distinctive name and an "
        "approach of three to six sentences concrete enough to build without further questions. "
        'Return only JSON {"diagnosis":"...","strategies":[{"name":"...","approach":"..."}]} with exactly ' + f"{count} strategies."])


def parse_diagnosis(answer, count):
    data = extract_object(answer, "strategies")
    items, diagnosis = data["strategies"], data.get("diagnosis")
    if not isinstance(items, list) or len(items) != count:
        raise ValueError(f"The answer must list exactly {count} strategies")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise ValueError("diagnosis must be text")
    found = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each strategy must be an object with a name and an approach")
        found.append({"name": text(item, "name")[:80], "approach": text(item, "approach")[:3000]})
    if len({s["name"].casefold() for s in found}) != count:
        raise ValueError("strategy names must be distinct")
    return diagnosis.strip()[:3000], found


def diagnose(run, state, ex):
    role, config = diagnoser(run, state)
    path, images = incumbent(run, ex), best_images(run, ex)
    where = folder(run, ex)
    where.mkdir(parents=True, exist_ok=True)
    log = where / f"diagnosis-{state.get('provider_calls', 0) + 1:04d}.jsonl"
    if role == "judge":
        run.independence(state, "judge", config, run.spec["milestones"][-1]["worker"])
    run.set("rescue_diagnosis_started", {"adapter": config["kind"], "role": role, "wave": ex["wave"], "attempt": ex["attempts"] + 1},
            log=str(log), retry_at=None)
    code, _, answer = run.call_provider(config, role, path, log, diagnosis_prompt(run, state, ex, images), evidence=where, images=images,
                                        mode="diagnosis")
    try:
        if code:
            raise ValueError(f"The {role} exited {code}: {(answer or tail(log))[:300]}")
        diagnosis, strategies = parse_diagnosis(answer, ex["count"])
    except ValueError as error:
        attempts = ex["attempts"] + 1
        issue = f"The {role} returned no valid diagnosis (attempt {attempts} of 2): {str(error)[:600]}"
        if attempts < 2:
            run.set("rescue_diagnosis_invalid", {"attempt": attempts, "issue": issue, "exit": code, "log": str(log)}, last_error=issue,
                    rescue={**ex, "attempts": attempts})
            return
        return stop(run, state, ex, [("rescue_skipped", {"wave": ex["wave"], "reason": issue, "stage": "diagnose", "log": str(log)})])
    run.set("rescue_diagnosed", {"wave": ex["wave"], "role": role, "diagnoser": describe(config), "diagnosis": diagnosis, "strategies": strategies,
            "images": len(images)}, rescue={**ex, "stage": "build", "diagnosis": diagnosis, "strategies": strategies, "diagnoser": describe(config)},
            last_error="", retry_at=None, transient_failures=0, **{role + "_route": 0})


# ---------------------------------------------------------------- stage 2: build and check each strategy

def build_prompt(run, state, ex, strategy):
    last, number = run.spec["milestones"][-1], len(run.spec["milestones"]) + 1
    paths = ", ".join(last["paths"]) or "the repository files needed for this goal"
    checks = "\n".join("- " + g["name"] + ": " + json.dumps(g, ensure_ascii=False) for g in last["gates"])
    gaps = "\n".join(f"- {g['title']}: {g['detail']}" for g in state.get("best_gaps", [])) or "- None recorded"
    return "\n\n".join([
        f"Project goal\n{run.spec['objective']}", "Criteria that define done\n" + criteria_lines(run),
        "Accepted milestones in this checkout\n" + ("\n".join(lineage(run, state)) or "None yet"),
        f"Plateau rescue\nProgress on this goal stalled: the last {ex['stale']} independent judgments did not beat the best build, which scored "
        f"{ex['best_score']}/100. You start from that build with one attempt, and a judge will keep your result only if it beats it.\n"
        f"Gaps in the best build, most important first\n{gaps}",
        f"Diagnosis of why progress stalled\n{ex['diagnosis']}",
        f"Milestone {number}/{number}: Rescue: {strategy['name']}\nStrategy {strategy['name']}: {strategy['approach']}\n"
        "Commit to this strategy; do not hedge toward the approach that already stalled.",
        f"Work in this supplied Git worktree. Keep edits within {paths}. Protected paths: {', '.join(run.spec.get('protected_paths', [])) or 'none declared'}. "
        "Do not commit, push, merge, edit verification rules to hide failures, or touch another worktree. Return a concise summary and stop. "
        f"The controller independently runs these exact checks and decides whether to keep your result.\n{checks}",
        context.brief(run.store, run.id),
        f"Budget remaining before this call: {budget_status(run.spec, state)['remaining']}\n"
        f"Operator updates\n{run.store.feedback_text(run.id) or 'None'}"])


def gate(run, state, step, path, snapshot, out, index, number, check):
    return run_check(run, state, step, path, snapshot, out, number, check, {"step": state["step"] + 1, "attempt": 0, "rescue": index})


def build_strategy(run, state, ex):
    index, step = len(ex["builds"]) + 1, run.spec["milestones"][-1]
    strategy, config, out = ex["strategies"][index - 1], worker_for(run, index), folder(run, ex, index)
    current = ex.get("current") if (ex.get("current") or {}).get("index") == index else None
    path = checkout(run, ex["best_commit"], f"rescue-{index:02d}", keep=current is not None, role="rescue")
    detail = {"index": index, "count": len(ex["strategies"]), "name": strategy["name"], "approach": strategy["approach"], "worker": describe(config),
              "evidence": str(out), "ref": None, "commit": None, "tree": None, "screenshots": [], "failed_checks": [], "checks": []}
    build_one(run, {**state, "accepted": ex["best_commit"]}, ex, current, config=config, path=path, out=out, step=step, detail=detail,
              prompt=build_prompt(run, state, ex, strategy), ref=strategy_ref(state, ex, index), gate_fn=gate, names=RESCUE)


# ---------------------------------------------------------------- stage 3: the incumbent against each strategy

def duel(ex, index):
    """(the strategy's points, the incumbent's points) over the calls between them; a win is 1, a tie .5 each, an unreadable call nothing."""
    blank = {"name": "", "failed_checks": []}
    sub = {"comparisons": [c for c in ex["comparisons"] if c["pair"] == sorted((0, index))]}
    points = {r["index"]: r["score"] for r in rank(sub, [{**blank, "index": 0}, {**blank, "index": index}])[0]}
    return points[index], points[0]


def standing(ex, passed):
    """(a row per passing strategy, the qualifiers with the top score). A strategy qualifies by strictly out-scoring the incumbent."""
    rows = []
    for build in passed:
        won, lost = duel(ex, build["index"])
        rows.append({"index": build["index"], "name": build["name"], "score": won, "incumbent": lost, "qualified": won > lost})
    top = max((r["score"] for r in rows if r["qualified"]), default=None)
    return rows, sorted(r["index"] for r in rows if r["qualified"] and r["score"] == top)


def schedule(ex, passed):
    """The ordered judge calls (first, second): every passing strategy against the incumbent in both orders, then, once those are
    in, the tied leaders against each other. It depends only on calls already recorded, so a resumed rescue asks the same ones."""
    calls = [pair for b in passed for pair in ((0, b["index"]), (b["index"], 0))]
    if len(ex["comparisons"]) < len(calls):
        return calls
    leaders = standing(ex, passed)[1]
    return calls + [pair for a, b in combinations(leaders, 2) for pair in ((a, b), (b, a))]


def candidate_block(run, ex, label, build, images):
    strategy = ex["strategies"][build["index"] - 1] if build["index"] else {"name": INCUMBENT, "approach": "The best build so far, unchanged."}
    lines = [f'Candidate {label}: strategy "{strategy["name"]}"', f"Approach: {strategy['approach']}",
             f"Source folder: {run.root / 'worktrees' / ('rescue-%02d' % build['index'])}"]
    lines += ["Screenshots:", *(f"- {p}" for p in images)] if images else ["Screenshots: none were produced; judge from the source."]
    return "\n".join(lines)


def comparison_prompt(run, state, ex, labelled):
    images = {label: [str(p) for p in build["screenshots"][:JUDGE_IMAGES]] for label, build in labelled}
    blocks = [candidate_block(run, ex, label, build, images[label]) for label, build in labelled]
    gaps = titles(state.get("best_gaps", []), 5)
    return compose(run, state, ex, [label for label, _ in labelled], blocks, images, False,
                   f"Context\nProgress on this goal stalled, so alternative strategies were built from the best build so far. Open gaps in that build: {gaps}.")


def select(run, state, ex):
    passed = [b for b in ex["builds"] if b["passed"]]
    calls = schedule(ex, passed) if passed else []
    number = len(ex["comparisons"])
    if number >= len(calls):
        return conclude(run, state, ex, passed)
    first, second = calls[number]
    path = incumbent(run, ex)
    by_index = {0: {"index": 0, "name": INCUMBENT, "screenshots": best_images(run, ex)}, **{b["index"]: b for b in passed}}
    labelled = [("A", by_index[first]), ("B", by_index[second])]
    prompt, images = comparison_prompt(run, state, ex, labelled)
    workers = [run.spec["milestones"][-1]["worker"] if b["index"] == 0 else worker_for(run, b["index"]) for _, b in labelled]
    ask(run, state, ex, labelled, prompt, images, single=False, total=len(calls), workers=workers, names=RESCUE, where=folder(run, ex),
        extra={"stage": "incumbent" if 0 in (first, second) else "tiebreak"})


def decide(ex, passed):
    """(winner index or None, a row per passing strategy, the qualifiers with the top score).

    Qualifiers with the same score meet head to head; whatever the judge cannot separate falls to the lower index."""
    rows, leaders = standing(ex, passed)
    if len(leaders) > 1:
        heads = {"comparisons": [c for c in ex["comparisons"] if 0 not in c["pair"]]}
        return rank(heads, [b for b in passed if b["index"] in leaders])[0][0]["index"], rows, leaders
    return (leaders[0] if leaders else None), rows, leaders


def conclude(run, state, ex, passed):
    cleanup(run, "rescue")
    if not passed:
        return stop(run, state, ex, [("rescue_failed", {"wave": ex["wave"], "reason": "No strategy passed its checks",
                                                        "strategies": [{"index": b["index"], "name": b["name"], "reason": b["reason"]} for b in ex["builds"]]})])
    winner, rows, leaders = decide(ex, passed)
    valid = [c for c in ex["comparisons"] if not c.get("invalid")]
    bias = rank({"comparisons": ex["comparisons"]}, [{"index": 0, "name": INCUMBENT, "failed_checks": []}, *passed])[1]
    chosen = {"wave": ex["wave"], "winner": winner, "scores": rows, "tied": leaders if len(leaders) > 1 else [], "best_score": ex["best_score"],
              "position_bias_pairs": bias, "comparisons": len(valid), "invalid_comparisons": len(ex["comparisons"]) - len(valid),
              "strategies": len(ex["builds"]), "passed": len(passed)}
    if winner is None:
        return stop(run, state, ex, [("rescue_chosen", chosen), ("rescue_failed", {"wave": ex["wave"], "reason": "No strategy beat the best build",
                                                                                     "scores": rows})])
    build = next(b for b in passed if b["index"] == winner)
    run.set("rescue_chosen", {**chosen, "name": build["name"], "commit": build["commit"], "ref": strategy_ref(state, ex, winner)},
            rescue={**ex, "stage": "promote", "winner": winner}, last_error="", retry_at=None, judge_route=0, judge_attempts=0, transient_failures=0)


def stop(run, state, ex, events):
    """End the rescue without a winner. The pursuit stops as a plateau, exactly as it would have without a rescue."""
    cleanup(run, "rescue")
    message, stopped = stopping("plateau", ex["wave"], state.get("best_score"), state.get("best_commit"), state.get("best_gaps", []))
    (kind, details), rest = events[0], events[1:]
    run.set(kind, details, following_event=[*rest, stopped], phase="blocked", blocked_from="judging", last_error=message,
            rescues_used=state.get("rescues_used", 0) + 1, rescue={**ex, "stage": "done", "winner": None}, retry_at=None,
            planner_route=0, judge_route=0, judge_attempts=0, transient_failures=0)


# ---------------------------------------------------------------- stage 4: promote the winner as the next milestone

def promote(run, state, ex):
    build = next(b for b in ex["builds"] if b["index"] == ex["winner"])
    strategy, last = ex["strategies"][build["index"] - 1], run.spec["milestones"][-1]
    stage_manifest(run, state, build, "strategy")
    title = f"Rescue: {strategy['name']}"
    milestone = {"title": title, "gates": last["gates"], "paths": last["paths"], "worker": last["worker"], "worker_fallbacks": last["worker_fallbacks"],
                 "task": f"{strategy['approach']}\n\nWhy progress stalled: {ex['diagnosis']}"[:12000]}
    done = {"stage": "done", "wave": ex["wave"], "step": ex["step"], "winner": build["index"], "name": strategy["name"], "evidence": build["evidence"],
            "checks_sha256": build["checks_sha256"]}
    # Run.promote verifies the manifest staged above and moves the result branch from the best build with a compare-and-swap.
    run.store.extend_plan(run.id, [milestone], {"wave": ex["wave"] + 1, "titles": [title], "planner": ex["diagnoser"], "rescue": True,
                                                  "strategy": strategy["name"]},
                          phase="promoting", wave=ex["wave"] + 1, candidate=build["commit"], candidate_tree=build["tree"],
                          checks_sha256=build["checks_sha256"], attempt=1, attempts_used=0, worker_feedback_version=build["feedback"],
                          worker_session=None, worker_route=0, reviewer_route=0, judge_route=0, judge_attempts=0, last_error="", retry_at=None,
                          log=None, plan_attempts=0, planner_route=0, failure_count=0, failure_fingerprint=None, route_rejections=0,
                          transient_failures=0, verification_batch=None, pursuit_conflicts=[], stale_judgments=0,
                          rescues_used=state.get("rescues_used", 0) + 1, rescue=done)
    run.spec = run.store.get(run.id)["spec"]


# ---------------------------------------------------------------- reports

def collect(events):
    """Every rescue in a run's events, in order, with its strategies, scores, outcome and the score of the judgment that followed."""
    rescues, current = [], None
    for event in events:
        d, kind = event["details"], event["kind"]
        if kind == "rescue_started":
            current = {"wave": d["wave"], "best_score": d["best_score"], "stale_judgments": d["stale_judgments"], "status": "in_progress",
                       "diagnosis": None, "diagnoser": None, "reason": None, "winner": None, "winner_name": None, "judge_calls": 0,
                       "invalid_comparisons": 0, "position_bias_pairs": [], "next_score": None, "rows": {}}
            rescues.append(current)
        elif current is None:
            continue
        elif kind == "rescue_diagnosed":
            current.update(diagnosis=d["diagnosis"], diagnoser=d["diagnoser"])
            for index, item in enumerate(d["strategies"], 1):
                current["rows"][index] = {"index": index, "name": item["name"], "approach": item["approach"], "worker": None, "passed": None,
                                          "reason": None, "seconds": None, "commit": None, "score": None, "qualified": None, "winner": False}
        elif kind in ("rescue_candidate_built", "rescue_candidate_failed"):
            row = current["rows"].setdefault(d["index"], {"index": d["index"], "name": d["name"], "approach": d.get("approach"), "score": None,
                                                           "qualified": None, "winner": False})
            row.update(worker=d["worker"], passed=d["passed"], reason=d.get("reason"), seconds=d.get("seconds"), commit=d.get("commit"))
        elif kind == "rescue_compared":
            current["judge_calls"] += 1
            current["invalid_comparisons"] += bool(d.get("invalid"))
        elif kind == "rescue_chosen":
            current["position_bias_pairs"] = d["position_bias_pairs"]
            for item in d["scores"]:
                if item["index"] in current["rows"]:
                    current["rows"][item["index"]].update(score=item["score"], qualified=item["qualified"])
            if d["winner"] is not None:
                current.update(status="chosen", winner=d["winner"], winner_name=d["name"])
                current["rows"][d["winner"]]["winner"] = True
        elif kind in ("rescue_failed", "rescue_skipped"):
            current.update(status="failed" if kind == "rescue_failed" else "skipped", reason=d.get("reason"))
        elif kind == "goal_judged" and current["status"] == "chosen" and current["next_score"] is None and d["wave"] == current["wave"] + 1:
            current["next_score"] = d["score"]
    for item in rescues:
        item["strategies"] = [item["rows"][i] for i in sorted(item["rows"])]
        del item["rows"]
    return rescues


def report(store, run_id):
    """What each rescue did, read from the run's events; [] when the run never rescued."""
    return collect(store.events(run_id, limit=None, kinds=[*EVENTS, "goal_judged"]))


def summary(events):
    """The measurement of whether rescues pay off, for a run's metrics: None when it never rescued."""
    rounds = collect(events)
    if not rounds:
        return None
    last = rounds[-1]
    return {"attempted": len(rounds), "won": sum(r["status"] == "chosen" for r in rounds), "failed": sum(r["status"] == "failed" for r in rounds),
            "skipped": sum(r["status"] == "skipped" for r in rounds), "outcome": OUTCOMES.get(last["status"], last["status"]), "winner": last["winner_name"],
            "score_before": last["best_score"], "score_after": last["next_score"],
            "rescues": [{"wave": r["wave"], "outcome": OUTCOMES.get(r["status"], r["status"]), "winner": r["winner_name"], "score_before": r["best_score"],
                         "score_after": r["next_score"]} for r in rounds]}


def format_lines(data):
    """Plain-text lines for one rescue in the pursuit report."""
    names = {"chosen": f"strategy {data['winner_name']} chosen", "failed": "no strategy beat the best build" + (f" ({data['reason']})" if data["reason"] else ""),
             "skipped": "skipped: " + str(data["reason"] or ""), "in_progress": "in progress"}
    lines = [f"  Plateau rescue at best {data['best_score']}/100 - {names[data['status']]} ({data['judge_calls']} judge calls)"]
    if data["diagnosis"]:
        lines.append("    Diagnosis: " + " ".join(data["diagnosis"].split())[:400])
    for row in data["strategies"]:
        first = str(row["reason"] or "").splitlines()
        state = ("not built yet" if row["passed"] is None else "passed" if row["passed"] else "failed" + (": " + first[0][:120] if first else ""))
        lines.append(f"    {row['index']}. {row['name']} [{(row['worker'] or {}).get('kind', '?')}] {state}"
                     + (f", score {row['score']:g}" if row["score"] is not None else "") + (" - WINNER" if row["winner"] else ""))
    if data["position_bias_pairs"]:
        lines.append("    Position bias in pairs: " + ", ".join(f"{'best build' if a == 0 else a} vs {b}" for a, b in data["position_bias_pairs"]))
    if data["next_score"] is not None:
        lines.append(f"    The next judgment scored {data['next_score']}/100 (best before the rescue {data['best_score']}/100)")
    return lines
