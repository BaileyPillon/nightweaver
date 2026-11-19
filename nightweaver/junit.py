"""Grade a fresh JUnit XML report written by an operator-defined command."""

from collections import Counter
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from hashlib import sha256
from xml.etree import ElementTree as ET

from .spec import array, fields, integer, text


def contract(raw):
    fields(raw, "min_passed max_skipped required max_case_ms", "junit")
    policy = {"min_passed": integer(raw.get("min_passed", 1), "min_passed", 1, 100_000)}
    for key, high in (("max_skipped", 100_000), ("max_case_ms", 2_147_483_647)):
        if key in raw:
            policy[key] = integer(raw[key], key, 0, high)
    required = []
    for case in array(raw.get("required", []), "required tests", 100):
        fields(case, "classname name", "required test")
        if not isinstance(case.get("classname"), str):
            raise ValueError(  # noqa: TRY004 - CLI validation uses ValueError
                "Required tests need classname (an empty string is allowed)"
            )
        text(case, "name")
        key = (case["classname"], case["name"])
        if key in required:
            raise ValueError("Duplicate required test identity")
        required.append(key)
    if required:
        policy["required"] = [
            {"classname": cls, "name": name} for cls, name in required
        ]
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


def duration(case):
    if "time" not in case.attrib:
        return None
    try:
        seconds = Decimal(case.get("time"))
        if not seconds.is_finite() or not 0 <= seconds <= 1_000_000_000:
            raise ValueError
        return int((seconds * 1000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid JUnit testcase time") from None


def cases_in(element, cases, identities):
    """Recompute each suite's declared counters from its actual descendants."""
    counts = Counter()
    for child in element:
        kind = tag(child.tag)
        if kind in ("testsuite", "testsuites"):
            counts.update(cases_in(child, cases, identities))
        elif kind == "testcase":
            name, cls = child.get("name"), child.get("classname", "")
            if not name or not name.strip() or len(name) + len(cls) > 2048:
                raise ValueError("JUnit test needs a nonempty bounded case identity")
            if (cls, name) in identities:
                raise ValueError(f"Duplicate JUnit test identity: {cls} / {name}")
            identities.add((cls, name))
            results = [node for node in child if tag(node.tag) in STATUSES]
            states = {STATUSES[tag(node.tag)] for node in results}
            for attr in ("status", "result"):
                hint = child.get(attr, "").casefold()
                if hint in ("", "run", "completed"):
                    continue
                outcome = {
                    "passed": "passed",
                    "success": "passed",
                    "failed": "failed",
                    "failure": "failed",
                    "error": "errors",
                    "notrun": "skipped",
                    "suppressed": "skipped",
                    "disabled": "skipped",
                    "skipped": "skipped",
                }.get(hint)
                if outcome is None:
                    raise ValueError(f"Unsupported JUnit testcase {attr}: {hint[:80]}")
                states.add(outcome)
            if len(states) > 1:
                raise ValueError(f"Contradictory JUnit outcomes for {name}")
            status = next(iter(states), "passed")
            case = {
                "classname": cls,
                "name": name,
                "status": status,
                "ms": duration(child),
            }
            if status in ("failed", "errors"):
                case["message"] = (
                    (results[0].get("message") or results[0].text or "")[:300]
                    if results
                    else ""
                )
            cases.append(case)
            if len(cases) > 100_000:
                raise ValueError("JUnit report exceeds 100000 cases")
            counts.update(("tests", status))
    for attr, key in TOTALS.items():
        if attr in element.attrib:
            declared = element.get(attr)
            if (
                not declared.isascii()
                or not declared.isdecimal()
                or len(declared) > 10
                or int(declared) != counts[key]
            ):
                raise ValueError(
                    f"JUnit {attr} disagrees with measured cases in {element.get('name', 'report')}"
                )
    return counts


def grade(data, policy):
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=ReportTree()))
    except (ET.ParseError, LookupError) as exc:
        raise ValueError(f"Invalid JUnit XML: {exc}") from exc
    if tag(root.tag) not in ("testsuite", "testsuites"):
        raise ValueError("Expected a JUnit testsuite or testsuites document")
    cases = []
    counts = cases_in(root, cases, set())
    counts = {
        key: counts[key] for key in ("tests", "passed", "failed", "errors", "skipped")
    }
    issues = []

    def issue(message):
        if len(issues) < 12:
            issues.append(message)

    for case in cases:
        if case["status"] in ("failed", "errors"):
            issue(
                f"{case['classname']} / {case['name']} ({case['status']}): {case['message']}"
            )
    if counts["passed"] < policy["min_passed"]:
        issue(f"Passing tests {counts['passed']} below required {policy['min_passed']}")
    if "max_skipped" in policy and counts["skipped"] > policy["max_skipped"]:
        issue(
            f"Skipped tests {counts['skipped']} exceed allowed {policy['max_skipped']}"
        )
    passed = {(c["classname"], c["name"]) for c in cases if c["status"] == "passed"}
    for req in policy.get("required", []):
        if (req["classname"], req["name"]) not in passed:
            issue(f"Required test did not pass: {req['classname']} / {req['name']}")
    timed = sorted((c for c in cases if c["ms"] is not None), key=lambda c: -c["ms"])
    if "max_case_ms" in policy:
        for c in cases:
            if c["status"] != "skipped" and (
                c["ms"] is None or c["ms"] > policy["max_case_ms"]
            ):
                issue(
                    f"Missing or over-limit timing: {c['classname']} / {c['name']} (limit {policy['max_case_ms']} ms)"
                )
    return {
        "counts": counts,
        "passed": not issues,
        "issues": issues,
        "timed_cases": len(timed),
        "slowest": [{k: c[k] for k in ("classname", "name", "ms")} for c in timed[:5]],
    }


def read_report(path, receipt, limit, policy):
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
    receipt["junit"] = grade(data, policy)


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
            }
        )
    return {"run_id": run_id, "observations": rows, "model_calls": 0}
