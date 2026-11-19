"""Browser scenario contracts shared by execution, evidence ingestion and reports."""

import json
import re
from collections import Counter
from urllib.parse import urlsplit
from uuid import uuid4

from nightweaver.server import local_path
from nightweaver.spec import array, artifact, fields, integer, text


def case_prefix(scenario, viewport):
    return f"{scenario}.{viewport}" if scenario is not None else viewport


def browser_cases(config):
    for scenario in config.get("scenarios", [{}]):
        for viewport in config["viewports"]:
            yield scenario, viewport, config["steps"] + scenario.get("steps", [])


def validate_scenarios(config, validate_steps):
    scenarios = array(config["scenarios"], "browser scenarios", 12)
    if not scenarios:
        raise ValueError("Browser scenarios must contain at least one case")
    names = set()
    for scenario in scenarios:
        fields(scenario, "name local_storage session_storage steps", "browser scenario")
        name = text(scenario, "name")
        if (
            name != scenario["name"]
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name)
            or name in names
        ):
            raise ValueError("Use unique lowercase scenario names")
        artifact(name + ".png")
        names.add(name)
        for kind in ("local_storage", "session_storage"):
            if kind not in scenario:
                continue
            values = scenario[kind]
            if (
                not isinstance(values, dict)
                or len(values) > 64
                or any(not k or not isinstance(v, str) for k, v in values.items())
                or len(json.dumps(values).encode()) > 65536
            ):
                raise ValueError(
                    f"{kind} requires at most 64 string entries and 64 KiB of fixture data"
                )
            if config.get("offline") and values:
                raise ValueError("Seeded storage requires an HTTP origin")
        extra = array(scenario.get("steps", []), "scenario steps", 100)
        validate_steps(config["steps"] + extra, config.get("offline", False))


LOCATOR_ACTIONS = {
    "accessible_name",
    "accessible_description",
    "attribute",
    "select",
    "uncheck",
    "in_viewport",
}
PAGE_ACTIONS = {"storage", "response", "url_path", "reopen"}


def validate_action(step, offline):
    action = step["action"]
    value = step.get("value")
    if action in PAGE_ACTIONS and set(step) != (
        {"action"} if action == "reopen" else {"action", "value"}
    ):
        raise ValueError(f"Invalid fields for {action}")
    if action == "storage":
        fields(value, "area key equals", "storage assertion")
        if (
            offline
            or value.get("area") not in ("local", "session")
            or set(value) != {"area", "key", "equals"}
        ):
            raise ValueError(
                "Storage assertions require an HTTP origin, area, key and equals"
            )
        text(value, "key")
        if value["equals"] is not None and not isinstance(value["equals"], str):
            raise ValueError(
                "storage equals must be a string or null for an absent key"
            )
    elif action == "response":
        fields(value, "method path status", "response assertion")
        if offline or value.get("method") not in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        ):
            raise ValueError(
                "Response assertions require an HTTP origin and an uppercase method"
            )
        pathname(value.get("path"))
        integer(value.get("status"), "response status", 100, 599)
    elif action == "url_path":
        pathname(value)
        if offline:
            raise ValueError("URL path assertions require an HTTP origin")
    elif action == "attribute":
        fields(value, "name equals", "attribute assertion")
        if set(value) != {"name", "equals"} or not re.fullmatch(
            r"[A-Za-z_:][\w:.-]*", text(value, "name")
        ):
            raise ValueError("attribute needs a valid name and equals")
        if value["equals"] is not None and not isinstance(value["equals"], str):
            raise ValueError("attribute equals must be a string or null")
    elif action in (
        "accessible_name",
        "accessible_description",
        "select",
    ) and not isinstance(value, str):
        raise ValueError(f"{action} requires a string value")
    elif action == "in_viewport":
        ratio = step.get("value", 0)
        if type(ratio) not in (int, float) or not 0 <= ratio <= 1:
            raise ValueError("in_viewport ratio must be between 0 and 1")


def pathname(value):
    local_path(value, "browser path")
    if "?" in value or "#" in value:
        raise ValueError("Use a URL pathname without a query or fragment")
    return value


def origin(url):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port


def initial_page(context, url, scenario):
    page = context.new_page()
    if not scenario.get("session_storage"):
        return page
    # This synthetic same-origin document initializes only this tab, once, before
    # app script execution. Reload, clearing storage and reopening cannot reseed it.
    parts = urlsplit(url)
    bootstrap = f"{parts.scheme}://{parts.netloc}/.nightweaver-fixture-{uuid4().hex}"
    page.route(
        bootstrap,
        lambda route: route.fulfill(
            content_type="text/html",
            body='<title>Fixture</title><link rel="icon" href="data:,">',
        ),
    )
    try:
        page.goto(bootstrap, wait_until="load")
        page.evaluate(
            "(entries) => entries.forEach(([k,v]) => sessionStorage.setItem(k,v))",
            list(scenario["session_storage"].items()),
        )
    finally:
        page.unroute(bootstrap)
    return page


def storage_state(url, scenario):
    parts = urlsplit(url)
    values = scenario.get("local_storage", {})
    if not values:
        return None
    return {
        "cookies": [],
        "origins": [
            {
                "origin": f"{parts.scheme}://{parts.netloc}",
                "localStorage": [{"name": k, "value": v} for k, v in values.items()],
            }
        ],
    }


def assert_page_state(page, step, url, timeout):
    value = step["value"]
    if step["action"] == "storage":
        try:
            page.wait_for_function(
                """([url, area, key, expected]) =>
                location.origin === new URL(url).origin && (area === 'local' ? localStorage : sessionStorage).getItem(key) === expected""",
                arg=[url, value["area"], value["key"], value["equals"]],
                timeout=timeout,
            )
        except Exception as error:
            raise ValueError(
                f"Expected {value['area']} storage condition for key {value['key']!r}"
            ) from error
    else:
        try:
            page.wait_for_function(
                "([url,path]) => location.origin === new URL(url).origin && location.pathname === path",
                arg=[url, value],
                timeout=timeout,
            )
        except Exception as error:
            raise ValueError(f"Expected browser pathname {value}") from error


def assert_element(locator, action, value, timeout):
    from playwright.sync_api import expect

    if action == "attribute":
        if value["equals"] is None:
            expect(locator).to_have_count(1, timeout=timeout)
            expect(locator).not_to_have_attribute(
                value["name"], re.compile(r"[\s\S]*"), timeout=timeout
            )
        else:
            expect(locator).to_have_attribute(
                value["name"], value["equals"], timeout=timeout
            )
    elif action == "accessible_name":
        expect(locator).to_have_accessible_name(value, timeout=timeout)
    elif action == "accessible_description":
        expect(locator).to_have_accessible_description(value, timeout=timeout)
    elif action == "in_viewport":
        expect(locator).to_be_in_viewport(ratio=value or 0, timeout=timeout)
    elif action == "select":
        locator.select_option(value=value, timeout=timeout)
    else:
        locator.uncheck(timeout=timeout)


class NetworkObservations:
    """Count traffic for one case and consume only matching same-origin responses."""

    def __init__(self, context, url, config, steps, issue):
        self.context = context
        self.origin = origin(url)
        self.require_success = config.get("network", {}).get("require_success", False)
        self.offline = config.get("offline", False)
        self.issue = issue
        self.by_type = Counter()
        self.statuses = Counter()
        self.failures = 0
        self.available = {
            (s["value"]["method"], s["value"]["path"], s["value"]["status"]): 0
            for s in steps
            if s["action"] == "response"
        }
        context.on("request", self.request)
        context.on("response", self.response)
        context.on("requestfailed", self.failed)

    def request(self, req):
        if urlsplit(req.url).scheme in ("http", "https"):
            self.by_type[req.resource_type] += 1
            if self.offline:
                self.issue(
                    "offline_request", "External request during offline playback"
                )

    def failed(self, req):
        parts = urlsplit(req.url)
        if parts.scheme in ("http", "https"):
            self.failures += 1
            if self.require_success:
                self.issue(
                    "network",
                    f"Request failed ({req.resource_type}) {parts.path[:240]}",
                )

    def key(self, response):
        if origin(response.url) == self.origin:
            return response.request.method, urlsplit(response.url).path, response.status
        return None

    def response(self, res):
        parts = urlsplit(res.url)
        if parts.scheme not in ("http", "https"):
            return
        self.statuses[f"{res.status // 100}xx"] += 1
        if self.available:
            key = self.key(res)
            if key in self.available:
                self.available[key] += 1
        if res.status >= 400 and self.require_success:
            self.issue(
                "network",
                f"HTTP {res.status} ({res.request.resource_type}) {parts.path[:240]}",
            )

    def consume(self, value, timeout):
        key = value["method"], value["path"], value["status"]
        if self.available[key] == 0:
            try:
                self.context.wait_for_event(
                    "response",
                    predicate=lambda res: self.key(res) == key,
                    timeout=timeout,
                )
            except Exception as error:
                raise ValueError(
                    f"Expected HTTP {key[0]} {key[1]} returning {key[2]}"
                ) from error
        self.available[key] -= 1

    def summary(self):
        return {
            "requests": self.by_type.total(),
            "by_type": dict(self.by_type),
            "response_classes": dict(self.statuses),
            "failed_requests": self.failures,
        }
