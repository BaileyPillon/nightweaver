"""Grade a fresh JUnit XML report written by an operator-defined command."""

from collections import Counter
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from hashlib import sha256
from xml.etree import ElementTree as ET

from .spec import array, fields, integer, text


def selector(raw, *, symptom=False):
    fields(
        raw,
        "classname name suite" + (" message_contains" if symptom else ""),
        "case selector",
    )
    if not isinstance(raw.get("classname"), str):
        raise ValueError("Case selectors require a classname string")  # noqa: TRY004 - plan validation uses ValueError
    text(raw, "name")
    if len(raw["classname"]) + len(raw["name"]) > 2048:
        raise ValueError("Case selector is too long")
    out = {"classname": raw["classname"], "name": raw["name"]}
    if "suite" in raw:
        path = array(raw["suite"], "suite path", 64)
        if any(not isinstance(s, str) or not s or len(s) > 512 for s in path):
            raise ValueError("Use an array of nonempty suite names")
        out["suite"] = list(path)
    if "message_contains" in raw:
        msg = raw["message_contains"]
        if not isinstance(msg, str) or not msg.strip() or len(msg) > 512:
            raise ValueError("message_contains needs 1 to 512 characters")
        out["message_contains"] = msg
    return out


def selectors(raw, label, *, symptom=False):
    items = []
    seen = set()
    for case in array(raw, label, 100):
        item = selector(case, symptom=symptom)
        key = (
            tuple(item["suite"]) if "suite" in item else None,
            item["classname"],
            item["name"],
        )
        if key in seen:
            raise ValueError(f"Duplicate {label} test identity")
        seen.add(key)
        items.append(item)
    return items


def contract(raw):
    fields(
        raw,
        "min_passed max_skipped required max_case_ms max_total_case_ms max_flaky",
        "junit",
    )
    policy = {"min_passed": integer(raw.get("min_passed", 1), "min_passed", 1, 100_000)}
    for key, high in (
        ("max_skipped", 100_000),
        ("max_flaky", 100_000),
        ("max_case_ms", 2_147_483_647),
        ("max_total_case_ms", 2_147_483_647),
    ):
        if key in raw:
            policy[key] = integer(raw[key], key, 0, high)
    required = selectors(raw.get("required", []), "required")
    if required:
        policy["required"] = required
    return policy


def tag(value):
    return value.rsplit("}", 1)[-1]


class ReportTree(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.nodes = 0

    def start(self, name, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > 64 or self.nodes > 250_000:
            raise ValueError(
                "JUnit report exceeds the supported depth or element count"
            )
        return super().start(name, attrs)

    def end(self, name):
        self.depth -= 1
        return super().end(name)

    def doctype(self, name, pubid, system):
        raise ValueError("JUnit reports must not contain a document type declaration")


STATUSES = {"failure": "failed", "error": "errors", "skipped": "skipped"}
TOTALS = {
    "tests": "tests",
    "failures": "failed",
    "errors": "errors",
    "skipped": "skipped",
}


OUTCOMES = {
    "passed": "passed",
    "success": "passed",
    "failed": "failed",
    "failure": "failed",
    "error": "errors",
    "notrun": "skipped",
    "suppressed": "skipped",
    "disabled": "skipped",
    "skipped": "skipped",
}
RECOVERED = {"flakyFailure", "flakyError"}
RETRIED = {"rerunFailure", "rerunError"}
STRUCTURAL = (
    set(STATUSES) | RECOVERED | RETRIED | {"testsuite", "testsuites", "testcase"}
)
METADATA = {"properties", "system-out", "system-err"}


def duration(case):
    if "time" not in case.attrib:
        return None
    try:
        raw = case.get("time")
        if len(raw) > 256:
            raise ValueError
        seconds = Decimal(raw)
        if not seconds.is_finite() or not 0 <= seconds <= 1_000_000_000:
            raise ValueError
        # Shift the decimal point exactly before rounding. Arithmetic multiplication
        # can round away a positive fraction just above the configured boundary.
        value = seconds.as_tuple()
        if not seconds:
            return 0
        if seconds < Decimal("0.001"):
            return 1
        ms = Decimal((value.sign, value.digits, value.exponent + 3))
        return int(ms.to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid JUnit testcase time") from None


def validate_extension(node):
    if tag(node.tag) not in METADATA and any(
        tag(n.tag) in STRUCTURAL for n in node.iter()
    ):
        raise ValueError(f"Misplaced JUnit result or test beneath {tag(node.tag)}")


def read_case(child, suite):
    name, cls = child.get("name"), child.get("classname", "")
    if not name or not name.strip() or len(name) + len(cls) > 2048:
        raise ValueError("JUnit test needs a nonempty bounded case identity")
    results = []
    retries = recovered = 0
    for node in child:
        kind = tag(node.tag)
        if kind in STATUSES:
            results.append(node)
        elif kind in RECOVERED:
            recovered += 1
        elif kind in RETRIED:
            retries += 1
        else:
            validate_extension(node)
    states = {STATUSES[tag(node.tag)] for node in results}
    hints = {child.get(k, "").casefold() for k in ("status", "result")} - {
        "",
        "run",
        "completed",
    }
    for hint in hints:
        if hint not in OUTCOMES:
            raise ValueError(f"Unsupported JUnit testcase outcome: {hint[:80]}")
        states.add(OUTCOMES[hint])
    if len(states) > 1:
        raise ValueError(f"Contradictory JUnit outcomes for {name}")
    status = next(iter(states), "passed")
    if (recovered and status != "passed") or (
        retries and status not in ("failed", "errors")
    ):
        raise ValueError(f"Inconsistent JUnit retry evidence for {name}")
    messages = [
        part for node in results for part in (node.get("message"), node.text) if part
    ]
    return {
        "suite": suite,
        "classname": cls,
        "name": name,
        "status": status,
        "ms": duration(child),
        "disabled": bool(hints & {"notrun", "suppressed", "disabled"}),
        "flaky": bool(recovered),
        "retry_failures": recovered + retries,
        "messages": messages,
    }


def cases_in(element, cases, identities, suite=()):
    counts = Counter()
    if tag(element.tag) == "testsuite" and element.get("name"):
        name = element.get("name")
        if len(name) > 512:
            raise ValueError("JUnit suite name exceeds 512 characters")
        suite = (*suite, name)
    for child in element:
        kind = tag(child.tag)
        if kind in ("testsuite", "testsuites"):
            counts.update(cases_in(child, cases, identities, suite))
        elif kind == "testcase":
            case = read_case(child, suite)
            key = (suite, case["classname"], case["name"])
            if key in identities:
                raise ValueError(f"Duplicate JUnit test identity: {key}")
            identities.add(key)
            cases.append(case)
            if len(cases) > 100_000:
                raise ValueError("JUnit report exceeds 100000 cases")
            counts.update(("tests", case["status"]))
            counts["disabled"] += int(case["disabled"])
        else:
            validate_extension(child)
    for attr, key in {**TOTALS, "disabled": "disabled"}.items():
        if attr in element.attrib:
            measured = counts[key]
            if attr == "skipped" and "disabled" in element.attrib:
                measured -= counts["disabled"]
            declared = element.get(attr)
            if (
                not declared.isascii()
                or not declared.isdecimal()
                or len(declared) > 10
                or int(declared) != measured
            ):
                raise ValueError(
                    f"JUnit {attr} disagrees with measured cases in {element.get('name', 'report')}"
                )
    return counts


def summarize_case(case):
    return {
        k: case[k]
        for k in (
            "suite",
            "classname",
            "name",
            "status",
            "ms",
            "flaky",
            "retry_failures",
        )
    }


def select_case(index, query):
    matches = index.get((query["classname"], query["name"]), [])
    if "suite" in query:
        matches = [c for c in matches if c["suite"] == tuple(query["suite"])]
    if len(matches) != 1:
        return {"selector": query, "status": "ambiguous" if matches else "missing"}
    case = matches[0]
    row = {**summarize_case(case), "selector": query}
    if case["messages"]:
        row["message"] = case["messages"][0][:300]
    if "message_contains" in query:
        row["symptom_matched"] = any(
            query["message_contains"] in m for m in case["messages"]
        )
    return row


def grade(data, policy, tracked=()):
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=ReportTree()))
    except (ET.ParseError, LookupError) as exc:
        raise ValueError(f"Invalid JUnit XML: {exc}") from exc
    if tag(root.tag) not in ("testsuite", "testsuites"):
        raise ValueError("Expected a JUnit testsuite or testsuites document")
    cases = []
    totals = cases_in(root, cases, set())
    counts = {k: totals[k] for k in ("tests", "passed", "failed", "errors", "skipped")}
    index = {}
    for c in cases:
        index.setdefault((c["classname"], c["name"]), []).append(c)
    issues = []
    total_issues = 0

    def issue(message):
        nonlocal total_issues
        total_issues += 1
        if len(issues) < 12:
            issues.append(message)

    for req in policy.get("required", []):
        observed = select_case(index, req)
        if observed["status"] != "passed":
            issue(
                f"Required test did not pass: {req['classname']} / {req['name']} ({observed['status']})"
            )
    for case in cases:
        if case["status"] in ("failed", "errors"):
            msg = case["messages"][0][:300] if case["messages"] else ""
            issue(f"{case['classname']} / {case['name']} ({case['status']}): {msg}")
    if counts["passed"] < policy["min_passed"]:
        issue(f"Passing tests {counts['passed']} below required {policy['min_passed']}")
    if "max_skipped" in policy and counts["skipped"] > policy["max_skipped"]:
        issue(
            f"Skipped tests {counts['skipped']} exceed allowed {policy['max_skipped']}"
        )
    flaky = sum(c["flaky"] for c in cases)
    if "max_flaky" in policy and flaky > policy["max_flaky"]:
        issue(f"Recovered flaky tests {flaky} exceed allowed {policy['max_flaky']}")
    executed = [c for c in cases if c["status"] != "skipped"]
    timed = sorted((c for c in executed if c["ms"] is not None), key=lambda c: -c["ms"])
    total_ms = sum(c["ms"] for c in timed)
    if "max_total_case_ms" in policy and (
        len(timed) != len(executed) or total_ms > policy["max_total_case_ms"]
    ):
        issue(
            f"Total case time {total_ms} ms across {len(timed)}/{len(executed)} timed executions; budget {policy['max_total_case_ms']} ms"
        )
    for c in executed:
        if "max_case_ms" in policy and (
            c["ms"] is None or c["ms"] > policy["max_case_ms"]
        ):
            issue(
                f"Missing or over-limit timing: {c['classname']} / {c['name']} (limit {policy['max_case_ms']} ms)"
            )
    return {
        "counts": counts,
        "passed": not issues,
        "issues": issues,
        "omitted_issues": total_issues - len(issues),
        "disabled_cases": totals["disabled"],
        "flaky_cases": flaky,
        "retry_failures": sum(c["retry_failures"] for c in cases),
        "timed_cases": len(timed),
        "executed_cases": len(executed),
        "measured_case_ms": total_ms,
        "slowest": [summarize_case(c) for c in timed[:5]],
        "tracked": [select_case(index, q) for q in tracked],
    }


def read_report(path, receipt, limit, policy, tracked=()):
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve().parent != path.parent.resolve()
    ):
        raise ValueError("Missing regular JUnit report in this gate's evidence folder")
    with path.open("rb") as stream:
        if path.stat().st_size > limit:
            raise ValueError("JUnit report exceeds the gate artifact limit")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JUnit report exceeds the gate artifact limit")
    receipt["artifacts"].append(
        {"name": path.name, "path": str(path), "sha256": sha256(data).hexdigest()}
    )
    receipt["junit"] = grade(data, policy, tracked)


def history(store, run_id):
    run = store.get(run_id)
    selected = {
        (i, j)
        for i, step in enumerate(run["spec"]["milestones"], 1)
        for j, gate in enumerate(step["gates"], 1)
        if "junit" in gate
    }
    from .checks import observed_pass

    rows = []
    for event in store.events(run_id, limit=None, kinds=["gate_finished"]):
        detail = event["details"]
        if (detail.get("step"), detail.get("gate_index")) not in selected:
            continue
        rows.append(
            {
                **{
                    k: detail.get(k)
                    for k in ("step", "attempt", "name", "tree", "exit")
                },
                "event": event["seq"],
                "sample": detail.get("sample", 1),
                "passed": observed_pass(detail),
                "error": detail.get("artifact_error") or detail.get("source_error"),
                "report": detail.get("junit"),
                "proof": detail.get("proof"),
            }
        )
    return {"run_id": run_id, "observations": rows, "model_calls": 0}
