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


# A piece that opens like an object ({"key" ... or {key: ...), as opposed to a brace in prose or code.
QUOTES = {"\"": "\"", "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
OBJECT_START = re.compile(r"\{\s*(?:[\"'\u2018\u201c]|[A-Za-z_][\w-]*\s*:)")
ALTERNATIVE = re.compile(r"\s*(?:or\b|[|/])\s*[\"'\u2018\u201c]?\w", re.IGNORECASE)
UNKNOWN = object()  # an unreadable piece names the field but gives no value that can be compared: cut off, or a list


def claims(text, key):
    """What an unreadable piece says its field is. Returns (values, described): the values it gives, UNKNOWN where it names the
    field without one that can be compared, and whether it gave alternatives ("PASS" or "REVISE"), which describe the answer
    format and claim nothing."""
    values, described = [], False
    quotes = "".join(QUOTES) + "".join(QUOTES.values())
    for mention in re.finditer(r"(?<!\w)[" + quotes + r"]?" + re.escape(key) + r"[" + quotes + r"]?\s*[:=]\s*", text, re.IGNORECASE):
        rest, value, after = text[mention.end():], None, ""
        if rest[:1] in QUOTES:
            close = rest.find(QUOTES[rest[0]], 1)
            if close != -1:
                value, after = rest[1:close], rest[close + 1:]
        elif rest[:1] not in ("", "[", "{"):
            value = re.split(r"[,}\n]", rest, maxsplit=1)[0].strip()
        if value and (ALTERNATIVE.match(after) or re.search(r"\bor\b|[|/]", value, re.IGNORECASE)):
            described = True
        else:
            values.append(value or UNKNOWN)
    return values, described


def extract_object(answer, required_key, *, fail_closed=None):
    """The JSON object in the answer that carries required_key, read by the strict parser.

    An answer that is one object (or one fenced block) is taken as it is. Otherwise the last object with the key wins,
    except for the keys in FAIL_CLOSED (or fail_closed=True), which decide something and never guess:
    - the readable objects with the key must agree on its value: a quoted example next to the real answer, or a draft
      followed by a final, is not something to settle by position;
    - a piece that names the key but cannot be read (cut off, duplicate field, a slip such as unescaped quotes) must not say
      anything other than the readable objects, wherever it stands: a malformed REVISE followed by a PASS is not a PASS;
    - an object-shaped piece after the last readable object that cannot be read (cut off before its key, single quotes)
      leaves it unclear whether a later answer was meant, so it counts as unreadable too.
    A field given alternatives, as in the answer format the prompt shows, describes nothing. Each case raises ValueError,
    which sends the caller to its re-ask path."""
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
    found, unread, problem = [], [], None
    for start, end, data in candidates(answer):
        if end is None:
            unread.append(start)
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
    if not found:
        cut = problem or next((ValueError("an object with that field is cut off or malformed") for start in unread
                               if re.search(re.escape(required_key), answer[start:], re.IGNORECASE)), None)
        raise ValueError(f"No JSON object with a {required_key!r} field was found" + (f" ({str(cut)[:200]})" if cut else ""))
    if closed:
        first = found[0][1][required_key]
        if any(not same(first, other[required_key]) for _, other in found[1:]):
            raise ValueError(f"The answer has several objects with a {required_key!r} field that disagree, so which one is meant is unclear")
        last, covered = found[-1][0], -1
        for start in unread:
            if start < covered:
                continue  # inside a piece that was already looked at
            covered = next((later for later, _ in found if later > start), len(answer))
            values, described = claims(answer[start:covered], required_key)
            if any(value is UNKNOWN or not same(first, value) for value in values):
                raise ValueError(f"The answer has a {required_key!r} field that cannot be read (cut off or not valid JSON) and does not match the "
                                 "one that can, so which one is meant is unclear. Reply with exactly one JSON object")
            if not values and not described and start > last and OBJECT_START.search(answer, start, covered):
                raise ValueError("The answer ends with an object that cannot be read (cut off or not valid JSON), so which answer is meant "
                                 "is unclear. Reply with exactly one JSON object")
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
