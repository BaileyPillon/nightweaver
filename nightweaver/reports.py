"""Complete-run measurements and portable reports; never infer unreported billing."""

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path

from .safety import atomic_write, redact
from .insights import provider_totals
from .store import current_stop
from .checks import observed_pass


def recovery_metrics(events):
    attempts = rejections = first_pass = repaired = 0
    measured_gates = []
    observed = {}
    stability = {}
    for event in events:
        kind, detail = event["kind"], event["details"]
        if kind == "worker_started":
            attempts += 1
        elif kind == "attempt_rejected":
            rejections += 1
        elif kind == "milestone_accepted":
            first_pass += int(attempts == 1 and rejections == 0)
            repaired += int(attempts > 0 and rejections > 0)
            attempts = rejections = 0
        elif kind == "gate_finished":
            if "duration_ms" in detail:
                measured_gates.append({"name": detail["name"], "duration_ms": detail["duration_ms"]})
            passed = observed_pass(detail)
            if passed is not None and detail.get("tree") and detail.get("contract_sha256"):
                key = detail["name"], detail["tree"], detail["contract_sha256"]
                observed.setdefault(key, set()).add(passed)
                if detail.get("verification_batch") and all(type(detail.get(key)) is int for key in ("gate_index", "stability_runs", "sample")):
                    group = (detail["verification_batch"], detail["gate_index"], detail["stability_runs"])
                    stability.setdefault(group, {})[detail["sample"]] = passed
    confirmations = recovered = 0
    for (_, _, count), samples in stability.items():
        if count + 1 in samples:
            confirmations += 1
            recovered += all(samples.get(i) is True for i in range(count + 1, 2 * count + 1))
    return {"first_pass_milestones": first_pass, "repaired_milestones": repaired,
            "timed_gate_executions": len(measured_gates), "gate_confirmation_rounds": confirmations,
            "gate_confirmation_recoveries": recovered,
            "slowest_gate": max(measured_gates, key=lambda g: g["duration_ms"], default=None),
            "mixed_gate_results": [{"name": name, "tree": tree, "contract_sha256": digest}
                                   for (name, tree, digest), outcomes in observed.items() if len(outcomes) == 2]}


def metrics(store, run_id):
    with store.snapshot() as reader:
        return _metrics(reader, run_id)


def _metrics(store, run_id):
    row = store.get(run_id)
    events = store.events(run_id, limit=None)
    kinds = [event["kind"] for event in events]
    calls = [event["details"] for event in events if event["kind"] == "provider_call_finished"]
    started = next((event for event in events if event["kind"] == "run_started"), None)
    start = started["time"] if started else None
    stop = next((event["time"] for event in reversed(events) if event["kind"] == "run_completed"), None)
    if not stop and row["state"]["phase"] == "done":
        stop = next((event["time"] for event in reversed(events) if event["kind"] == "milestone_accepted"), None)
    terminal = None
    if row["state"]["phase"] == "blocked":
        terminal = next((event for event in reversed(events) if event["kind"] in (
            "blocked", "budget_exhausted", "provider_unavailable", "dispatcher_run_failed",
            "repeated_failure_stopped", "verification_inconsistent", "pursuit_stopped")), None)
        if terminal:
            stop = terminal["time"]
    seconds = None
    if start and (stop or row["state"]["phase"] not in ("blocked", "done")):
        seconds = max(0, ((datetime.fromisoformat(stop) if stop else datetime.now(timezone.utc)) - datetime.fromisoformat(start)).total_seconds())
    queued = next((e["time"] for e in events if e["kind"] == "queued"), None)
    first_acceptance = next((e["time"] for e in events if e["kind"] == "milestone_accepted"), None)

    def interval(begin, end):
        if begin and end:
            return round(max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(begin)).total_seconds()), 3)
        return None
    unknown = sum(call.get("observed_tokens") is None for call in calls)
    recorded = sum(call.get("observed_tokens") or 0 for call in calls)
    interventions = sum(event["kind"] in ("operator_feedback", "blocked_run_retried") and started is not None
                        and event["seq"] > started["seq"] for event in events)
    phase = row["state"]["phase"]
    rejection = next((e["details"] for e in reversed(events) if e["kind"] == "attempt_rejected"), {})
    outcome = "accepted" if phase == "done" else "unfinished"
    if phase == "blocked":
        if terminal and terminal["kind"] == "verification_inconsistent":
            outcome = "verification_inconsistent"
        elif terminal and terminal["kind"] == "provider_unavailable":
            outcome = "provider_blocked"
        elif terminal and terminal["kind"] == "pursuit_stopped":
            outcome = "pursuit_stopped"
        elif terminal and terminal["kind"] == "budget_exhausted":
            outcome = "budget_exhausted"
            recovered = next((event["seq"] for event in reversed(events) if event["kind"] == "interrupted_worker_recovered"), 0)
            last_call = next((event["seq"] for event in reversed(events) if event["kind"] == "provider_call_started"), 0)
            if row["spec"].get("budgets", {}).get("max_calls") == 1 and recovered > last_call:
                outcome = "interrupted"
        elif terminal and (terminal["kind"] == "repeated_failure_stopped" or
                           (terminal["kind"] == "blocked" and "during" not in terminal["details"])):
            outcome = "rejected"
            rejected = next((e["details"] for e in reversed(events) if e["kind"] == "attempt_rejected"), {})
            if rejected.get("phase") == "worker":
                outcome = {124: "worker_timeout", 125: "worker_idle_timeout", 126: "worker_output_limit"}.get(
                    rejected.get("exit"), outcome)
        else:
            outcome = "execution_blocked"
    current = store.accepted_milestones(run_id)
    discarded = len(store.accepted_milestones(run_id, discarded=True)) - len(current)  # a revert's milestones are neither accepted nor part of the plan
    pursuit = None
    if row["spec"].get("pursue"):
        judged = row["state"].get("judgments", [])
        stopped = current_stop(events)  # a stop that an extension, a retry or a later block has superseded is not the run's stop
        pursuit = {"waves": row["state"].get("wave", 1), "judgments": len(judged), "last_score": judged[-1]["score"] if judged else None,
                   "best_score": row["state"].get("best_score"), "goal_met": "goal_met" in kinds, "reverts": kinds.count("pursuit_reverted"),
                   "stop_reason": stopped["reason"] if stopped and phase == "blocked" else None,
                   "confirmations": kinds.count("goal_confirming"), "met_unconfirmed": kinds.count("met_unconfirmed"),
                   "confirmations_skipped": kinds.count("confirmation_skipped"), "discarded_milestones": discarded}
    exploration = None
    if {"direction_planned", "exploration_skipped"} & set(kinds):
        chosen = next((e["details"] for e in reversed(events) if e["kind"] == "direction_chosen"), None)
        exploration = {"directions": kinds.count("direction_planned"), "built": kinds.count("direction_built"),
                       "failed": kinds.count("direction_failed"), "comparisons": kinds.count("direction_compared"),
                       "winner": chosen["winner"] if chosen else None, "chosen_by": chosen["chosen_by"] if chosen else None,
                       "position_bias_pairs": len(chosen["position_bias_pairs"]) if chosen else 0,
                       "outcome": "chosen" if chosen else "failed" if "exploration_failed" in kinds else
                                  "skipped" if "exploration_skipped" in kinds else "in progress"}
    from .rescue import summary as rescue_summary
    rescue = rescue_summary(events) if {"rescue_started", "rescue_skipped"} & set(kinds) else None
    return {
        **recovery_metrics(events),
        **({"pursuit": pursuit} if pursuit else {}),
        **({"exploration": exploration} if exploration else {}),
        **({"rescue": rescue} if rescue else {}),
        "provider_usage": provider_totals(events),
        "run_id": run_id, "title": row["spec"]["title"], "phase": row["state"]["phase"],
        "outcome": outcome, "block_reason": row["state"].get("last_error") if phase == "blocked" else None,
        "last_rejection_stage": rejection.get("phase"),
        "accepted": row["state"]["phase"] == "done", "baseline": row["state"]["base"],
        "accepted_milestones": len(current), "total_milestones": len(row["spec"]["milestones"]) - discarded,
        "elapsed_seconds": round(seconds, 3) if seconds is not None else None,
        "queue_seconds": interval(queued, start), "seconds_to_first_acceptance": interval(start, first_acceptance),
        "provider_calls": kinds.count("provider_call_started"), "worker_attempts": kinds.count("worker_started"),
        "failed_attempts": kinds.count("attempt_rejected"), "gate_failures": sum(
            event["kind"] == "gate_finished" and observed_pass(event["details"]) is False for event in events),
        "gate_unscored": sum(event["kind"] == "gate_finished" and observed_pass(event["details"]) is None for event in events),
        "interventions": interventions, "unassisted_acceptance": row["state"]["phase"] == "done" and interventions == 0,
        "interruptions_recovered": kinds.count("interrupted_worker_recovered"),
        "supervised_restarts": kinds.count("guard_restart_scheduled"),
        "unobserved_runner_exits": kinds.count("guard_unobserved_exit"),
        "unexpected_runner_exits": sum(e["kind"] == "guard_child_exited" and e["details"].get("unexpected") is True for e in events),
        "fallbacks": kinds.count("provider_fallback"), "observed_tokens": recorded if calls and unknown < len(calls) else None,
        "repair_handoffs": kinds.count("repair_handoff"),
        "accepted_after_handoff": len({e["details"]["step"] for e in events if e["kind"] == "repair_handoff"} &
                                      {e["details"]["step"] for e in events if e["kind"] == "milestone_accepted" and "step" in e["details"]}),
        "usage_unknown_calls": unknown, "usage_pending_calls": kinds.count("provider_call_started") - len(calls),
        "billing": "not_measured", "event_count": len(events),
    }


STYLE = """
:root{color-scheme:dark;--bg:#161a19;--panel:#1d2421;--ink:#ecf1eb;--muted:#b2c1b9;--line:#46554e;--accent:#d4edad}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 'Segoe UI',sans-serif}
main{max-width:1120px;margin:auto;padding:48px 32px}header{border-bottom:1px solid var(--line);padding-bottom:32px}
h1{font-size:clamp(2rem,5vw,3.2rem);line-height:1.13;max-width:25ch;margin:0 0 20px;font-weight:600;overflow-wrap:anywhere}
h2{font-size:1.45rem;margin:40px 0 16px}p{max-width:75ch;white-space:pre-wrap;overflow-wrap:anywhere}
a{color:var(--accent);text-underline-offset:4px}a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
::selection{background:var(--accent);color:var(--bg)}.muted,footer{color:var(--muted)}.state{color:var(--accent);font-weight:600}
.table{overflow:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:12px 16px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
td:first-child{padding-left:0}details{border-bottom:1px solid var(--line);padding:14px 0}summary{cursor:pointer;overflow-wrap:anywhere}time{color:var(--muted);font-size:.85rem;margin-right:16px;white-space:nowrap}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel);padding:18px;border-radius:6px;font:13px/1.6 Consolas,monospace}
figure{margin:24px 0}img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px}figcaption{color:var(--muted);margin-top:8px}
footer{border-top:1px solid var(--line);margin-top:40px;padding-top:20px;font-size:.85rem}@media(max-width:600px){main{padding:28px 18px}th,td{padding:10px 8px}time{display:block}}
@media print{body{background:white;color:black}details{break-inside:avoid}pre{background:#eee;color:black}}
"""


def document(title, body):
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>{escape(title)}</title><style>{STYLE}</style><main>{body}<footer>Nightweaver · Local evidence report. Usage is reported telemetry, not a bill. Browser checks do not grade aesthetics.</footer></main></html>'


def run_report(store, run_id, output, include_images=False):
    with store.snapshot() as reader:
        return _run_report(reader, run_id, output, include_images)


def _run_report(store, run_id, output, include_images):
    from .report_activity import activity, checkpoint_header
    row = store.get(run_id)
    measured = redact(metrics(store, run_id))
    events = redact(store.events(run_id, limit=None))
    title = redact(row["spec"]["title"])
    body = f'<header><h1>{escape(title)}</h1><p class="state">{escape(measured["phase"].capitalize())} · {measured["accepted_milestones"]}/{measured["total_milestones"]} milestones accepted</p><p>{escape(redact(row["spec"]["objective"]))}</p></header>'
    body += checkpoint_header(row, events)
    body += '<h2 id="measurements">Run measurements</h2><div class="table"><table><tbody>'
    for key, value in measured.items():
        if key == "slowest_gate" and value:
            value = f"{value['name']} ({value['duration_ms']:,} ms)"
        elif key == "mixed_gate_results":
            value = ", ".join(f"{g['name']} at {g['tree'][:12]}" for g in value) or "None observed"
        elif key == "pursuit":
            value = (f"{value['waves']} waves, {value['judgments']} judgments, last {value['last_score']}, best {value['best_score']}, "
                     f"{'goal met' if value['goal_met'] else 'stopped: ' + value['stop_reason'] if value['stop_reason'] else 'in progress'}, "
                     f"{value['reverts']} reverts"
                     + (f", {value['confirmations']} confirmations ({value['met_unconfirmed']} not confirmed)" if value["confirmations"] else "")
                     + (f", {value['confirmations_skipped']} without a confirmation (budget)" if value["confirmations_skipped"] else "")
                     + (f", {value['discarded_milestones']} milestones discarded" if value["discarded_milestones"] else ""))
        elif key == "exploration":
            value = (f"{value['outcome']}: {value['built']} of {value['directions']} directions passed their checks, "
                     f"{value['comparisons']} judge comparisons"
                     + (f", direction {value['winner']} chosen by {value['chosen_by']}" if value["winner"] else "")
                     + (f", position bias in {value['position_bias_pairs']} pairs" if value["position_bias_pairs"] else ""))
        elif key == "rescue":
            value = (f"{value['attempted']} attempted, {value['won']} won, {value['failed']} failed, {value['skipped']} skipped; latest {value['outcome']}"
                     + (f", strategy {value['winner']}" if value["winner"] else "") + f", best score before {value['score_before']}"
                     + (f", next judgment {value['score_after']}" if value["score_after"] is not None else ""))
        elif key == "provider_usage":
            value = " · ".join(f"{r['role']} / {r['provider']} ({r['model'] or 'default'}), "
                               f"{r['measured_calls']}/{r['calls']} calls with usage" for r in value) or "No finished calls"
        body += f'<tr><th scope="row">{escape(key.replace("_", " ").capitalize())}</th><td>{escape(str(value) if value is not None else "Unavailable")}</td></tr>'
    body += '</tbody></table></div>'
    if row["spec"].get("pursue"):
        from .pursuit import report as pursuit_report
        data = pursuit_report(store, run_id)
        body += ('<h2>Goal pursuit</h2><div class="table"><table><thead><tr><th>Wave</th><th>Milestones</th><th>Judgment</th>'
                 '<th>Top gaps</th></tr></thead><tbody>')
        for wave in data["waves"]:
            verdict = wave["judgment"]
            summary = "Not judged" if verdict is None else f'{verdict["verdict"]} {verdict["score"]}/100'
            if verdict and verdict.get("confirmation"):
                sure = verdict["confirmation"]
                summary += (f" · confirmed ({sure['score']}/100)" if sure["confirmed"] else f" · MET not confirmed ({sure['verdict']} {sure['score']}/100)")
            if verdict and verdict.get("confirmation_skipped"):
                summary += " · MET stood without a confirmation (budget)"
            if wave["reverted"]:
                summary += " · reverted to the best build"
            gaps = "; ".join(g["title"] for g in verdict["gaps"][:3]) if verdict else ""
            body += (f'<tr><td>{wave["wave"]}</td><td>{escape("; ".join(m["title"] + (" (discarded)" if m["discarded"] else "") for m in wave["milestones"]))}</td>'
                     f'<td>{escape(summary)}</td><td>{escape(gaps)}</td></tr>')
        body += '</tbody></table></div>'
        if data.get("exploration"):
            ex = data["exploration"]
            body += (f'<h2>Design exploration</h2><p>{escape(ex["status"].replace("_", " ").capitalize())}'
                     + (f' · winner chosen by {escape(str(ex["chosen_by"]))}' if ex["chosen_by"] else "")
                     + f' · {ex["judge_calls"]} judge calls'
                     + (' · position bias in pairs ' + escape(", ".join(f"{a} vs {b}" for a, b in ex["position_bias_pairs"])) if ex["position_bias_pairs"] else "")
                     + (f' · {escape(str(ex["reason"]))}' if ex["reason"] else "")
                     + '</p><div class="table"><table><thead><tr><th>Direction</th><th>Worker</th><th>Checks</th><th>Score</th><th>Result</th>'
                       '</tr></thead><tbody>')
            for row in ex["directions"]:
                worker = row["worker"] or {}
                checks = "Not built yet" if row["passed"] is None else "Passed" if row["passed"] else "Failed: " + str(row["reason"] or "")[:160]
                body += (f'<tr><td>{row["index"]}. {escape(row["name"])}</td><td>{escape(str(worker.get("kind", "")))}'
                         f'{" / " + escape(str(worker["model"])) if worker.get("model") else ""}</td><td>{escape(checks)}</td>'
                         f'<td>{"" if row["score"] is None else format(row["score"], "g")}</td><td>{"Winner" if row["winner"] else ""}</td></tr>')
            body += '</tbody></table></div>'
        for wave, rescue in ((w, r) for w in data["waves"] for r in w.get("rescues", [])):
            status = {"chosen": f"strategy {rescue['winner_name']} chosen", "failed": "no strategy beat the best build", "skipped": "skipped",
                      "in_progress": "in progress"}[rescue["status"]]
            again = f" (rescue {rescue['n']})" if rescue["n"] > 1 else ""
            body += (f'<h2>Plateau rescue after wave {wave["wave"]}{again}</h2><p>{escape(status[0].upper() + status[1:])} · best score before {rescue["best_score"]}/100'
                     + (f' · next judgment {rescue["next_score"]}/100' if rescue["next_score"] is not None else "")
                     + f' · {rescue["judge_calls"]} judge calls'
                     + (' · position bias in pairs ' + escape(", ".join(f"{'best build' if a == 0 else a} vs {b}" for a, b in rescue["position_bias_pairs"]))
                        if rescue["position_bias_pairs"] else "")
                     + (f' · {escape(str(rescue["reason"]))}' if rescue["reason"] and rescue["status"] == "skipped" else "") + '</p>'
                     + (f'<p>{escape(rescue["diagnosis"])}</p>' if rescue["diagnosis"] else "")
                     + '<div class="table"><table><thead><tr><th>Strategy</th><th>Worker</th><th>Checks</th><th>Score</th><th>Result</th></tr></thead><tbody>')
            for item in rescue["strategies"]:
                worker = item["worker"] or {}
                checks = "Not built yet" if item["passed"] is None else "Passed" if item["passed"] else "Failed: " + str(item["reason"] or "")[:160]
                read = item["readable"]
                result = "Winner" if item["winner"] else f"Judge read {read} of 2 orders" if read is not None and read < 2 else ""
                body += (f'<tr><td>{item["index"]}. {escape(item["name"])}</td><td>{escape(str(worker.get("kind", "")))}'
                         f'{" / " + escape(str(worker["model"])) if worker.get("model") else ""}</td><td>{escape(checks)}</td>'
                         f'<td>{"" if item["score"] is None else format(item["score"], "g")}</td><td>{result}</td></tr>')
            body += '</tbody></table></div>'
    from .junit import history
    observations = history(store, run_id)["observations"]
    if observations:
        body += '<h2>Test results by attempt</h2><div class="table"><table><thead><tr><th>Milestone / attempt / sample</th><th>Check</th><th>Verdict</th><th>Passed / total</th><th>Failures / errors / skips</th></tr></thead><tbody>'
        for obs in observations:
            counts = (obs["report"] or {}).get("counts")
            verdict = "Unscored" if obs["passed"] is None else "Passed" if obs["passed"] else "Failed"
            coverage = f'{counts["passed"]} / {counts["tests"]}' if counts else "Unavailable"
            outcomes = f'{counts["failed"]} / {counts["errors"]} / {counts["skipped"]}' if counts else "Unavailable"
            body += (f'<tr><td>{obs["step"]} / {obs["attempt"]} / {obs["sample"]}</td>'
                     f'<td>{escape(obs["name"])}</td><td>{verdict}</td><td>{coverage}</td><td>{outcomes}</td></tr>')
        body += '</tbody></table></div>'
        proofs = [o for o in observations if o.get("proof")]
        if proofs:
            body += '<h2>Regression proof</h2><div class="table"><table><thead><tr><th>Milestone / attempt</th><th>Check</th><th>Baseline commit</th><th>Baseline samples matched</th><th>Joint verdict</th></tr></thead><tbody>'
            for obs in proofs:
                base = obs["proof"]["baseline"]
                samples = base["samples"]
                verdict = (
                    "Unscored"
                    if obs["passed"] is None
                    else "Passed"
                    if obs["passed"]
                    else "Failed"
                )
                body += (
                    f"<tr><td>{obs['step']} / {obs['attempt']}</td><td>{escape(obs['name'])}</td>"
                    f"<td>{escape(base['commit'][:12])}</td><td>{sum(s['passed'] for s in samples)} / {len(samples)}</td><td>{verdict}</td></tr>"
                )
            body += "</tbody></table></div>"
    from .contracts import catalog_history
    catalog_rows = catalog_history(store, run_id)["observations"]
    if catalog_rows:
        body += '<h2>Catalog changes by attempt</h2><div class="table"><table><thead><tr><th>Milestone / attempt</th><th>Check</th><th>Verdict</th><th>Added / removed / modified</th><th>Findings</th></tr></thead><tbody>'
        for obs in catalog_rows:
            delta = " / ".join(str(obs[k]) if obs[k] is not None else "?" for k in ("added_ids", "removed_ids", "changed_records"))
            verdict = "Unscored" if obs["passed"] is None else "Passed" if obs["passed"] else "Failed"
            findings = obs["error"] or ", ".join(f"{k} ({v})" for k,v in obs["violations"].items()) or "None recorded"
            body += (f"<tr><td>{obs['step']} / {obs['attempt']}</td><td>{escape(redact(obs['gate']))}</td>"
                     f"<td>{verdict}</td><td>{delta}</td><td>{escape(redact(findings))}</td></tr>")
        body += '</tbody></table></div>'
    if include_images:
        body += '<h2>Verified screenshots</h2>'
        seen, total = set(), 0
        unavailable = Counter()
        evidence_root = (store.directory(run_id) / "evidence").resolve()
        for event in events:
            if event["kind"] not in ("gate_finished", "judge_gate_finished"):
                continue
            for artifact in event["details"].get("artifacts", []):
                if not isinstance(artifact, dict) or not str(artifact.get("path", "")).lower().endswith(".png"):
                    continue
                try:
                    path = Path(artifact["path"]).resolve(strict=True)
                    if not path.is_relative_to(evidence_root) or not path.is_file():
                        unavailable["outside the evidence folder"] += 1
                        continue
                    if (path, artifact.get("sha256")) in seen:
                        continue
                    budget = min(5 * 1024 * 1024, 20 * 1024 * 1024 - total)
                    if path.stat().st_size > budget:
                        unavailable["size limit"] += 1
                        continue
                    with path.open("rb") as stream:
                        data = stream.read(budget + 1)
                except (OSError, RuntimeError, ValueError):
                    unavailable["unreadable"] += 1
                    continue
                if len(data) > budget or not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
                    unavailable["content mismatch"] += 1
                    continue
                seen.add((path, artifact["sha256"]))
                total += len(data)
                caption = escape(f"{event['details'].get('name', 'Browser')} · {artifact.get('name', path.name)} · exit {event['details'].get('exit')}")
                body += f'<figure><img loading="lazy" alt="{caption}" src="data:image/png;base64,{base64.b64encode(data).decode()}"><figcaption>{caption}</figcaption></figure>'
        if not seen:
            body += '<p>No verified screenshots to display.</p>'
        if unavailable:
            body += '<p>Omitted screenshot receipts · ' + ', '.join(f'{count} {reason}' for reason, count in unavailable.items()) + '.</p>'
    body += activity(events)
    atomic_write(output, document(title, body))
    return {"run_id": run_id, "report": str(Path(output).resolve()), "metrics": measured}
