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


def discover(repository, objective, worker, rescue=None, python=None):
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
    python_tests = [
        p
        for p in blobs
        if ("/" not in p or p.startswith("tests/"))
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
        protected.update(p for p in blobs if PurePosixPath(p).name == "conftest.py")
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
    js_ext = (".js", ".mjs", ".cjs")
    js_files = [
        p
        for p in blobs
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
                raise ValueError(
                    "package.json declares a placeholder test script. Set a real test command or use an explicit plan."
                )
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
    all_projects = sorted(p for p in blobs if p.endswith(".csproj"))
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
    if not gates:
        raise ValueError(
            "No supported committed test suite found. Use a Nightweaver template with an explicit check."
        )
    if len(protected) > 4096:
        raise ValueError(
            "The discovered suite exceeds 4096 protected files. Use a scoped template."
        )
    return {
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
