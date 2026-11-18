"""Complete-run measurements and portable reports; never infer unreported billing."""

import base64
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path

from .safety import atomic_write, redact


def recovery_metrics(events):
    attempts = rejections = first_pass = repaired = 0
    measured_gates = []
    observed = {}
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
            if detail.get("tree") and detail.get("contract_sha256"):
                key = detail["name"], detail["tree"], detail["contract_sha256"]
                passed = detail.get("exit") == 0 and not detail.get("artifact_error")
                observed.setdefault(key, set()).add(passed)
    return {"first_pass_milestones": first_pass, "repaired_milestones": repaired,
            "timed_gate_executions": len(measured_gates),
            "slowest_gate": max(measured_gates, key=lambda g: g["duration_ms"], default=None),
            "mixed_gate_results": [{"name": name, "tree": tree, "contract_sha256": digest}
                                   for (name, tree, digest), outcomes in observed.items() if len(outcomes) == 2]}


def metrics(store, run_id):
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
            "repeated_failure_stopped")), None)
        if terminal:
            stop = terminal["time"]
    seconds = None
    if start:
        seconds = max(0, ((datetime.fromisoformat(stop) if stop else datetime.now(timezone.utc)) - datetime.fromisoformat(start)).total_seconds())
    unknown = sum(call.get("observed_tokens") is None for call in calls)
    recorded = sum(call.get("observed_tokens") or 0 for call in calls)
    interventions = sum(event["kind"] in ("operator_feedback", "blocked_run_retried") and started is not None
                        and event["seq"] > started["seq"] for event in events)
    phase = row["state"]["phase"]
    outcome = "accepted" if phase == "done" else "unfinished"
    if phase == "blocked":
        if terminal and terminal["kind"] == "provider_unavailable":
            outcome = "provider_blocked"
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
    return {
        **recovery_metrics(events),
        "run_id": run_id, "title": row["spec"]["title"], "phase": row["state"]["phase"],
        "outcome": outcome, "block_reason": row["state"].get("last_error") if phase == "blocked" else None,
        "accepted": row["state"]["phase"] == "done", "baseline": row["state"]["base"],
        "accepted_milestones": len(store.accepted_milestones(run_id)), "total_milestones": len(row["spec"]["milestones"]),
        "elapsed_seconds": round(seconds, 3) if seconds is not None else None,
        "provider_calls": kinds.count("provider_call_started"), "worker_attempts": kinds.count("worker_started"),
        "failed_attempts": kinds.count("attempt_rejected"), "gate_failures": sum(
            event["kind"] == "gate_finished" and (event["details"].get("exit") != 0 or bool(event["details"].get("artifact_error"))) for event in events),
        "interventions": interventions, "unassisted_acceptance": row["state"]["phase"] == "done" and interventions == 0,
        "interruptions_recovered": kinds.count("interrupted_worker_recovered"),
        "fallbacks": kinds.count("provider_fallback"), "observed_tokens": recorded if calls and unknown < len(calls) else None,
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
    row = store.get(run_id)
    measured = redact(metrics(store, run_id))
    events = redact(store.events(run_id, limit=None))
    title = redact(row["spec"]["title"])
    body = f'<header><h1>{escape(title)}</h1><p class="state">{escape(measured["phase"].capitalize())} · {measured["accepted_milestones"]}/{measured["total_milestones"]} milestones accepted</p><p>{escape(redact(row["spec"]["objective"]))}</p></header>'
    body += '<h2>Run measurements</h2><div class="table"><table><tbody>'
    for key, value in measured.items():
        if key == "slowest_gate" and value:
            value = f"{value['name']} ({value['duration_ms']:,} ms)"
        elif key == "mixed_gate_results":
            value = ", ".join(f"{g['name']} at {g['tree'][:12]}" for g in value) or "None observed"
        body += f'<tr><th scope="row">{escape(key.replace("_", " ").capitalize())}</th><td>{escape(str(value) if value is not None else "Unavailable")}</td></tr>'
    body += '</tbody></table></div>'
    if include_images:
        body += '<h2>Verified screenshots</h2>'
        seen, total = set(), 0
        for event in events:
            for artifact in event["details"].get("artifacts", []):
                path = Path(artifact["path"]).resolve()
                if path in seen or path.suffix.lower() != ".png" or not path.is_relative_to(store.directory(run_id).resolve()):
                    continue
                seen.add(path)
                if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024 or total + path.stat().st_size > 20 * 1024 * 1024:
                    continue
                data = path.read_bytes()
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    continue
                total += len(data)
                caption = escape(f"{event['details'].get('name', 'Browser')} · {artifact['name']} · exit {event['details'].get('exit')}")
                body += f'<figure><img alt="{caption}" src="data:image/png;base64,{base64.b64encode(data).decode()}"><figcaption>{caption}</figcaption></figure>'
    body += '<h2>Complete activity</h2>'
    for event in events:
        body += f'<details><summary><time>{escape(event["time"])}</time>{escape(event["kind"].replace("_", " ").capitalize())}</summary><pre>{escape(json.dumps(event["details"], indent=2))}</pre></details>'
    atomic_write(output, document(title, body))
    return {"run_id": run_id, "report": str(Path(output).resolve()), "metrics": measured}
