"""Tolerant reading of the JSON verdict a model buries in prose, code fences or trailing commentary."""

import json
import re

from .providers import json_response

LIMIT = 1000000
DECODER = json.JSONDecoder()


def candidates(text):
    """Every top-level JSON object in the text, in order. A brace in prose that does not start an object is skipped."""
    start = text.find("{")
    while start != -1:
        try:
            _, end = DECODER.raw_decode(text, start)
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)
        else:
            yield text[start:end]
            start = text.find("{", end)


def extract_object(answer, required_key):
    """The last top-level JSON object containing required_key, parsed by the strict parser."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("The answer is empty")
    if len(answer.encode("utf-8")) > LIMIT:
        raise ValueError("Structured response exceeds 1MB")
    problem = None
    for text in reversed(list(candidates(answer))):
        try:
            data = json_response(text)
        except ValueError as error:
            problem = error
            continue
        if required_key in data:
            return data
    raise ValueError(f"No JSON object with a {required_key!r} field was found" + (f" ({str(problem)[:200]})" if problem else ""))


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
