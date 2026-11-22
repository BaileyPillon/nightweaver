"""Build a runnable OneShot template from the committed, conventional test suites."""

import ast
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from .engine import checkout, git, git_bytes
from .spec import strict_json


def verify_tools(gates):
    for gate in gates:
        if gate.get("kind") == "browser":
            from .readiness import browser_readiness

            browser = browser_readiness()
            if browser["status"] == "unavailable":
                raise ValueError(
                    f"{gate['name']} needs a browser available before starting this OneShot. {browser['action']}"
                )
            continue
        argv = gate["argv"]
        wrapped = gate["name"].startswith(".NET ") or gate["name"] == "npm test"
        executable = argv[4] if wrapped else argv[0]
        if not shutil.which(executable):
            raise ValueError(
                f"{gate['name']} needs {executable} available before starting this OneShot"
            )
        if gate["name"] == "Python pytest":
            probe = subprocess.run(
                [
                    executable,
                    "-P",
                    "-c",
                    "import importlib.util, sys; sys.exit(importlib.util.find_spec('pytest') is None)",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if probe.returncode:
                raise ValueError(
                    f"Install pytest in {executable} or select the repository interpreter with --python"
                )


def unittest_roots(files, blobs):
    folders = {PurePosixPath(p).parent for p in files}
    roots = []
    for folder in sorted(folders, key=lambda p: (len(p.parts), p.as_posix())):
        parent = folder
        while (
            parent not in roots
            and parent != parent.parent
            and (parent / "__init__.py").as_posix() in blobs
        ):
            parent = parent.parent
        if parent not in roots:
            roots.append(folder)
    return [p.as_posix() for p in roots]


def needs_pytest(source, filename):
    try:
        module = ast.parse(source, filename)
    except SyntaxError as error:
        raise ValueError(
            f"Cannot discover Python tests in {filename}: {error}"
        ) from error
    function = (ast.FunctionDef, ast.AsyncFunctionDef)
    if filename.endswith("_test.py") or any(
        isinstance(n, function) and n.name.startswith("test") for n in module.body
    ):
        return True
    if any(
        isinstance(n, ast.ClassDef)
        and n.name.startswith("Test")
        and not n.bases
        and any(
            isinstance(method, function) and method.name.startswith("test")
            for method in n.body
        )
        for n in module.body
    ):
        return True
    return any(
        (isinstance(n, ast.ImportFrom) and n.module == "pytest")
        or (isinstance(n, ast.Import) and any(a.name == "pytest" for a in n.names))
        for n in ast.walk(module)
    )


def node_installation():
    return Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs"


def node_command():
    native = node_installation() / "node.exe"
    return shutil.which("node") or (str(native) if native.is_file() else "node")


def npm_command():
    npm = shutil.which("npm")
    if npm and Path(npm).suffix.lower() == ".exe":
        return [npm, "test"]
    folders = [Path(npm).parent] if npm else []
    folders.append(node_installation())
    for folder in folders:
        npm_cli = folder / "node_modules/npm/bin/npm-cli.js"
        sibling = folder / "node.exe"
        node = str(sibling) if sibling.is_file() else shutil.which("node")
        if npm_cli.is_file() and node:
            return [node, str(npm_cli), "test"]
    return None


STATIC_PAGES = ("index.html", "public/index.html", "docs/index.html")
SERVED_SCRIPTS = ("dev", "start", "preview", "serve")
# Files a brand-new repository starts with. Anything else means the project has its own shape.
INERT_FILES = re.compile(
    r"(?i)(?:readme|licen[cs]e|copying|notice)(?:\.[a-z]+)?|\.(?:gitignore|gitattributes|editorconfig|gitkeep|keep)"
)
# What a new repository's goal names. The deliverable is the noun phrase that opens the objective ("Build a landing page",
# "Write a Rust library"): a tool, library or script is never a page, whatever it handles, and a game or calculator is one
# unless a language or engine says otherwise.
HEAD_END = re.compile(
    r"(?i)[.;:,!?\n(]|\s(?:that|which|who|whose|where|when|to|for|with|using|in|on|at|by|from|so|and|but|because|if|about|of)\s"
)
NON_WEB = re.compile(
    r"(?i)\b(?:cli|command[- ]?line|terminals?|consoles?|scripts?|librar(?:y|ies)|packages?|modules?|crates?|sdk|tkinter|curses|pyqt\d*"
    r"|pyside\d*|wxpython|pygame|unity|unreal|godot|scrapers?|crawlers?|parsers?|bots?|daemons?|services?|apis?|servers?|plugins?"
    r"|extensions?|compilers?|interpreters?|desktop|android|ios|notebooks?|jupyter|text[- ]based|text adventure|algorithms?|linters?"
    r"|formatters?)\b"
)
WEB_NOUN = re.compile(
    r"(?i)\b(?:web ?(?:sites?|pages?|apps?|applications?|ui|games?|design)|html5?|css|browsers?|landing|dashboards?|canvas|svg|webgl"
    r"|front-?end|responsive|stylesheets?|portfolios?|single[- ]page)\b"
)
WEB_SOFT = re.compile(
    r"(?i)\b(?:pages?|sites?|ui|ux|games?|calculators?|to-?dos?|tetris|snake|tic-?tac-?toe|quiz|clock|timer|gallery|animations?)\b"
)
LANGUAGE = re.compile(r"(?i)(?<!\w)(?:python|rust|golang|java|kotlin|swift|haskell|perl|lua|bash|powershell|shell|c\+\+|c#|scala|elixir|zig)(?!\w)")
WEB_PHRASE = re.compile(r"(?i)\b(?:in (?:a|the|your) browser|browser[- ]based|web ?apps?|landing pages?|single[- ]page|static (?:web ?)?sites?)\b")
CODE_SUFFIXES = frozenset(
    ".py .js .mjs .cjs .ts .mts .cts .tsx .jsx .go .rs .rb .php .java .kt .kts .scala .sc .groovy .cs .fs .fsx .vb .swift .c .cc .cpp .h "
    ".hpp .ex .exs .erl .hs .clj .cljs .dart .lua .pl .pm .r .jl .ml .mli .elm .cr .nim .zig .rkt .scm .lisp .sol .gd .sh .bash .ps1".split()
)
JS_SUFFIXES = frozenset(".js .mjs .cjs .jsx .ts .mts .cts .tsx".split())
# These suffixes only ever hold tests. A file in a test folder counts whatever it is, except documents, pictures and fonts.
TEST_SUFFIXES = frozenset(".bats .robot .feature .hurl .t".split())
ASSET_SUFFIXES = frozenset(
    ".md .txt .rst .png .jpg .jpeg .gif .webp .ico .svg .bmp .pdf .woff .woff2 .ttf .otf .eot .mp3 .mp4 .webm .ogg .wav .css .scss".split()
)
TEST_STEM = re.compile(r"(?i)(?:tests?|test[_.-].*|.*[_.-](?:tests?|spec)|.*\.(?:test|spec))")
CAMEL_TEST_STEM = re.compile(r".*[a-z0-9](?:Tests?|Spec)|Test[A-Z0-9_].*")
TEST_FOLDERS = frozenset({"test", "tests", "__tests__"})
SPEC_FOLDERS = frozenset({"spec", "specs", "e2e", "cypress"})
# A script named like a test may be a feature (typing-test.js, ab-test.js). It counts when its text calls a test runner.
TEST_CODE = re.compile(
    r"""(?x)(?<![.\w$])(?:(?:describe|it|test|suite|context)(?:\.\w+)?\s*\(\s*['"`]|expect\s*\(|assert(?:\.\w+)?\s*\(|QUnit\.|jasmine\.)"""
    r"""|\b(?:from|require\s*\()\s*['"](?:node:test|node:assert|assert|vitest|jest|mocha|chai|ava|tap|uvu|bun:test|@playwright/test|cypress)\b"""
)
# Build or test manifests of ecosystems this discovery does not run. Their presence means the project has tests of its own.
FOREIGN_MANIFESTS = frozenset(
    "Cargo.toml pom.xml build.gradle build.gradle.kts settings.gradle settings.gradle.kts build.sbt composer.json mix.exs "
    "CMakeLists.txt meson.build build.zig Package.swift deno.json deno.jsonc tox.ini pytest.ini .pytest.ini pytest.toml .pytest.toml "
    "noxfile.py Makefile.PL Build.PL cpanfile rebar.config stack.yaml .rspec jasmine.json".split()
)
FOREIGN_PROJECTS = (".sln", ".csproj", ".fsproj", ".vbproj", ".cabal")
FOREIGN_CONFIGS = tuple(
    f"{runner}.*" for runner in ("jest.config", "vitest.config", "vitest.workspace", "playwright.config", "cypress.config", "karma.conf",
                                 ".mocharc", "wdio.conf", "ava.config")
)
# Manifests that also describe projects with no code to test (a Hugo module is a go.mod, a Jekyll site a Gemfile), so they count with code beside them.
# (A Gemfile that names a test gem counts on its own, below.)
SOURCE_MANIFESTS = {"go.mod": ".go", "Gemfile": ".rb"}
# Files that count as evidence only when their text names a test runner or a test target.
CONFIGURED_TESTS = {
    name: re.compile(r"(?im)^(?:tests?|check|test[-_.][\w.-]*)\s*:")
    for name in ("makefile", "gnumakefile", "justfile")
} | {name: re.compile(r"(?i)\b(?:pytest|tox|nox|unittest)\b") for name in ("pyproject.toml", "setup.cfg")} | {
    "gemfile": re.compile(r"(?i)\b(?:rspec|minitest|test-unit|cucumber|capybara|rake|rails|sinatra)\b"),
    "rakefile": re.compile(r"(?i)\b(?:rspec|minitest|testtask|task\s*\(?\s*:(?:test|spec))\b"),
    "package.json": re.compile(
        r'(?i)"(?:jest|vitest|mocha|jasmine|ava|karma|tap|uvu|nightwatch|testcafe|cypress|webdriverio|@playwright/test|playwright)"'
        r'|"(?:test[:._-][\w.:-]*|e2e[\w.:-]*)"\s*:'
    ),
    "taskfile.yml": re.compile(r"(?im)^\s+(?:tests?|check|test[-_:.][\w.:-]*)\s*:"),
    "taskfile.yaml": re.compile(r"(?im)^\s+(?:tests?|check|test[-_:.][\w.:-]*)\s*:"),
}


def web_goal(objective):
    """True when the objective reads as a web deliverable. A tool, library or script never does; a game or calculator does unless
    a language or engine is named. Everything else is false, so a new repository asks for --web instead of building the wrong page."""
    sentence = re.split(r"[.;\n]", objective.strip(), maxsplit=1)[0]
    head = HEAD_END.split(sentence, 1)[0]
    if NON_WEB.search(head):
        return False
    if WEB_NOUN.search(head) or (WEB_SOFT.search(head) and not LANGUAGE.search(sentence)):
        return True
    return bool(WEB_PHRASE.search(objective))


def served_by_script(scripts):
    return isinstance(scripts, dict) and any(
        isinstance(scripts.get(name), str) and scripts[name].strip() for name in SERVED_SCRIPTS
    )


def test_marks(path):
    """'yes' for a test file or a build or test manifest of an ecosystem discovery cannot run, 'maybe' for a script named like a
    test (its text decides, because typing-test.js and ab-test.js are features), else None."""
    pure = PurePosixPath(path)
    name, suffix, folders = pure.name.lower(), pure.suffix.lower(), {part.lower() for part in pure.parts[:-1]}
    if (pure.name in FOREIGN_MANIFESTS or suffix in FOREIGN_PROJECTS or suffix in TEST_SUFFIXES
            or any(fnmatch.fnmatchcase(name, pattern) for pattern in FOREIGN_CONFIGS)):
        return "yes"
    if not TEST_FOLDERS.isdisjoint(folders) and suffix not in ASSET_SUFFIXES and not INERT_FILES.fullmatch(pure.name):
        return "yes"  # Whatever the language, a file in tests/ belongs to the tests.
    if suffix not in CODE_SUFFIXES:
        return None
    named = TEST_STEM.fullmatch(pure.stem) or CAMEL_TEST_STEM.fullmatch(pure.stem) or not SPEC_FOLDERS.isdisjoint(folders)
    if suffix in JS_SUFFIXES:
        return "yes" if re.fullmatch(r".*\.(?:test|spec)", pure.stem, re.IGNORECASE) else "maybe" if named else None
    return "yes" if named else None


def foreign_tests(sources, js_files, contents):
    """Paths showing tests that discovery found but cannot run, sorted. None means a smoke gate cannot replace anything."""
    found, suffixes = set(js_files), {PurePosixPath(p).suffix.lower() for p in sources}
    for path in sources:
        name = PurePosixPath(path).name
        kind = test_marks(path)
        if kind == "yes" or SOURCE_MANIFESTS.get(name) in suffixes:
            found.add(path)
        elif (pattern := CONFIGURED_TESTS.get(name.lower()) or (TEST_CODE if kind else None)) is not None:
            try:
                if pattern.search(contents(path)):
                    found.add(path)
            except ValueError:
                found.add(path)  # Too large or not text: tests cannot be ruled out.
    return sorted(found)


def static_page(blobs, scripts, web=None):
    """The page a browser smoke check can load for a web repository without tests, or None."""
    if served_by_script(scripts):
        return None  # A bundler or framework serves this project, so a bare file server cannot render its index.html.
    page = next((p for p in STATIC_PAGES if p in blobs), None)
    if page is None and web and new_repository(blobs):
        page = "index.html"  # A new repository whose goal is a web page: the worker creates it.
    return page


def new_repository(blobs):
    return all(INERT_FILES.fullmatch(p) for p in blobs)


def page_root(page):
    """Where a static page is served from. A site kept in public/ or docs/ is deployed with that folder as its root."""
    folder = PurePosixPath(page).parent.as_posix()
    return None if folder == "." else folder


def smoke_gate(page):
    root = page_root(page)
    return {"name": "Browser smoke", "kind": "browser",
            "browser": {"page": page, **({"root": root} if root else {}), "sweep": {}}}


class NewRepository(ValueError):
    """A brand-new repository whose goal is not a web page has nothing to check, and a smoke gate must not stand in."""


def missing_suite(scripts, unrun=(), new=False):
    if served_by_script(scripts):
        return (
            "No supported committed test suite found. This web project is served by a package.json script, which OneShot "
            "cannot start by itself. Use a Nightweaver template with a browser check that owns the server, for example "
            '{"name": "Browser smoke", "kind": "browser", "browser": {"server": {"argv": ["<executable>", "<server script>", "{port}"]}}}.'
        )
    if unrun:
        return (
            f"No supported committed test suite found. This repository has tests or a build OneShot cannot run ({', '.join(unrun[:4])}"
            f"{', and more' if len(unrun) > 4 else ''}), so it will not stand in a browser smoke check for them. Use a Nightweaver "
            'template with a gate that runs them, for example {"name": "Project tests", "argv": ["<test command>"]}.'
        )
    if new:
        return (
            "No supported committed test suite found. This repository is new and the goal does not name a web page, so there is "
            "nothing to check yet. Use a Nightweaver template with an explicit check, or pass --web if the goal is a web page."
        )
    return "No supported committed test suite found. Use a Nightweaver template with an explicit check."


def discover(repository, objective, worker, rescue=None, python=None, web=None, notes=None):
    """`web` says the goal is a web page. Only a brand-new repository needs it: None and False give it no smoke gate and
    raise NewRepository. A list in `notes` collects what the caller should tell the operator about a guess it made."""
    root, _ = checkout(Path(repository).resolve(strict=True))
    interpreter = (
        str(Path(python).expanduser().resolve(strict=True))
        if python
        else sys.executable
    )
    if not objective.strip():
        raise ValueError("Give the OneShot a nonempty project objective")
    base = git(root, "rev-parse", "HEAD^{commit}")
    entries = git_bytes(root, "ls-tree", "-r", "-z", base)
    blobs = {}
    for entry in entries.rstrip(b"\0").split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        if kind == b"blob" and mode in (b"100644", b"100755"):
            blobs[path.decode("utf-8")] = oid.decode("ascii")

    def contents(path):
        oid = blobs[path]
        if int(git(root, "cat-file", "-s", oid)) > 1_000_000:
            raise ValueError(
                f"Verifier discovery input exceeds 1MB: {path}. Use an explicit plan."
            )
        return git_bytes(root, "cat-file", "blob", oid).decode("utf-8-sig")

    runner = str(Path(__file__).with_name("suite_runner.py"))
    gates, protected = [], set()
    excluded = {
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "third_party",
        "dist",
        "build",
        "obj",
    }
    sources = [p for p in blobs if excluded.isdisjoint(PurePosixPath(p).parts[:-1])]
    python_tests = [
        p
        for p in sources
        if (
            "/" not in p
            or p.startswith(("tools/", "scripts/"))
            or not {"test", "tests"}.isdisjoint(PurePosixPath(p).parts[:-1])
        )
        and (
            fnmatch.fnmatchcase(PurePosixPath(p).name, "test*.py")
            or p.endswith("_test.py")
        )
    ]
    if python_tests:
        markers = {
            "pytest.ini",
            ".pytest.ini",
            "pytest.toml",
            ".pytest.toml",
        } & blobs.keys()
        manifests = {"pyproject.toml", "setup.cfg", "tox.ini"} & blobs.keys()
        uses_pytest = (
            bool(markers)
            or (
                "pyproject.toml" in manifests
                and "pytest"
                in tomllib.loads(contents("pyproject.toml")).get("tool", {})
            )
            or any(
                re.search(r"(?im)^\s*\[(?:tool:)?pytest\]\s*$", contents(p))
                for p in manifests - {"pyproject.toml"}
            )
            or any(needs_pytest(contents(p), p) for p in python_tests)
        )
        protected.update(python_tests)
        protected.update(markers | manifests)
        protected.update(p for p in sources if PurePosixPath(p).name == "conftest.py")
        if uses_pytest:
            gates.append(
                {
                    "name": "Python pytest",
                    "argv": [
                        interpreter,
                        "-B",
                        "-m",
                        "pytest",
                        "-p",
                        "no:cacheprovider",
                        "--junitxml={junit}",
                    ],
                    "junit": {"min_passed": 1},
                }
            )
        else:
            # Discover root tests without losing a non-package tests/ directory.
            for folder in unittest_roots(python_tests, blobs):
                gates.append(
                    {
                        "name": "Python unittest " + folder,
                        "argv": [interpreter, "-B", runner, "unittest", folder],
                    }
                )
    scripts, placeholder = {}, None
    js_ext = (".js", ".mjs", ".cjs")
    js_files = [
        p
        for p in sources
        if p.endswith(js_ext)
        and (
            "test" in PurePosixPath(p).parts
            or "tests" in PurePosixPath(p).parts
            or ".test." in p
            or ".spec." in p
        )
    ]
    if js_files or "package.json" in blobs:
        package = (
            strict_json(contents("package.json")) if "package.json" in blobs else {}
        )
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        declared = scripts.get("test") if isinstance(scripts, dict) else None
        native = [
            p for p in js_files if re.search(r"""['"]node:test['"]""", contents(p))
        ]
        if declared == "node --test" or (native and declared is None):
            files = [] if declared else sorted(native)
            gates.append(
                {
                    "name": "Node tests",
                    "argv": [
                        node_command(),
                        "--test",
                        "--test-reporter=junit",
                        "--test-reporter-destination={junit}",
                        *files,
                    ],
                    "junit": {"min_passed": 1},
                }
            )
            protected.update(js_files)
            protected.update({"package.json", "package-lock.json"} & blobs.keys())
        elif isinstance(declared, str) and declared.strip():
            if "no test specified" in declared.lower() or re.fullmatch(
                r"(?:echo\b[^&|;]*|true|exit\s+0)", declared.strip(), re.IGNORECASE
            ):
                placeholder = "package.json declares a placeholder test script. Set a real test command or use an explicit plan."
            else:
                argv = npm_command()
                if not argv:
                    raise ValueError(
                        "The committed npm test script needs a native Node/npm installation. Use an explicit plan for another package manager."
                    )
                gates.append(
                    {
                        "name": "npm test",
                        "argv": [sys.executable, "-B", runner, "npm", *argv],
                    }
                )
                protected.update(js_files)
                script = re.fullmatch(
                    r"""node\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))""", declared.strip()
                )
                if script:
                    entry = PurePosixPath(
                        next(value for value in script.groups() if value).replace("\\", "/")
                    ).as_posix()
                    if entry in blobs:
                        protected.add(entry)
                protected.update({"package.json", "package-lock.json"} & blobs.keys())
    projects = []
    all_projects = sorted(p for p in sources if p.endswith(".csproj"))
    project_dirs = {PurePosixPath(p).parent for p in all_projects}
    for path in all_projects:
        try:
            xml = ET.fromstring(contents(path))
        except ET.ParseError as error:
            raise ValueError(
                f"Cannot read committed .NET project {path}: {error}"
            ) from error
        if any(
            (tag.text or "").strip().lower() == "true"
            for tag in xml.findall(".//{*}IsTestProject")
        ) or any(
            e.get("Include", "").lower() == "microsoft.net.test.sdk"
            for e in xml.findall(".//{*}PackageReference")
        ):
            projects.append(path)
    for project in projects:
        folder = PurePosixPath(project).parent
        dotnet = shutil.which("dotnet")
        installed = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "dotnet/dotnet.exe"
        )
        gates.append(
            {
                "name": ".NET " + project,
                "argv": [
                    sys.executable,
                    "-B",
                    runner,
                    "dotnet",
                    dotnet or (str(installed) if installed.exists() else "dotnet"),
                    project,
                ],
            }
        )
        protected.add(project)
        protected.update(
            p
            for p in blobs
            if p.endswith(".cs")
            and next((d for d in PurePosixPath(p).parents if d in project_dirs), None)
            == folder
        )
    unrun = [] if gates else foreign_tests(sources, js_files, contents)
    page = None if gates or unrun else static_page(blobs, scripts, web)
    if placeholder and (gates or not page):
        raise ValueError(placeholder)
    if not gates:
        if not page:
            new = new_repository(blobs)
            raise (NewRepository if new else ValueError)(missing_suite(scripts, unrun, new))
        if notes is not None and page not in blobs:
            notes.append("This repository is new and the goal reads as a web page, so the browser smoke check expects an index.html. "
                         "Pass --no-web if it is not one.")
        # A web page without tests still gets loaded in a desktop and a phone viewport, and its visible controls get clicked, with screenshots kept as evidence.
        gates.append(smoke_gate(page))
    if len(protected) > 4096:
        raise ValueError(
            "The discovered suite exceeds 4096 protected files. Use a scoped template."
        )
    plan = {
        "repository": str(root),
        "expected_base": base,
        "title": objective.strip().splitlines()[0][:100],
        "objective": objective.strip(),
        "worker": worker,
        "worker_fallbacks": [rescue] if rescue else [],
        "attempts": 3,
        "budgets": {"max_calls": 15, "max_seconds": 3600},
        "gates": gates,
        "protected_paths": sorted(protected),
        "repository_map": True,
        "repair": {
            "collect_failures": True,
            "switch_after": 1 if rescue else 0,
            "fresh_session_after": 1,
            "history": 3,
        },
        "milestones": [{"title": "Deliver the goal", "task": objective.strip()}],
    }
    if page:
        plan["acceptance"] = [
            f"{page} exists, loads over HTTP without console or page errors and has no horizontal overflow at "
            "1440x900 (desktop) and 390x844 (phone)."
        ]
    return plan
