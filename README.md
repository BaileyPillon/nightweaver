# Nightweaver

An autonomous workshop for your code, built for Windows.

Windows-native orchestration for coding agents. Give Nightweaver a goal with checked milestones. It hands off work between agents, sends failed checks back for repair, and advances after the result passes verification. With `--pursue`, an independent judge then scores the finished build against the goal and a planner adds further waves of milestones until the goal is met or progress stops. Close the desktop dashboard whenever you need to. The runner keeps its place in SQLite.

## What's new in 0.19 — goal pursuit

Until 0.19 a run ended when its fixed milestone list was accepted, and a person decided what came next. Version 0.19 adds that outer loop. After the last planned milestone is accepted, the controller re-runs every distinct check from every milestone on the finished build and collects screenshots. This is the evidence ledger. An independent, read-only judge then returns `MET` or `NOT_MET`, a 0–100 score, a result for each criterion and a ranked list of gaps. The run is done only when the judge says `MET` at or above `target_score` and every ledger check passes. Otherwise a read-only planner turns the gaps into the next wave of milestones, which inherit the last milestone's checks, scope and workers.

A pursuit stops when the goal is met, when the score stops improving (`plateau`), when the wave allowance is spent (`max_waves`) or at the 50-milestone cap. A wave that scores more than `regression_margin` below the best build is discarded to a `nightweaver/<run-id>-discarded-wNN` ref, and the result branch returns to the best build. A stopped run always leaves the best build on its result branch. `nightweaver pursuit RUN_ID --add-waves N` grants more waves and resumes a run that stopped at the judge.

```powershell
nightweaver oneshot C:\src\my-project "Build a polished pricing page" --pursue --judge claude --judge-model claude-opus-5-5 --judge-effort xhigh --waves 3 --wait
nightweaver pursuit RUN_ID
nightweaver pursuit RUN_ID --add-waves 2
```

The release also fixes what an audit of 118 runs on one machine, 13 of them real work, showed about unattended work:

- A provider usage limit parks the run until the provider's reset time and resumes it automatically. Before, the run blocked within seconds.
- Failure identity is the phase, the failed checks and the build tree, not the wording of a message. The repeated-failure stop that never fired in those runs now does, and a worker that changes nothing after a review rejection is stopped without a new review.
- A reviewer receives the full staged diff, the attempt's screenshots, its own earlier findings and the local model's advice. Approvals with prose before the JSON verdict are no longer read as rejections.
- A worker that goes silent for 900 seconds is retried in a fresh session without costing an attempt. Resumed Codex sessions count only their own tokens.
- Browser gates fall back to Edge or Chrome when the bundled Chromium is missing, and web repositories without tests get a browser smoke gate.
- A browser gate can sweep a page's controls: Nightweaver itself clicks each visible button, tab, switch, radio, checkbox and disclosure and photographs every state, so reviewers and judges see what sits behind a control instead of only the first load.
- Claude can run unattended with a long-lived token. A run warns when its reviewer or judge shares the worker's vendor.

Those same runs found no matched evidence that a OneShot beats standalone Codex or Claude Code. The one matched pair was parity on acceptance at about 2.8 times the time and 4.8 times the tokens, on a trivial task. Version 0.19 targets the gap the field data supports, a missing outer loop, and does not claim a measured advantage. [The 30 ranked changes, field evidence, research sources and what is not yet built](docs/v0.19-research.md)

## Earlier releases

Version 0.18 can restart a crashed Windows runner at its saved milestone. Give a dedicated run a restart allowance, inspect its accepted commit, and trace a crash from the dashboard or offline report. [The 40 ranked changes, recent research and Windows recovery trials](docs/v0.18-research.md)

```powershell
nightweaver oneshot C:\src\my-project "Fix the broken share link" --guard-restarts 2 --wait
nightweaver recovery RUN_ID
nightweaver restarts RUN_ID
```

The allowance applies across supervisor launches. To recover after the supervisor itself exits, use `nightweaver dispatch --recover-guards` for one sweep or add `--watch` for ongoing monitoring. In the Windows dashboard, choose **New OneShot → Restart a crashed runner up to twice**.

Version 0.17 added functional handoffs between workers with the partial edits, failed checks and remaining budget intact. [All 30 changes, sources and the restart comparison](docs/v0.17-research.md)

```powershell
nightweaver oneshot C:\src\my-project "Fix the broken share link" --preview
nightweaver oneshot C:\src\my-project "Fix the broken share link" --worker claude --rescue codex --wait
nightweaver repair-history RUN_ID
```

The first command shows the pinned Git commit, checks, protected files and budgets without calling a model. The second plans and executes in isolated worktrees. Add `--profile-dir PATH` to use a particular signed-in profile, `--python PATH` for a project virtual environment or `--direct` for one checked milestone. Run without `--wait` to return after planning and background launch. Existing JSON plans can opt in with `"repair": {"collect_failures": true, "switch_after": 1, "fresh_session_after": 1, "history": 3}` and a distinct `worker_fallbacks` entry.

Version 0.16 lets agents enrich a prompt catalog while preserving the tags people search for. Compare catalog revisions, profile a field, and enforce compound uniqueness, deletion budgets and tag retention during a OneShot. See the [30 ranked changes and the 1,493-prompt Afterimage trial](docs/v0.16-research.md).

Version 0.15 can require a named test to fail for a specified reason on the original Git commit and pass on the candidate, while a known-good control passes on both. Use `check-baseline` before a model call or `compare-revisions` on two existing commits. Read the [30 ranked changes, research sources, Windows trial and proof example](docs/v0.15-research.md).

Version 0.14 lets a OneShot carry earlier checks into later milestones and grade individual JUnit test results. On a Windows watchlist fixture, it caught a later change that broke an accepted feature even though the test command exited zero. See the [30 ranked changes, research sources, Windows trials and plan example](docs/v0.14-research.md).

Version 0.13 checks a change for new and returning visitors on desktop and phone. A OneShot can now seed an existing collection, close and reopen the app, verify what persisted, and tell a repair worker exactly which visitor failed. [All 30 ranked changes, a working browser contract and the Afterimage experiment](docs/v0.13-research.md)

Version 0.12 measures how consistently coding workflows finish the same tasks. Run a balanced comparison, find mixed results across repeats, measure the size of accepted patches, and set a minimum acceptance rate for CI. [All 30 ranked changes and the eight-trial Afterimage experiment](docs/v0.12-research.md)

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

## What runs on Windows

- A WPF dashboard for starting a plan or OneShot, watching attempts, opening evidence, and pausing or resuming runs.
- Native Codex, Claude Code, and DeepSeek adapters. Use your existing CLI logins or select a specific installed profile for a role. DeepSeek runs through Claude Code's tools with a separate API key and configuration directory. An optional local Ollama model supplies advisory feedback. You can also plug in a custom executable.
- A pinned Git baseline and a separate worktree per milestone. Accepted commits land on `nightweaver/<run-id>` for inspection or integration.
- Independent operator-defined checks, with optional required screenshots or other files and a separate reviewer. Check receipts bind artifacts and decisions to a Git tree. Failed checks feed the next attempt automatically.
- A configured reviewer checks the whole objective at the final milestone and can send unfulfilled requirements back for repair. In a pursuit run, the judge decides whether the whole goal is met and the final-milestone reviewer checks only that milestone.
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

Install the .NET 8 SDK with the Windows Desktop runtime and run the following. Select **New OneShot** to choose a repository, inspect the detected checks and enter the goal. The same dialog accepts an existing plan template. **Start a plan** runs an explicit `plan.json`. Set `NIGHTWEAVER_PYTHON` to the path of your Python executable if `python.exe` is unavailable on `PATH`.

```powershell
dotnet run --project Nightweaver.Desktop -c Release
```

The compiled desktop app accepts `--home <runtime-directory>` and `--python <python.exe>` for shortcuts that need explicit paths.

The activity list names goal pursuit and unattended-run events in plain words, for example "Goal judged 72/100", "Waiting for usage reset until 18:40", "Worker stalled; retrying fresh" and "Reviewer shares the worker's vendor". The New OneShot dialog does not offer pursuit yet. Start a pursuit from the command line and watch it in the dashboard.

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

The report resolves the plan's checkout and identifies the executable, profile, model and configured reasoning effort for each role. A non-live report checks local configuration. With `--live`, Nightweaver sends a small test prompt per distinct model route. Live requests use your normal subscription limits or API balance. For Ollama, the live report also records the responding model and whether it returned thinking. Custom commands are checked for executable availability and run only as part of the plan.

Set `effort` on a Claude, Codex or DeepSeek worker or reviewer. Use `--effort` for a CLI planner. For example, Opus 5.5 and Astra 6.2 accept `xhigh`. DeepSeek's highest native level is `max`. Explicit settings apply to the child process, including resumed calls.

The command exits with status 3 if a required model route is blocked or a live request could not be verified. On a run, a missing CLI, missing selected profile, or recognized account, model, or usage-limit error blocks with the affected role and profile. Correct that route and retry the existing run. [Claude profile settings](https://code.claude.com/docs/en/settings) · [Ollama model listing](https://docs.ollama.com/api/tags)

## Pursue a goal until it is met

A OneShot normally ends when its planned milestones are accepted. Add `--pursue` to keep going until an independent judge accepts the result.

```powershell
nightweaver oneshot C:\src\my-project "Build a polished pricing page" --pursue --judge claude --judge-model claude-opus-5-5 --judge-effort xhigh --waves 3 --wait
```

`--judge` chooses the judging provider. The default is a different model family from the worker: Claude, unless the worker is Claude. `--judge-model`, `--judge-effort` and `--judge-profile` pick its model, reasoning effort and signed-in profile. The judge also plans each wave unless `--planner-kind` names another provider, with `--planner-model`, `--planner-effort` and `--planner-profile`. `--waves` sets the additional waves allowed after the initial plan (1–20, default 3). `--target-score` sets the score that counts as done (default 85). `--reference IMAGE` gives the judge a target to compare screenshots with, repeated up to six times. `--reviewer` adds a reviewer for each milestone. These flags require `--pursue`. Each wave costs a planner call, a worker call for each milestone and a judge call, so set `budgets` in a template when spend matters.

### The `pursue` block

Plans and OneShot templates take the same settings in a `pursue` block beside `worker` and `gates`. Only `judge` is required. The judge, its fallbacks and the planner take the same provider settings as a worker or reviewer: `kind`, `model`, `effort` and `profile_dir`.

```json
"pursue": {
  "judge": {"kind": "claude", "model": "claude-opus-5-5", "effort": "xhigh"},
  "judge_fallbacks": [{"kind": "codex"}],
  "planner": {"kind": "claude"},
  "max_waves": 3,
  "wave_size": 3,
  "target_score": 85,
  "plateau": 2,
  "regression_margin": 5,
  "references": ["reference/pricing-target.png"],
  "criteria": [
    "Three pricing tiers are readable at 390 px width",
    "The recommended tier is visually distinct"
  ]
}
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `judge` | required | The independent, read-only judge. A judge from the worker's own vendor works but is recorded as less independent. |
| `judge_fallbacks` | none | Up to five other routes tried in order when the judge's provider fails. |
| `planner` | the judge | Plans each wave from the judged gaps. It stays read-only. |
| `max_waves` | 3 (1–20) | Additional waves after the initial plan. |
| `wave_size` | 3 (1–5) | Most milestones one wave may add. |
| `target_score` | 85 (0–100) | The score a `MET` verdict must reach. Every ledger check must also pass. |
| `plateau` | 2 (1–5) | Consecutive judgments that fail to beat the best score before the pursuit stops. |
| `regression_margin` | 5 (0–50) | A wave scoring this far below the best build is discarded and the best build restored. |
| `references` | none | Up to six `.png`, `.jpg`, `.jpeg` or `.webp` images, relative to the plan file. |
| `criteria` | the plan's `acceptance` | What defines done. The judge answers each one. |

The judge reads the source, the check logs and up to eight distinct screenshots from the ledger checks, plus any reference images. A pursuit stops with `wave_limit` when `max_waves` is spent, `plateau` when progress stalls, or `milestone_limit` at 50 milestones. It leaves the best build on `nightweaver/<run-id>`.

### Read and extend a pursuit

```powershell
nightweaver pursuit RUN_ID
nightweaver pursuit RUN_ID --add-waves 2
```

`pursuit` prints each wave with its milestones, the judgment, the top gaps, any failed ledger checks and any revert. Add `--json` for the same data as JSON. `--add-waves N` raises `max_waves` by N (1–20). A run that stopped at the judge goes back there with a fresh plateau count, so resume it or let the dispatcher continue it. `report` adds a Goal pursuit section and `handoff` lists the scores and open gaps. `doctor` and `preflight` check the judge, its fallbacks and the planner.

### Run unattended

- **Usage limits.** When a provider reports a usage limit, Nightweaver reads its reset time, parks the run and retries 90 seconds after the reset. The worker attempt is refunded. If the message gives no time, the retry comes after 30 minutes, doubling up to 2 hours. `retry.max_parks` (default 8 consecutive waits, `0` turns waiting off) and `retry.max_park_seconds` (default 21600, range 60–604800) bound the waiting. A reset that falls after the run's time budget blocks with a clear message. Parked runs resume through the runner, the dispatcher and the guard. A parked inline runner keeps the repository lock while it waits.
- **Silent workers.** Workers, reviewers, judges and planners get a `budgets.idle_timeout` of 900 seconds unless the plan sets one (1–14400). A worker that goes silent is retried in a fresh session and its attempt is refunded, at most twice in a row.
- **Big repositories.** A top-level `git_timeout` (30–3600 seconds, default 90) governs git calls. `git worktree add` always gets at least 900 seconds, and a timed-out checkout is cleaned up and explained.
- **Claude sign-in.** A Claude login can expire while a run is unattended. Run `claude setup-token` once. Keep the result in the user environment variable `CLAUDE_CODE_OAUTH_TOKEN`, which Nightweaver also reads from the registry when a process started before it was set, or in a file named by the route's `oauth_token_file`. The token goes only to Claude processes and is never logged or reported. `doctor` names the source in use. An expired login blocks with the `claude setup-token` remedy instead of quietly leaving review to the worker's own vendor.
- **Independence and credentials.** When a reviewer or judge comes from the same vendor family as the worker, the run records `independence_degraded` once for that pair and continues. Codex and Gemini processes do not inherit Claude, Anthropic or DeepSeek credentials, and Claude processes drop `DEEPSEEK_API_KEY`.

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

**Smoke gates and the Edge fallback.** A OneShot for a repository with no committed tests gets a `Browser smoke` gate when the repository is a static web project (an `index.html` at the root, in `public/` or in `docs/`) or a new repository holding only a README, license or ignore files. The gate loads the page at desktop and phone sizes and keeps the screenshots, so a goal can start from nothing. A repository whose `package.json` has a `dev`, `start`, `preview` or `serve` script is not given one, because a bare file server cannot render it. The failure message points to a browser gate that owns the server. When the bundled Chromium is missing, a browser gate that uses the default retries with Microsoft Edge and then Chrome. The channel that ran and the reason are recorded in `browser.json` and the gate receipt's `browser_runtime`. An explicit `channel` or a `PLAYWRIGHT_BROWSERS_PATH` pin still fails closed. A non-bundled browser takes one throwaway screenshot before the timed cases, and a missing favicon is not a page error. `doctor` reports `browser` as `ready`, `ready_fallback` or `unavailable` from the install folders without starting a browser.

**Interaction sweep.** Add `"sweep": {"max_controls": 8}` (1 to 16, default 8) to a browser gate, and the OneShot smoke gate carries `"sweep": {}`. After the desktop viewport (the one named `desktop`, else the first) loads cleanly, the supervisor clicks up to that many visible, enabled controls in page order (`button`, `[role=button|tab|radio|switch|checkbox]`, `input[type=button]`, radio and checkbox inputs, `summary`, `[aria-pressed]`, `[aria-expanded]`, `[onclick][tabindex]`, hash-only anchors that act as widgets, and each other option of a `select`; a radio or checkbox hidden behind a styled label is clicked through its label), waits until the page stops changing and saves a `sweep-NN-<name>.png` after each click. It lists the page again after every click, so controls an intro overlay or a menu was hiding are swept once they appear. It skips real links, submit buttons inside forms, disabled, hidden, covered or off-screen controls, duplicates of the same role and name, and anything whose name or identifier starts with delete, remove, reset, clear, erase, log out, sign out, buy, pay, purchase, submit, send, subscribe, cancel, empty, discard, archive, drop, destroy, wipe, revoke, deactivate, disconnect, publish, deploy, confirm, checkout, order, donate, transfer or withdraw. The clicks are contained: while the sweep runs, every write (anything but GET and HEAD) and every navigation to another site is answered locally with an empty success, so nothing leaves the machine, and each row counts its `blocked_requests`; add `"allow_writes": true` to let writes to the page's own origin through. A click that navigates away (even a moment later), opens a popup, a native dialog or a modal is recorded, and the original page is reloaded before the next control; a gate with `steps` cannot replay them, so there the sweep ends with a note instead of reloading. A click that hangs the page ends the sweep with a failure that names the control, instead of hanging the gate. A console error, page error or new horizontal overflow caused by a click fails the gate like a load-time one, and the failure names the control (`After clicking 'Night': ...`). The sweep screenshots are hashed with the other gate artifacts, `browser.json` gains a `sweep` list and the gate receipt a `browser_sweep` summary and a `browser_sweep_coverage` (found, clicked, not clicked, left alone, never reached, why it stopped), and the trace and the request budget cover the page load only. Reviewers and judges see the first load of each viewport first (labelled with the steps it followed, or the point of failure), then the swept states in order, each labelled with the control it shows, then one line of sweep coverage so a partial sweep never reads as a full one. Without a `sweep` key a browser gate behaves as before.

Run data, model output, and evidence live under `%LOCALAPPDATA%\Nightweaver` or the directory chosen through `--home` or `NIGHTWEAVER_HOME`. Treat them as local project data. The engine creates a result branch locally. Review its commits before merging them into your branch or pushing them to a remote.

Upgrading from the original Rookline release preserves its existing runtime directory, run history, evidence, provider configuration, and result branches. New runs use `nightweaver/<run-id>`. The old `python -m rookline` command and `ROOKLINE_HOME` remain supported for existing scripts. No history or worktrees are moved automatically.

For local advice, set `think` to a value supported by the installed Ollama model and allow room for both thinking and its final answer with `max_tokens`. Defaults are thinking off, 100 output tokens and a 90-second timeout. Set `required` to `true` when every accepted milestone needs a local response. A failed required call blocks at review and can be retried with `resume --retry`.

[Design rationale and current limits](docs/design-rationale.md)

## Run the integration tests

```powershell
python -m unittest discover -s tests -v
dotnet build Nightweaver.Desktop -c Release
```

The suite has 336 tests. Version 0.19 added 134 in four modules: `test_release_019_pursuit` (25), `test_release_019_browser` (30), `test_release_019_runtime` (55) and `test_release_019_repair` (24). To run only those:

```powershell
python -m unittest discover -s tests -p "test_release_019*.py"
```
