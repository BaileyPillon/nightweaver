"""Offline navigation for a run's checkpoint and event evidence."""

import json
from html import escape

from .checks import observed_pass


def category(event):
    kind = event["kind"]
    detail = event["details"]
    if kind == "gate_finished":
        verdict = observed_pass(detail)
        return (
            "Passed checks"
            if verdict is True
            else "Failed checks"
            if verdict is False
            else "Unscored checks"
        )
    if kind.startswith("guard_") or kind in (
        "interrupted_worker_recovered",
        "repair_handoff",
        "provider_fallback",
    ):
        return "Recovery"
    if kind in (
        "blocked",
        "attempt_rejected",
        "provider_unavailable",
        "budget_exhausted",
        "repeated_failure_stopped",
        "verification_inconsistent",
    ):
        return "Problems"
    if kind.startswith("provider_call"):
        return "Model calls"
    return "Progress"


def checkpoint_header(row, events):
    state, steps = row["state"], row["spec"]["milestones"]
    accepted = state["step"]
    commit = escape(str(state["accepted"]))
    latest = next(
        (
            e
            for e in reversed(events)
            if category(e) in ("Failed checks", "Problems", "Unscored checks")
        ),
        None,
    )
    body = [
        (
            '<nav aria-label="Report sections"><a href="#checkpoint">Checkpoint</a>'
            '<a href="#measurements">Measurements</a><a href="#activity">Activity</a></nav>'
        ),
        '<section id="checkpoint"><h2>Saved checkpoint</h2><dl class="checkpoint">',
        f"<dt>Accepted commit</dt><dd><code>{commit}</code></dd>",
    ]
    if accepted < len(steps):
        body.append(
            f"<dt>Current milestone</dt><dd>{escape(steps[accepted]['title'])}</dd>"
        )
    if "guard_limit" in state:
        body.append(
            f"<dt>Crash restarts used</dt><dd>{int(state.get('guard_restarts', 0))} of {int(state['guard_limit'])}</dd>"
        )
    interruption = next(
        (
            e
            for e in reversed(events)
            if e["kind"] == "guard_unobserved_exit"
            or (e["kind"] == "guard_child_exited" and e["details"].get("unexpected"))
        ),
        None,
    )
    if interruption:
        d = interruption["details"]
        label = (
            f"Runner {d['pid']} exited with {d['exit']}"
            if "exit" in d
            else "Supervisor lost before exit was recorded"
        )
        seq = int(interruption["seq"])
        body.append(
            f'<dt>Last interruption</dt><dd><a href="#event-{seq}">{escape(label)} · #{seq}</a></dd>'
        )
    if latest:
        detail = latest["details"]
        label = (
            detail.get("issue")
            or detail.get("name")
            or latest["kind"].replace("_", " ")
        )
        seq = int(latest["seq"])
        body.append(
            f'<dt>Latest recorded problem</dt><dd><a href="#event-{seq}">{escape(str(label)[:300])} · #{seq}</a></dd>'
        )
    body.append("</dl></section>")
    return "".join(body)


STYLES = """
nav[aria-label="Report sections"]{display:flex;gap:1.5rem;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--line)}
nav a{padding:8px 0}.checkpoint{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 24px}
.checkpoint dt,.event-id{color:var(--muted)}.checkpoint dd{margin:0;overflow-wrap:anywhere}
.checkpoint code,.event-id{font-variant-numeric:tabular-nums}.activity-tools{display:flex;align-items:end;flex-wrap:wrap;gap:12px}
.activity-tools label{display:grid;gap:5px;font-size:.9rem}.activity-tools input,.activity-tools select,.activity-tools button{
font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:5px;min-height:44px;padding:6px 12px;max-width:100%}
.activity-tools button{cursor:pointer}.activity-tools button:hover{border-color:var(--accent)}
.activity-tools[hidden]{display:none}
.activity-tools :focus-visible{outline:2px solid var(--accent);outline-offset:3px}
details:target{outline:1px solid var(--accent);outline-offset:3px}.event-id{font-size:.85rem;margin-left:1em}
.event-tag{font-size:.85rem;margin-inline-start:1em;color:var(--accent)}
[data-category="Failed checks"] .event-tag,[data-category="Problems"] .event-tag{color:#ffc4a7}
@media(max-width:600px){.checkpoint{display:block}.checkpoint dd{margin-bottom:12px}.activity-tools>*{width:100%}nav[aria-label="Report sections"]{gap:12px}.event-tag{display:block;margin:0}}
@media print{.activity-tools,nav,[role=status]{display:none!important}.event-tag,.event-id,.checkpoint dt{color:#333}details[hidden]{display:block}}
"""


SCRIPT = """
const list=document.getElementById('activity-list');
const events=Array.from(list.children);
const field=document.getElementById('event-search'), group=document.getElementById('event-group');
const count=document.getElementById('match-count');
function filterEvents(){
  const q=field.value.trim().toLowerCase();
  let visible=0;
  for(const event of events){
    event.hidden=(group.value!=='' && group.value!==event.dataset.category) ||
      (q!=='' && !event.textContent.toLowerCase().includes(q));
    if(!event.hidden) visible++;
  }
  count.textContent=visible ? visible+' of '+events.length+' events' : 'No matching events. Clear filters to see all activity.';
}
function clearFilters(){field.value='';group.value='';filterEvents();}
function revealAnchor(){
  const target=document.getElementById(location.hash.slice(1));
  if(target && target.parentNode===list){if(target.hidden)clearFilters();target.open=true;target.scrollIntoView();}
}
field.addEventListener('input',filterEvents);group.addEventListener('change',filterEvents);
document.getElementById('clear-events').addEventListener('click',()=>{clearFilters();field.focus();});
window.addEventListener('hashchange',revealAnchor);
document.querySelector('.activity-tools').hidden=false;filterEvents();revealAnchor();
"""


def activity(events):
    groups = sorted({category(e) for e in events})
    options = "".join(f"<option>{g}</option>" for g in groups)
    body = [
        f'<style>{STYLES}</style><h2 id="activity">Complete activity</h2>',
        '<div class="activity-tools" hidden><label>Search activity<input id="event-search" type="search"></label>',
        f'<label>Event type<select id="event-group"><option value="">All events</option>{options}</select></label>',
        '<button type="button" id="clear-events">Clear filters</button></div>',
        f'<p class="muted" role="status" id="match-count">{len(events)} events</p><div id="activity-list">',
    ]
    for e in events:
        seq, group = int(e["seq"]), category(e)
        body.append(
            f'<details id="event-{seq}" data-category="{group}"><summary>'
            f'<time datetime="{escape(e["time"])}">{escape(e["time"])}</time>'
            f"{escape(e['kind'].replace('_', ' ').capitalize())}"
            f'<span class="event-tag">{group}</span><span class="event-id">#{seq}</span></summary>'
            f'<a href="#event-{seq}">Link to event #{seq}</a><pre>{escape(json.dumps(e["details"], indent=2))}</pre></details>'
        )
    body += ["</div>", f'<script type="module">{SCRIPT}</script>']
    return "".join(body)
