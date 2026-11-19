# Nightweaver

Version 0.11 can catch a lost catalog record even when the total stays the same. OneShots retain milestone-specific checks and file scopes, can preview their planner input, and can reuse a saved plan response. [All 30 ranked changes, the Afterimage control experiment and a working catalog gate](docs/v0.11-research.md)

Version 0.10 checks that every verifier observed the same candidate, tests graders against known-good and known-bad controls, and checks saved browser state across reloads. Compare contenders by distinct tasks and observed calls, or trace a slow run down to the provider call and browser step. [Thirty ranked changes, research sources and verification](docs/v0.10-research.md)

Version 0.9 rechecks evidence before accepting a milestone, adds blocking OneShot completion, and provides dependency graphs, browser coverage, and matched regression verdicts. [Ranked top 10 issues, improvements and features, with research and verification](docs/v0.9-research.md)

Version 0.8.1 checks browser availability before an immediate OneShot calls its planner. A browser outage during verification preserves the candidate for retry. Passing browser receipts must cover every configured viewport and step. [Browser recovery and evidence requirements](docs/live-stability.md#browser-outages-and-incomplete-results)

Version 0.8 brings stability checks into live OneShot runs. Set `stability_runs` on a repeatable gate to confirm an inconsistent result on the same candidate before spending another worker call. [Behavior, examples and restart recovery](docs/live-stability.md)

Version 0.7 adds pinned verifier inputs, repeatable command and browser checks on accepted commits, failure diagnostics and repeated-trial benchmark measurements. The [three ranked lists and research sources](docs/v0.7-research.md) cover all 30 changes, their checks and CLI examples.

Version 0.6 adds pinned visual comparisons, accessible and mobile browser contracts, model-free replay of accepted commits, repair measurements, and online runtime backups. Read the [ranked top twenty with research sources and commands](docs/v0.6-research.md).

Version 0.5 adds session handoffs, an operator attention queue, accepted Git diffs, offline browser journeys with named screenshots, and matched benchmark history. The [ranked features, reliability improvements and research sources](docs/v0.5-field-guide.md) include commands and an example journey.

Run a [head-to-head benchmark of AGTX OneShot, Codex and Claude Code](docs/head-to-head.md) using a frozen task, shared time limits and independent checks.

Version 0.4 adds owned application servers for browser checks, direct standalone CLI comparisons, complete standalone task handoffs, per-task matched outcomes, and explicit account/environment blockers. See the [0.4 guide and comparison starter](docs/v0.4-guide.md). Version 0.3 introduced the [ranked 10 features and 10 improvements with research sources](docs/upgrade-plan.md); its [guide](docs/v0.3-guide.md) remains applicable except where superseded.

An autonomous workshop for your code, built for Windows.

Windows-native orchestration for coding agents. Give Nightweaver a goal with checked milestones. It hands off work between agents, sends failed checks back for repair, and advances after the result passes verification. Close the desktop dashboard whenever you need to. The runner keeps its place in SQLite.

## What runs on Windows

- A WPF dashboard for starting a plan or OneShot, watching attempts, opening evidence, and pausing or resuming runs.
- Native Codex, Claude Code, and DeepSeek adapters. Use your existing CLI logins or select a specific installed profile for a role. DeepSeek runs through Claude Code's tools with a separate API key and configuration directory. An optional local Ollama model supplies advisory feedback. You can also plug in a custom executable.
- A pinned Git baseline and a separate worktree per milestone. Accepted commits land on `nightweaver/<run-id>` for inspection or integration.
- Independent operator-defined checks, with optional required screenshots or other files and a separate reviewer. Check receipts bind artifacts and decisions to a Git tree. Failed checks feed the next attempt automatically.
- A configured reviewer checks the whole objective at the final milestone and can send unfulfilled requirements back for repair.
- A background runner with Windows Job Objects for child process cleanup. Relaunch a failed or interrupted runner against the same durable state, worktree, and recorded provider session where available.
- A persistent queue with run prerequisites, recovery after interruption, and an optional dispatcher launched by Windows Task Scheduler at sign-in.

## Try it without model accounts

Install Git for Windows and Python 3.12 or later. Clone the repo and run these commands in PowerShell from its root.

```powershell
$demo = Join-Path $env:TEMP ('nightweaver-demo-' + [guid]::NewGuid().ToString('N'))
$plan = python examples/create_demo.py $demo
$run = python -m nightweaver add $plan --start | ConvertFrom-Json
python -m nightweaver status $run.run_id
python -m nightweaver events $run.run_id
```

The first worker intentionally misses the title check. Nightweaver passes the actual failure back to the worker, verifies the correction, and starts the summary milestone. Inspect the accepted result with `git -C "$demo/project" log "nightweaver/$($run.run_id)" --oneline` when the run reaches `done`.

With signed-in Codex and Claude Code installed, pass `--live` to the demo generator to use Codex for the title and Claude for the summary. Add `--ollama <installed-model>` to include a local Ollama advisory response. `python -m nightweaver doctor` lists detected executables. The engine does not require WSL.

## Use DeepSeek

Install the native Claude Code CLI. Nightweaver uses it as DeepSeek's tool and session harness, following [DeepSeek's official integration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/). Model requests use your DeepSeek API account and its balance. A Claude subscription is not required for this provider.

Save your API key in a local text file outside the repository, then configure its location in PowerShell.

```powershell
python -m nightweaver configure deepseek --key-file "$env:USERPROFILE\Desktop\deepseek-api-key.txt"
```

Nightweaver stores the file path in its private runtime settings. It reads the key when starting a DeepSeek process, passes credentials through that process's environment, and uses a dedicated `profiles/deepseek` configuration directory. Keep the key file in place. `DEEPSEEK_API_KEY`, `DEEPSEEK_API_KEY_FILE`, or a provider's `api_key_file` are also supported. Inline `api_key` values in plans are rejected.

Use `{"kind":"deepseek"}` for any worker or reviewer. The default model is `deepseek-flash`. To select another model available to your account, set `model`, for example `{"kind":"deepseek","model":"deepseek-v4-pro"}`. Each milestone may choose its own provider, so Codex, Claude, and DeepSeek can work on successive steps of the same goal.

```powershell
python -m nightweaver goal template.json "Your goal" --planner deepseek --start
```

DeepSeek is also available in the desktop OneShot planner dropdown. The CLI accepts `--model` for the planner. The template still selects workers and reviewers independently. For a small live test, pass `--deepseek` to `examples/create_demo.py`. This uses DeepSeek for both milestones and a fresh independent review after each.

## Open the desktop dashboard

Install the .NET 8 SDK with the Windows Desktop runtime and run the following. Then select **Start a plan** and choose the generated `plan.json`, or use **New OneShot** with a template and an objective. Set `NIGHTWEAVER_PYTHON` to the path of your Python executable if `python.exe` is unavailable on `PATH`.

```powershell
dotnet run --project Nightweaver.Desktop -c Release
```

The compiled desktop app accepts `--home <runtime-directory>` and `--python <python.exe>` for shortcuts that need explicit paths.

## Use it on a project

Choose the repository root with the committed base you want. Set `repository`, `title`, `objective`, `worker`, `gates`, and ordered `milestones` in a local JSON plan. A milestone can specify a different `worker`, its own extra `gates`, and its allowed `paths`. `protected_paths` defends tracked checks and project boundaries during promotion. Run with trusted plans and projects. Custom commands run with your Windows permissions, and provider tool calls use the provider's permissions.

Set `expected_base` to a full Git commit hash to reject the wrong starting version. Nightweaver records the resolved checkout and serializes runs that share a Git directory, including linked worktrees.

### Register the canonical checkout

```powershell
python -m nightweaver project bind journal 'C:\Projects\field-journal'
python -m nightweaver project list
```

Set `"project": "journal"` in a plan or OneShot template to use that checkout. Omit `repository`, or supply it as an explicit assertion that must match the binding. A second clone is rejected even at the same commit. Bindings live in the selected Nightweaver runtime. Use `project bind journal <new-root> --replace` to redirect future runs. Already registered runs keep their original checkout and base.

- **OneShot.** Create a template with the repository, title, worker, and trusted checks. `python -m nightweaver goal template.json "Your goal" --planner claude --start` makes a fresh plan of up to five milestones and starts it. Omit `--start` to print the proposed plan first. Use `--profile-dir` to select an existing planner configuration directory.
- **Checks.** Each gate has a `name`, an executable `argv` array, an optional `timeout` in seconds, and an optional `artifacts` array of paths relative to the worktree. Put whole-project checks under `final_gates`. These run on the final candidate in addition to its regular checks. Use the `acceptance` array for requirements the worker and reviewer should carry across milestones, including visual criteria.
- **Profiles.** The `codex` and `claude` workers and reviewers accept `model`, `executable`, and `profile_dir`. Log into each chosen profile with its own CLI. The engine passes the matching configuration directory to that process. Use `reviewer` with `{"kind":"claude"}` or `{"kind":"codex"}` to require a fresh review. A custom `command` reviewer receives the brief on stdin and must emit a JSON `verdict` (`PASS` or `REVISE`) and `reason`.
- **Recovery.** `python -m nightweaver resume <run-id>` starts an interrupted or paused run again. After correcting an external blocker, use `resume <run-id> --retry`. Interrupted workers recover without using up a repair attempt, and their original attempt logs remain available. Use the dashboard or `events` to inspect the run.

### Check provider readiness

```powershell
python -m nightweaver doctor --plan plan.json
python -m nightweaver doctor --plan plan.json --planner deepseek --live
python -m nightweaver doctor --planner claude --profile-dir 'C:\Profiles\claude-work' --live
```

The report resolves the plan's checkout and identifies which executable, profile and model each role will use. A non-live report checks local configuration. With `--live`, Nightweaver sends a small test prompt per distinct model route from an empty directory. A successful model reply confirms access at the time of the check. Live requests use your normal subscription limits or API balance. Ollama is checked by listing local models. Custom commands are checked for executable availability and run only as part of the plan.

The command exits with status 3 if a required model route is blocked or a live request could not be verified. On a run, a missing CLI, missing selected profile, or recognized account, model, or usage-limit error blocks with the affected role and profile. Correct that route and retry the existing run. [Claude profile settings](https://code.claude.com/docs/en/settings) · [Ollama model listing](https://docs.ollama.com/api/tags)

## Queue and recover runs on Windows

```powershell
$first = python -m nightweaver add prepare.json --enqueue | ConvertFrom-Json
python -m nightweaver add verify.json --enqueue --after $first.run_id
python -m nightweaver dispatch
```

`dispatch` processes eligible runs once. `dispatch --watch` keeps checking for new or interrupted runs. The dispatcher skips explicitly paused and blocked runs. Use `--after` more than once to wait for multiple prerequisites in the same runtime. A prerequisite controls order. Each run still starts from its own pinned source commit.

To start the watcher automatically when you sign in, register and start the optional task below. It runs as your Windows user and uses the repo and runtime paths captured at registration. Use `-Runtime` for a custom runtime or `-ToolPath` for a semicolon-separated list of provider executable directories. Dispatcher output goes to `dispatcher.log` in the runtime directory.

```powershell
.\examples\install_dispatcher.ps1
Start-ScheduledTask -TaskName 'Nightweaver Dispatcher'
```

To remove the sign-in task, stop it with `Stop-ScheduledTask -TaskName 'Nightweaver Dispatcher'` and unregister it with `Unregister-ScheduledTask -TaskName 'Nightweaver Dispatcher'`. [Windows Task Scheduler settings](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)

## Add browser acceptance checks

For static HTML, [check_static_page.py](examples/check_static_page.py) checks the expected heading, horizontal overflow, and uncaught page errors in desktop and mobile viewports. Install Playwright with `python -m pip install playwright` and `python -m playwright install chromium`, or use `--channel msedge` with a compatible installed Edge. Add `.nightweaver-browser/` to the target project's Git ignore list. [Playwright browser setup](https://playwright.dev/python/docs/browsers)

```json
{
  "name": "Responsive homepage",
  "argv": ["python", "C:/path/to/Nightweaver/examples/check_static_page.py", "index.html", "Field Notes"],
  "artifacts": [".nightweaver-browser/desktop.png", ".nightweaver-browser/mobile.png"]
}
```

Put this gate in `gates` or `final_gates`. For an app with a build step or interactions, supply a project-specific Playwright check that launches the candidate and exercises the relevant flows. Configure a reviewer that can inspect screenshots and describe the visual requirements in `acceptance`. A failed check, missing screenshot, or review requesting changes sends the issue back for repair.

Run data, model output, and evidence live under `%LOCALAPPDATA%\Nightweaver` or the directory chosen through `--home` or `NIGHTWEAVER_HOME`. Treat them as local project data. The engine creates a result branch locally. Review its commits before merging them into your branch or pushing them to a remote.

Upgrading from the original Rookline release preserves its existing runtime directory, run history, evidence, provider configuration, and result branches. New runs use `nightweaver/<run-id>`. The old `python -m rookline` command and `ROOKLINE_HOME` remain supported for existing scripts. No history or worktrees are moved automatically.

For a slow local model, set `timeout` and `max_tokens` inside `advisory_ollama`. Defaults are 90 seconds and 100 tokens. Model timeouts are recorded in the activity log and independent verification continues.

[Design rationale and current limits](docs/design-rationale.md)

## Run the integration tests

```powershell
python -m unittest discover -s tests -v
dotnet build Nightweaver.Desktop -c Release
```
