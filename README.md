# Nightweaver

An autonomous workshop for your code, built for Windows.

Windows-native orchestration for coding agents. Give Nightweaver a goal with checked milestones. It hands off work between agents, sends failed checks back for repair, and advances after the result passes verification. Close the desktop dashboard whenever you need to. The runner keeps its place in SQLite.

## What runs on Windows

- A WPF dashboard for starting a plan or OneShot, watching attempts, opening evidence, and pausing or resuming runs.
- Native Codex, Claude Code, and DeepSeek adapters. Use your existing CLI logins or select a specific installed profile for a role. DeepSeek runs through Claude Code's tools with a separate API key and configuration directory. An optional local Ollama model supplies advisory feedback. You can also plug in a custom executable.
- A pinned Git baseline and a separate worktree per milestone. Accepted commits land on `nightweaver/<run-id>` for inspection or integration.
- Independent operator-defined checks, with optional required screenshots or other files and a separate reviewer. Check receipts bind artifacts and decisions to a Git tree. Failed checks feed the next attempt automatically.
- A configured reviewer checks the whole objective at the final milestone and can send unfulfilled requirements back for repair.
- A background runner with Windows Job Objects for child process cleanup. Relaunch a failed or interrupted runner against the same durable state, worktree, and recorded provider session where available.

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

- **OneShot.** Create a template with the repository, title, worker, and trusted checks. `python -m nightweaver goal template.json "Your goal" --planner claude --start` makes a fresh plan of up to five milestones and starts it. Omit `--start` to print the proposed plan first. Use `--profile-dir` to select an existing planner configuration directory.
- **Checks.** Each gate has a `name`, an executable `argv` array, an optional `timeout` in seconds, and an optional `artifacts` array of paths relative to the worktree. For browser QA, have your Playwright or browser script start the candidate build, verify its main flow, and generate fresh viewport screenshots. Add the screenshots to `artifacts` and configure a reviewer that can inspect images. Keep generated files ignored by Git. A failed browser check, missing screenshot, or `REVISE` result stops promotion and sends the issue back to the worker.
- **Profiles.** The `codex` and `claude` workers and reviewers accept `model`, `executable`, and `profile_dir`. Log into each chosen profile with its own CLI. The engine passes the matching configuration directory to that process. Use `reviewer` with `{"kind":"claude"}` or `{"kind":"codex"}` to require a fresh review. A custom `command` reviewer receives the brief on stdin and must emit a JSON `verdict` (`PASS` or `REVISE`) and `reason`.
- **Recovery.** `python -m nightweaver resume <run-id>` starts an interrupted or paused run again. After correcting an external blocker, use `resume <run-id> --retry`. Windows reboot recovery starts when you resume the run. Use the dashboard or `events` to inspect what the runner actually recorded.

Run data, model output, and evidence live under `%LOCALAPPDATA%\Nightweaver` or the directory chosen through `--home` or `NIGHTWEAVER_HOME`. Treat them as local project data. The engine creates a result branch locally. Review its commits before merging them into your branch or pushing them to a remote.

Upgrading from the original Rookline release preserves its existing runtime directory, run history, evidence, provider configuration, and result branches. New runs use `nightweaver/<run-id>`. The old `python -m rookline` command and `ROOKLINE_HOME` remain supported for existing scripts. No history or worktrees are moved automatically.

For a slow local model, set `timeout` and `max_tokens` inside `advisory_ollama`. Defaults are 90 seconds and 100 tokens. Model timeouts are recorded in the activity log and independent verification continues.

[Design rationale and current limits](docs/design-rationale.md)

## Run the integration tests

```powershell
python -m unittest discover -s tests -v
dotnet build Nightweaver.Desktop -c Release
```
