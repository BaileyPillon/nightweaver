"""Check record-level invariants against two immutable Git objects without executing project code."""

import argparse
import json
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nightweaver.engine import git_bytes
from nightweaver.safety import redact, write_json
from nightweaver.spec import array, artifact, fields, integer, strict_json, text

LIMIT = 16 * 1024 * 1024
MISSING = object()


class CatalogDataError(ValueError):
    """A committed input does not meet the operator's data contract."""


def pointer(value):
    if (
        not isinstance(value, str)
        or (value and not value.startswith("/"))
        or re.search(r"~(?![01])", value)
    ):
        raise ValueError("Use a JSON Pointer with / separators and ~0 or ~1 escapes")
    return value


def at(value, path):
    for part in path.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(key, MISSING)
        elif (
            isinstance(value, list)
            and re.fullmatch(r"0|[1-9][0-9]{0,8}", key)
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            return MISSING
    return value


TYPES = {
    "string": lambda v: isinstance(v, str),
    "nonempty_string": lambda v: isinstance(v, str) and bool(v.strip()),
    "number": lambda v: type(v) in (float, int, Decimal),
    "integer": lambda v: (
        type(v) is int or isinstance(v, Decimal) and v == v.to_integral_value()
    ),
    "boolean": lambda v: type(v) is bool,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_contract(raw):
    fields(
        raw,
        "path records key assignment min_records preserve_ids preserve_fields fields references "
        "required choices lengths unique_by unique_items preserve_items required_ids max_removed required_values",
        "catalog",
    )
    config = {
        "path": artifact(text(raw, "path")),
        "records": pointer(raw.get("records", "")),
        "key": pointer(raw.get("key", "/id")),
        "min_records": integer(raw.get("min_records", 0), "min_records", 0, 1000000),
    }
    if not config["key"]:
        raise ValueError("The identity pointer must select a field inside each record")
    if "assignment" in raw:
        name = text(raw, "assignment")
        if not re.fullmatch(
            r"[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*", name, flags=re.ASCII
        ):
            raise ValueError("Use a fixed JavaScript assignment name")
        config["assignment"] = name
    keep = raw.get("preserve_ids", True)
    if type(keep) is not bool:
        raise ValueError("preserve_ids must be boolean")
    config["preserve_ids"] = keep
    for key in ("preserve_fields", "references"):
        paths = [pointer(p) for p in array(raw.get(key, []), key, 32)]
        if len(set(paths)) != len(paths):
            raise ValueError(f"Duplicate {key} pointer")
        config[key] = paths
    types = raw.get("fields", {})
    if (
        not isinstance(types, dict)
        or len(types) > 32
        or any(not isinstance(v, str) or v not in TYPES for v in types.values())
    ):
        raise ValueError("Use at most 32 typed catalog fields")
    config["fields"] = {pointer(k): v for k, v in types.items()}
    config.update(extra_rules(raw))
    return config


def pointers(values, label):
    paths = [pointer(p) for p in array(values, label, 32)]
    if len(set(paths)) != len(paths):
        raise ValueError(f"Duplicate {label} pointer")
    return paths


def extra_rules(raw):
    rules = {}
    for label in ("required", "unique_items", "preserve_items"):
        if label in raw:
            rules[label] = pointers(raw[label], label)
    for label in ("choices", "required_values", "lengths"):
        if label not in raw:
            continue
        mapping = raw[label]
        if not isinstance(mapping, dict) or len(mapping) > 32:
            raise ValueError(f"{label} requires up to 32 field pointers")
        rules[label] = {}
        for key, value in mapping.items():
            path = pointer(key)
            if label == "lengths":
                fields(value, "min max", "length bounds")
                bounds = {k: integer(v, k, 0, LIMIT) for k, v in value.items()}
                if not bounds or bounds.get("min", 0) > bounds.get("max", LIMIT):
                    raise ValueError(f"Invalid length bounds for {path}")
                rules[label][path] = bounds
            else:
                values = array(value, label, 128)
                if (
                    not values
                    or any(not isinstance(v, str) or len(v) > 512 for v in values)
                    or len(set(values)) != len(values)
                ):
                    raise ValueError(f"{label} needs distinct string values at {path}")
                rules[label][path] = values
    if "unique_by" in raw:
        groups = [
            pointers(group, "unique_by")
            for group in array(raw["unique_by"], "unique_by", 16)
        ]
        if any(not g for g in groups) or len({frozenset(g) for g in groups}) != len(
            groups
        ):
            raise ValueError(
                "unique_by needs distinct, nonempty groups of field pointers"
            )
        rules["unique_by"] = groups
    if "required_ids" in raw:
        ids = array(raw["required_ids"], "required_ids", 256)
        if any(not valid_id(v) for v in ids) or len(set(ids)) != len(ids):
            raise ValueError("required_ids needs distinct catalog identities")
        rules["required_ids"] = ids
    if "max_removed" in raw:
        rules["max_removed"] = integer(raw["max_removed"], "max_removed", 0, 1_000_000)
    return rules


def object_data(repo, revision, config):
    path = config["path"]

    def git_object(*args):
        try:
            return git_bytes(repo, *args)
        except ValueError as error:
            raise OSError(f"Cannot read Git object for {path}: {error}") from error

    entry = git_object("--literal-pathspecs", "ls-tree", "-z", revision, "--", path)
    if not entry:
        raise ValueError(f"Missing catalog file: {path}")
    mode, kind, oid = entry.split(b"\t", 1)[0].decode("ascii").split()
    if kind != "blob" or mode not in ("100644", "100755"):
        raise ValueError("Catalog must be a regular tracked file")
    if int(git_object("cat-file", "-s", oid)) > LIMIT:
        raise ValueError("Catalog exceeds 16 MiB")
    data = git_object("cat-file", "blob", oid).decode("utf-8-sig").strip()
    if "assignment" in config:
        match = re.match(re.escape(config["assignment"]) + r"\s*=\s*", data)
        if not match:
            raise ValueError("Catalog assignment differs from the contract")
        data = data[match.end() :].removesuffix(";").rstrip()
    try:
        rows = at(strict_json(data, parse_float=Decimal), config["records"])
    except InvalidOperation as error:
        raise ValueError("Unsupported catalog decimal exponent") from error
    if not isinstance(rows, list):
        raise CatalogDataError("The catalog records pointer must select an array")
    records = {}
    for i, row in enumerate(rows):
        check_depth(row)
        key = at(row, config["key"])
        if not isinstance(row, dict) or not valid_id(key):
            raise ValueError(f"Invalid record identity at row {i + 1}")
        if key in records:
            raise ValueError(f"Duplicate record identity at row {i + 1}")
        records[key] = row
    return records


def valid_id(value):
    return type(value) is int or isinstance(value, str) and bool(value.strip())


def check_depth(row):
    pending = [(row, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 128:
            raise ValueError("Catalog record nesting exceeds 128 levels")
        children = value.values() if isinstance(value, dict) else value
        if isinstance(value, (list, dict)):
            pending.extend(
                (child, depth + 1)
                for child in children
                if isinstance(child, (list, dict))
            )


def canonical(value):
    """Hashable JSON equality with exact decimals, unordered objects and typed booleans."""
    if isinstance(value, dict):
        return ("object", tuple((k, canonical(value[k])) for k in sorted(value)))
    if isinstance(value, list):
        return ("array", tuple(map(canonical, value)))
    if type(value) in (int, Decimal, float):
        return ("number", value)
    return (type(value).__name__, value)


def inspect_row(key, row, config, current, violation):
    for path in config.get("required", []):
        if at(row, path) is MISSING:
            violation("missing_field", key, path)
    for path, values in config.get("choices", {}).items():
        if at(row, path) not in values:
            violation("outside_choices", key, path)
    for path, bounds in config.get("lengths", {}).items():
        value = at(row, path)
        if not isinstance(value, (str, list)) or not bounds.get("min", 0) <= len(
            value
        ) <= bounds.get("max", LIMIT):
            violation("field_length", key, path)
    for path in config.get("unique_items", []):
        value = at(row, path)
        if not isinstance(value, list) or len({canonical(v) for v in value}) != len(
            value
        ):
            violation("nonunique_items", key, path)
    for path, kind in config["fields"].items():
        if not TYPES[kind](at(row, path)):
            violation("field_type", key, path)
    for path in config["references"]:
        target = at(row, path)
        if target is not MISSING:
            for ref in target if isinstance(target, list) else [target]:
                if not valid_id(ref) or ref not in current:
                    violation("dangling_reference", key, path)


def index_row(key, row, config, seen_keys, needed, violation):
    for paths, seen in zip(config.get("unique_by", []), seen_keys):
        values = [at(row, path) for path in paths]
        if any(
            v is MISSING or v is None or isinstance(v, (dict, list)) for v in values
        ):
            violation("invalid_unique_key", key, fields=paths)
        else:
            composite = tuple(map(canonical, values))
            if composite in seen:
                violation("duplicate_key", key, fields=paths)
            seen.add(composite)
    for path, missing in needed.items():
        value = at(row, path)
        for item in value if isinstance(value, list) else (value,):
            if isinstance(item, str):
                missing.discard(item)


def check_catalog(repo, base, candidate, config):
    result = {
        "schema": "nightweaver.catalog.v1",
        "base": base,
        "tree": candidate,
        "passed": False,
        "baseline_records": None,
        "candidate_records": None,
        "violations": {},
        "examples": [],
    }
    try:
        original = object_data(repo, base, config)
        result["baseline_records"] = len(original)
        for path in config["preserve_fields"]:
            if any(at(row, path) is MISSING for row in original.values()):
                raise ValueError(f"Baseline lacks a preserved field: {path}")
        for path in config.get("preserve_items", []):
            if any(not isinstance(at(row, path), list) for row in original.values()):
                raise ValueError(f"Baseline lacks a preserved array: {path}")
    except (ValueError, OSError) as error:
        return {**result, "input_error": str(error)}
    try:
        current = (
            original if candidate == base else object_data(repo, candidate, config)
        )
    except (ValueError, UnicodeError) as error:
        return {
            **result,
            "violations": {"invalid_catalog": 1},
            "diagnostic": str(error),
        }
    except OSError as error:
        return {**result, "input_error": str(error)}
    counts = Counter()
    groups = {}

    def violation(reason, key=None, path=None, **context):
        counts[reason] += 1
        examples = groups.setdefault(reason, [])
        if len(examples) < 12:
            example = {"reason": reason, **context}
            if key is not None:
                example["id"] = redact(str(key)[:120])
                example["id_type"] = "integer" if type(key) is int else "string"
            if path is not None:
                example["field"] = path
            examples.append(example)

    removed = [key for key in original if key not in current]
    result.update(
        candidate_records=len(current),
        added_ids=sum(k not in original for k in current),
        removed_ids=len(removed),
    )
    if "max_removed" in config and len(removed) > config["max_removed"]:
        violation("removal_budget", observed=len(removed), limit=config["max_removed"])
    for key in config.get("required_ids", []):
        if key not in current:
            violation("required_id", key)
    if config["preserve_ids"]:
        for key in removed:
            violation("missing_id", key)
    if len(current) < config["min_records"]:
        violation("too_few_records")
    changed = 0
    seen_keys = [set() for group in config.get("unique_by", [])]
    needed = {p: set(v) for p, v in config.get("required_values", {}).items()}
    for key, row in current.items():
        if key in original:
            if current is not original and canonical(row) != canonical(original[key]):
                changed += 1
            for path in config["preserve_fields"]:
                new = at(row, path)
                if new is MISSING or canonical(new) != canonical(
                    at(original[key], path)
                ):
                    violation("changed_field", key, path)
            for path in config.get("preserve_items", []):
                after = at(row, path)
                before = Counter(map(canonical, at(original[key], path)))
                if not isinstance(after, list) or before - Counter(
                    map(canonical, after)
                ):
                    violation("lost_items", key, path)
        inspect_row(key, row, config, current, violation)
        index_row(key, row, config, seen_keys, needed, violation)
    for path, missing in needed.items():
        for value in sorted(missing):
            violation("missing_value", path=path, value=redact(value))
    # Give each violated rule a turn before a noisy rule uses the remaining slots.
    examples = [
        item
        for batch in zip_longest(*groups.values())
        for item in batch
        if item is not None
    ][:12]
    result.update(
        violations=dict(counts),
        passed=not counts,
        changed_records=changed,
        unchanged_records=len(current) - result["added_ids"] - changed,
        examples=examples,
        omitted_examples=sum(counts.values()) - len(examples),
    )
    return result


def field_profile(records, paths):
    """Summarize a committed catalog without returning prompt text or field values."""
    rows = []
    for path in pointers(paths, "profile fields"):
        kinds = Counter()
        distinct = set()
        minimum = maximum = None
        for row in records.values():
            value = at(row, path)
            if value is MISSING:
                kinds["missing"] += 1
                continue
            kind, normalized = canonical(value)
            kinds[
                {"str": "string", "bool": "boolean", "NoneType": "null"}.get(kind, kind)
            ] += 1
            distinct.add((kind, normalized))
            if isinstance(value, (str, list)):
                size = len(value)
                minimum = size if minimum is None else min(size, minimum)
                maximum = size if maximum is None else max(size, maximum)
        rows.append(
            {
                "field": path,
                "counts": dict(kinds),
                "distinct_present": len(distinct),
                "min_length": minimum,
                "max_length": maximum,
            }
        )
    return rows


def validate_receipt(report, base, tree, code):
    """Reconcile the child process verdict with its counts before accepting it."""

    def count(v):
        return type(v) is int and v >= 0

    if (
        not isinstance(report, dict)
        or report.get("schema") != "nightweaver.catalog.v1"
        or report.get("base") != base
        or report.get("tree") != tree
        or type(report.get("passed")) is not bool
    ):
        raise ValueError("Catalog receipt does not identify the requested comparison")
    if "input_error" in report:
        if (
            code != 2
            or report["passed"]
            or not isinstance(report["input_error"], str)
            or not report["input_error"]
        ):
            raise ValueError(
                "Catalog input diagnostic disagrees with the process outcome"
            )
        return
    violations = report.get("violations")
    if (
        not isinstance(violations, dict)
        or any(not count(v) or v == 0 for v in violations.values())
        or report["passed"] != (not violations)
        or code != int(not report["passed"])
    ):
        raise ValueError(
            "Catalog verdict disagrees with its violations or command exit"
        )
    old, new = report.get("baseline_records"), report.get("candidate_records")
    if not count(old):
        raise ValueError("Invalid baseline record count")
    if (
        new is None
        and violations == {"invalid_catalog": 1}
        and isinstance(report.get("diagnostic"), str)
    ):
        return
    added, removed = report.get("added_ids"), report.get("removed_ids")
    if not all(count(v) for v in (new, added, removed)):
        raise ValueError("Invalid catalog record or identity count")
    if added > new or removed > old or old - removed != new - added:
        raise ValueError("Catalog record counts do not reconcile")
    changed, unchanged = report.get("changed_records"), report.get("unchanged_records")
    if (
        not all(count(v) for v in (changed, unchanged))
        or changed + unchanged != new - added
    ):
        raise ValueError("Catalog matched row counts do not reconcile")
    examples = report.get("examples")
    if (
        not isinstance(examples, list)
        or len(examples) > 12
        or any(
            not isinstance(ex, dict)
            or not isinstance(ex.get("reason"), str)
            or ex["reason"] not in violations
            for ex in examples
        )
        or not count(report.get("omitted_examples"))
        or len(examples) + report["omitted_examples"] != sum(violations.values())
    ):
        raise ValueError("Catalog example counts or reasons do not reconcile")


def main():
    parser = argparse.ArgumentParser()
    for arg in ("base", "candidate", "contract", "output"):
        parser.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    config = validate_contract(
        strict_json(Path(args.contract).read_text(encoding="utf-8"))
    )
    result = check_catalog(Path.cwd(), args.base, args.candidate, config)
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=True))
    return 2 if result.get("input_error") else int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
