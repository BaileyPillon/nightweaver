"""Check record-level invariants against two immutable Git objects without executing project code."""

import argparse
import json
import re
import sys
from collections import Counter
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
    if not isinstance(value, str) or (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
        raise ValueError("Use a JSON Pointer with / separators and ~0 or ~1 escapes")
    return value


def at(value, path):
    for part in path.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(key, MISSING)
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]{0,8}", key) and int(key) < len(value):
            value = value[int(key)]
        else:
            return MISSING
    return value


TYPES = {
    "string": lambda v: isinstance(v, str),
    "nonempty_string": lambda v: isinstance(v, str) and bool(v.strip()),
    "number": lambda v: type(v) in (float, int),
    "integer": lambda v: type(v) is int,
    "boolean": lambda v: type(v) is bool,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_contract(raw):
    fields(
        raw,
        "path records key assignment min_records preserve_ids preserve_fields fields references",
        "catalog",
    )
    config = {
        "path": artifact(text(raw, "path")),
        "records": pointer(raw.get("records", "")),
        "key": pointer(raw.get("key", "/id")),
        "min_records": integer(raw.get("min_records", 0), "min_records", 0, 1000000),
    }
    if "assignment" in raw:
        name = text(raw, "assignment")
        if not re.fullmatch(r"[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*", name, flags=re.ASCII):
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
    return config


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
    rows = at(strict_json(data), config["records"])
    if not isinstance(rows, list):
        raise CatalogDataError("The catalog records pointer must select an array")
    records = {}
    for i, row in enumerate(rows):
        key = at(row, config["key"])
        if not isinstance(row, dict) or not valid_id(key):
            raise ValueError(f"Invalid record identity at row {i + 1}")
        if key in records:
            raise ValueError(f"Duplicate record identity at row {i + 1}")
        records[key] = row
    return records


def valid_id(value):
    return type(value) is int or isinstance(value, str) and bool(value.strip())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
    except (ValueError, OSError) as error:
        return {**result, "input_error": str(error)}
    try:
        current = object_data(repo, candidate, config)
    except (ValueError, UnicodeError) as error:
        return {**result, "violations": {"invalid_catalog": 1}, "diagnostic": str(error)}
    counts = Counter()

    def violation(reason, key=None, path=None):
        counts[reason] += 1
        if len(result["examples"]) < 12:
            example = {"reason": reason}
            if key is not None:
                example["id"] = redact(str(key)[:120])
            if path is not None:
                example["field"] = path
            result["examples"].append(example)

    removed = [key for key in original if key not in current]
    result.update(
        candidate_records=len(current),
        added_ids=sum(k not in original for k in current),
        removed_ids=len(removed),
    )
    if config["preserve_ids"]:
        for key in removed:
            violation("missing_id", key)
    if len(current) < config["min_records"]:
        violation("too_few_records")
    for key, row in current.items():
        if key in original:
            for path in config["preserve_fields"]:
                new = at(row, path)
                if new is MISSING or canonical(new) != canonical(at(original[key], path)):
                    violation("changed_field", key, path)
        for path, kind in config["fields"].items():
            if not TYPES[kind](at(row, path)):
                violation("field_type", key, path)
        for path in config["references"]:
            target = at(row, path)
            if target is not MISSING:
                for ref in target if isinstance(target, list) else [target]:
                    if not valid_id(ref) or ref not in current:
                        violation("dangling_reference", key, path)
    result.update(violations=dict(counts), passed=not counts)
    return result


def main():
    parser = argparse.ArgumentParser()
    for arg in ("base", "candidate", "contract", "output"):
        parser.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    config = validate_contract(strict_json(Path(args.contract).read_text(encoding="utf-8")))
    result = check_catalog(Path.cwd(), args.base, args.candidate, config)
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=True))
    return 2 if result.get("input_error") else int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
