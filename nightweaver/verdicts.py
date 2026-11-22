"""Tolerant reading of the JSON verdict a model buries in prose, code fences or trailing commentary."""

import json
import re

from .providers import json_response

LIMIT = 1000000
DECODER = json.JSONDecoder()
# The answers that decide something: a verdict accepts or rejects a build, milestones are what gets built. These never guess.
FAIL_CLOSED = ("verdict", "milestones")


def candidates(text):
    """Every top-level piece of the text that starts with a brace, in order, as (start, end, decoded). end and decoded are
    None where the brace opens something that is not JSON: prose, or an answer cut off."""
    start = text.find("{")
    while start != -1:
        try:
            data, end = DECODER.raw_decode(text, start)
        except (ValueError, RecursionError):
            yield start, None, None
            start = text.find("{", start + 1)
        else:
            yield start, end, data
            start = text.find("{", end)


def same(left, right):
    """Whether two values of a field agree. Strings compare as verdicts do: pass, Pass and PASS are one answer."""
    def key(value):
        return re.sub(r"[\s-]+", "_", value.strip()).upper() if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return key(left) == key(right)


def extract_object(answer, required_key, *, fail_closed=None):
    """The JSON object in the answer that carries required_key, read by the strict parser.

    An answer that is one object (or one fenced block) is taken as it is. Otherwise the last object with the key wins,
    except for the keys in FAIL_CLOSED (or fail_closed=True): there every object with the key must agree on its value,
    since a quoted example next to the real answer, or a draft followed by a final, is not something to guess about, and an
    object with the key that comes last but cannot be read (cut off, duplicate field) is never skipped in favor of an
    earlier one. Those cases raise ValueError, which sends the caller to its re-ask path."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("The answer is empty")
    if len(answer.encode("utf-8")) > LIMIT:
        raise ValueError("Structured response exceeds 1MB")
    try:
        whole = json_response(answer)
    except (ValueError, RecursionError):
        whole = None
    if whole is not None and required_key in whole:
        return whole
    closed = required_key in FAIL_CLOSED if fail_closed is None else fail_closed
    pieces = list(candidates(answer))
    shaped = re.compile(r'"' + re.escape(required_key) + r'"\s*:')
    found, unread, problem = [], [], None
    for index, (start, end, data) in enumerate(pieces):
        if end is None:
            following = next((later for later, stop, _ in pieces[index + 1:] if stop is not None), len(answer))
            if shaped.search(answer[start:following]):
                unread.append(start)
                problem = problem or ValueError("an object with that field is cut off or malformed")
            continue
        try:
            data = json_response(answer[start:end])
        except (ValueError, RecursionError) as error:
            if isinstance(data, dict) and required_key in data:
                unread.append(start)
                problem = problem or error
            continue
        if required_key in data:
            found.append((start, data))
    if closed and found and any(start > found[-1][0] for start in unread):
        raise ValueError(f"The answer has a later object with a {required_key!r} field that cannot be read ({str(problem)[:200]}), "
                         "so which one is meant is unclear")
    if not found:
        raise ValueError(f"No JSON object with a {required_key!r} field was found" + (f" ({str(problem)[:200]})" if problem else ""))
    if closed and any(not same(found[0][1][required_key], other[required_key]) for _, other in found[1:]):
        raise ValueError(f"The answer has several objects with a {required_key!r} field that disagree, so which one is meant is unclear")
    return found[-1][1]


def normalize(value, allowed):
    """pass, Pass and PASS read the same, and 'not met', not-met and NOT_MET too. None if it is not one of allowed."""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s-]+", "_", value.strip()).upper()
    return key if key in allowed else None


def extract_verdict(answer, allowed):
    data = extract_object(answer, "verdict")
    verdict = normalize(data["verdict"], allowed)
    if verdict is None:
        raise ValueError("verdict must be " + " or ".join(allowed))
    return {**data, "verdict": verdict}


def conflict_notes(value):
    """The optional list of contradictory or unsatisfiable requirements a planner reports."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10 or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in value):
        raise ValueError("conflicts must be a list of at most 10 nonempty notes of at most 1000 characters")
    return [item.strip() for item in value]
