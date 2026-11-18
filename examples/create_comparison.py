"""Create a disposable Prompt Atlas search task and three explicitly labeled contenders."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent


CHECKS = '''
from copy import deepcopy
import sys
from search import normalize_query, search_prompts

assert normalize_query("  CINEMATIC\\t Light ") == "cinematic light"
assert normalize_query("ＳＣＥＮＥ  Straße") == "scene strasse"
assert normalize_query("") == ""
try:
    normalize_query(None)
except TypeError:
    pass
else:
    raise AssertionError("Non-string queries must raise TypeError")

if "--final" in sys.argv:
    rows = [
        {"id": "z", "title": "Ocean", "tags": ["Cinematic", "BLUE"]},
        {"id": "b", "title": "Blue City", "tags": ["cinematic"]},
        {"id": "a", "title": "Blue City", "tags": []},
        {"id": "z", "title": "Ignored duplicate", "tags": ["wrong"]},
        {"id": "u", "title": "ＳＣＥＮＥ Straße", "tags": ["night"]},
    ]
    original = deepcopy(rows)
    assert search_prompts(rows, "blue cinematic") == ["b", "z"]
    assert search_prompts(rows, "") == ["a", "b", "z", "u"]
    assert search_prompts(rows, "ＢＬＵＥ", limit=1) == ["a"]
    assert search_prompts(rows, "scene STRASSE") == ["u"]
    assert search_prompts(rows, "wrong") == []
    assert search_prompts(rows, "not present") == []
    assert search_prompts(rows, "blue", limit=0) == []
    assert rows == original, "Input records were mutated"
    for limit in (-1, True, 1.5):
        try:
            search_prompts(rows, "", limit=limit)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError("Invalid result limit accepted")
print("Independent search contract passed")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="A new directory; existing paths are never overwritten")
    parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 21))
    parser.add_argument("--codex-model")
    parser.add_argument("--claude-model")
    parser.add_argument("--claude-profile", type=Path)
    parser.add_argument("--agtx-skill", type=Path, help="Installed upstream skills/oneshot/SKILL.md; compare AGTX, Codex and Claude")
    parser.add_argument("--agtx-distro", default="AGTX-Pilot")
    parser.add_argument("--also-codex-coordinator", action="store_true", help="Include a separately labeled Codex-hosted OneShot")
    parser.add_argument("--seconds", type=int, default=600, help="Shared per-workflow timeout in AGTX comparisons")
    args = parser.parse_args()
    if args.also_codex_coordinator and not args.agtx_skill:
        parser.error("--also-codex-coordinator requires --agtx-skill")
    if not 30 <= args.seconds <= 14400:
        parser.error("--seconds must be between 30 and 14400")
    folder = args.directory.resolve()
    folder.mkdir(parents=True)
    repo = folder / "project"
    repo.mkdir()
    (repo / "search.py").write_text('def normalize_query(value):\n    raise NotImplementedError\n\n'
                                   'def search_prompts(records, query, limit=10):\n    raise NotImplementedError\n', encoding="utf-8")
    (repo / "check.py").write_text(dedent(CHECKS).lstrip(), encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for argv in (["init", "-b", "main"], ["config", "user.name", "Nightweaver Benchmark"],
                 ["config", "user.email", "benchmark@example.invalid"], ["add", "."], ["commit", "-m", "Frozen search baseline"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, stdout=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    codex, claude = {"kind": "codex"}, {"kind": "claude"}
    if args.codex_model:
        codex["model"] = args.codex_model
    if args.claude_model:
        claude["model"] = args.claude_model
    if args.claude_profile:
        claude["profile_dir"] = str(args.claude_profile.resolve(strict=True))
    plan = {
        "repository": "project", "title": "Prompt Atlas search contract",
        "objective": "Implement the two pure Python search functions in search.py. This is a disposable library benchmark, not the user's Prompt Atlas project.",
        "worker": codex, "attempts": 2, "agent_timeout": 120,
        "budgets": {"max_seconds": 360, "max_calls": 6, "max_log_bytes": 4194304},
        "protected_paths": ["check.py", ".gitignore"], "context_files": ["search.py"],
        "gates": [{"name": "Normalization contract", "argv": [sys.executable, "check.py"]}],
        "final_gates": [{"name": "Complete search contract", "argv": [sys.executable, "check.py", "--final"]}],
        "milestones": [
            {"title": "Normalize queries", "paths": ["search.py"],
             "task": "Implement normalize_query(value): require a string or raise TypeError; use Unicode NFKC, then casefold, then collapse all whitespace to single spaces and strip edges. Empty input returns an empty string. Keep search_prompts importable."},
            {"title": "Search and rank prompts", "paths": ["search.py"],
             "task": "Implement search_prompts(records, query, limit=10). Input records have string id/title and a list of string tags. Keep only the first record for each id before matching. Normalize query, title and tags using normalize_query. Every query word must appear as a substring of the normalized title plus tags joined with spaces. Empty query matches all. Sort by normalized title then id; return a list of ids truncated to limit. Limit must be an integer excluding bool and at least zero; raise TypeError or ValueError otherwise. Do not mutate records. Preserve normalize_query behavior."},
        ],
    }
    suite = {"name": "Prompt Atlas search workflow pilot", "seed": 42, "repetitions": args.repetitions,
             "tasks": [{"name": "Unicode search and ranking", "plan": "plan.json"}],
             "contenders": [
                 {"name": "Codex standalone", "mode": "standalone", "worker": codex},
                 {"name": "Claude standalone", "mode": "standalone", "worker": claude},
                 {"name": "Nightweaver Codex + Claude review", "worker": codex, "reviewer": claude},
             ]}
    if args.agtx_skill:
        skill = args.agtx_skill.resolve(strict=True)
        plan["agent_timeout"] = args.seconds
        plan["budgets"]["max_seconds"] = args.seconds + 45
        suite["contenders"].pop()
        hosts = ["claude", "codex"] if args.also_codex_coordinator else ["claude"]
        for host in hosts:
            argv = [sys.executable, str(Path(__file__).with_name("agtx_contender.py").resolve()),
                    "--distro", args.agtx_distro, "--host", host, "--skill", str(skill), "--seconds", str(args.seconds)]
            if host == "claude" and args.claude_profile:
                argv.extend(["--profile", str(args.claude_profile.resolve(strict=True))])
            model = args.claude_model if host == "claude" else args.codex_model
            if model:
                argv.extend(["--model", model])
            suite["contenders"].append({"name": f"AGTX OneShot ({host} coordinator, Codex workers)", "mode": "external",
                                        "worker": {"kind": "command", "argv": argv, "unavailable_exit_code": 78}})
    for name, value in (("plan.json", plan), ("suite.json", suite)):
        (folder / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(folder / "suite.json")


if __name__ == "__main__":
    main()
