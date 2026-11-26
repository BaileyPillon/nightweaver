# Nightweaver

An autonomous workshop for your code, built for Windows.

Windows-native orchestration for coding agents. Give Nightweaver a goal with checked milestones. It hands off work between agents, sends failed checks back for repair, and advances after the result passes verification. With `--pursue`, an independent judge then scores the finished build against the goal and a planner adds further waves of milestones until the goal is met or progress stops. `nightweaver wish` starts such a run from one line. Close the desktop dashboard whenever you need to. The runner keeps its place in SQLite.

## What's new in 0.19.2

A fix for the `antigravity` provider, from the first signed-in runs of `agy` 1.5.17 (nine small runs on 2026-11-25). [What each run showed, what was decided and what is still not known](docs/v0.19.2-notes.md).

- **No role runs plan mode any more.** Every role runs `--mode accept-edits`. The reviewer, judge, planner, replanner, brief writer, directions, comparison, ranking, diagnosis and the `doctor` probe also pass `--settings` with deny rules for file writes, shell commands and MCP tools. Those rules are the one barrier the live runs showed stopping `agy`'s edit and write tools and its shell: plan mode held only while the model chose to obey, no write was ever attempted under it, and for a task-like prompt `agy` wrote an `implementation_plan.md` instead of the requested JSON. The sandbox (`--sandbox`) did not stop an edit. [Per-role table](docs/v0.19.2-notes.md#the-mode-of-each-role)
- **Checked before the call, and after it.** Before a read-only role starts, Nightweaver asks `agy -p /permissions --settings FILE`, which costs no model turn, whether `agy` loaded the rules, and does not start the role when it did not (`configuration`). After the call, a stream that shows a completed edit, write, command, notebook, workflow, MCP or image step is refused (`read_only_violation`, the answer discarded), as is a call whose subagent changed a file in the worktree (a subagent's own steps are not in the stream, so Nightweaver compares the files before and after), and a role that tried an edit or command, was refused under the rules and answered anyway is not taken as a result (`read_only_refused`, retried). Behind all of these, the run's own snapshot check still catches an `agy` reviewer that changes the worktree during its review and sends the run back to its checks.
- **An answer, or a failure that says why.** The parser reads the stream `agy` really writes: the structured answer for the roles with a schema (the `response` field holds the JSON twice after `agy`'s own re-ask), `response` for the others, else the last message with text. A read-only role that finishes with nothing to read now fails as `no_answer` naming the status, the actions `agy` refused and any `implementation_plan.md` it wrote instead (never read as an answer), and is retried as a transient failure.
- **Token counts of a resumed call.** `agy`'s `usage` is cumulative over a resumed conversation, so each call counts what its own steps reported instead of the whole conversation again.
- **`doctor --live` probes the way the roles run.** The probe uses the same read-only configuration, asserts a non-empty `READY` and reports `empty_answer` apart from `unexpected_response`. The `agy` report adds `read_only_rules` (`enforced`, `missing` or null); a probe that answers `READY` while `read_only_rules` is null is `unverified` (`read_only_unverified`), not `ready`.
- **What 0.19.1 left unverified is settled** (answers, structured output, token counts, the served-model check, plan mode) or changed, as the notes list item by item.

## What's new in 0.19.1

A fix release. It comes from one unattended pursuit that spent most of a 14-hour time budget blocked, and from Google moving AI Pro and Ultra to Antigravity. [Each fix, the incident behind it and what is still unverified](docs/v0.19.1-notes.md).

- **Wave milestones no longer inherit the final checks.** A new wave's early milestones pass the plan's per-milestone checks. Only the wave's last milestone also passes the final-only ones: `final_gates`, checks retained `final` and checks only the last milestone owns, recorded in the plan as `final_checks`. A plan whose checks are all final plans one-milestone waves, and a one-milestone plan keeps its own checks per-milestone.
- **`settle_gates` for runs 0.19 already extended.** A pursuit made by 0.19 is settled when it starts again (`resume`, `--retry` included). The split is recorded, and the unbuilt early milestones of the waves 0.19 already planned lose the final-only checks, so a run stuck on them goes on. A `gates_split` event says what changed. Milestones already built stay as they ran.
- **`--objective-file`.** `goal` and `oneshot` read the objective from a UTF-8 file (BOM and CRLF accepted, up to 1 MB) instead of the 32,767-character Windows command line. It cannot be combined with the objective argument.
- **Git identity check and fallback.** `goal` and `add` with `--start` or `--enqueue`, `oneshot` unless `--preview`, and `wish --start` refuse a repository where git resolves no author or committer identity, before any model call, and print the exact `git config` commands that fix it. `doctor --plan` and `preflight` report it. A run that reaches a commit without an identity anyway gets it under `Nightweaver <nightweaver@localhost>`, for whatever git cannot resolve (a configured name or email is kept and only its missing partner is filled), and `milestone_accepted` records `identity_fallback`.
- **A gate that times out is run again.** A gate the controller ends at its time limit is run again once with twice the time, never above 4 hours, before anything is decided. `retry.gate_timeout_retries` (0 to 3, default 1) sets how many times, and 0 is the 0.19 behavior. A timeout is never a worker failure. One that survives the retry (or the first stop, with retries at 0) blocks the run with the gate, its limits and what to do about them. The same retry now covers the judge's evidence ledger: a ledger check that times out for good is unscored, not failed, so the judgment blocks (`blocked_from: judging`) instead of planning a "Check failed" milestone. A design direction or a rescue strategy whose checks time out for good is recorded as unscored and left out of the contest, and a contest in which every candidate is unscored blocks. [Details](#run-unattended)
- **`max_seconds` counts active time.** The time budget is wall time less the stretches the run sat blocked or paused. Parking on a usage limit still counts, and a wait that would outlast the budget is still refused. `budget` and `metrics` show active, idle and wall time.
- **Gemini CLI fixes.** The Gemini route runs the npm package's real entrypoint under node, trusts the headless worktree, and recognizes its sign-in, folder-trust, capacity, quota and model errors, so they block or park a run instead of using up attempts. `doctor` shows how the route will sign in.
- **The `antigravity` provider for Gemini 4 Neon.** Google's `agy` CLI is the supported route to Gemini 4 Neon at a chosen reasoning level (`gemini-4-neon`, `effort` up to `xhigh`). Effort is checked per model when the plan loads: Neon and Hydrogen take `low` to `xhigh`, Carbon, Argon and the 3.x Flash models `low` to `high`, 3.1 Pro `low` and `high`, and none takes `max`. Sign in by launching `agy` once and signing in; Nightweaver checks the sign-in before every call, because a signed-out `agy` waits a minute for a code that no unattended run can give. [Details](#use-gemini-and-antigravity)
- **Live verification, settled in 0.19.2.** This release shipped with the success half of `agy`'s stream-json output and plan mode unverified. 0.19.2 verified the stream and replaced plan mode; see above.

## What's new in 0.19 — goal pursuit

Until 0.19 a run ended when its fixed milestone list was accepted, and a person decided what came next. Version 0.19 adds that outer loop. After the last planned milestone is accepted, the controller re-runs every check that applies to the finished build and collects screenshots. This is the evidence ledger. An independent, read-only judge then returns `MET` or `NOT_MET`, a 0–100 score, a result for each criterion and a ranked list of gaps. The run is done only when the judge says `MET` at or above `target_score`, every ledger check passes and, by default, a second, independent judgment of the same build confirms it. (`confirm: false` turns the confirmation off, and if the call, time or token budget cannot fund it, the first judgment stands and the run records `confirmation_skipped`.) Otherwise a read-only planner turns the gaps into the next wave of milestones, which inherit the last milestone's workers and its per-milestone checks (only a wave's last milestone also passes the final-only checks, as of 0.19.1), and the paths of all plan milestones together (no path limit if any milestone had none).

A pursuit stops when the goal is met, when the score stops improving (`plateau`, after one [plateau rescue](#rescue-a-plateau) by default), when the wave allowance is spent (`max_waves`) or at the 50-milestone cap. A wave that scores more than `regression_margin` below the best build is discarded to a `nightweaver/<run-id>-discarded-wNN` ref, and the result branch returns to the best build. A stopped run always leaves the best build on its result branch. `nightweaver pursuit RUN_ID --add-waves N` grants more waves and resumes a run that stopped on a judged build.

```powershell
nightweaver wish C:\src\my-project "Make the pricing page feel finished" --start --wait
nightweaver oneshot C:\src\my-project "Build a polished pricing page" --pursue --judge claude --judge-model claude-opus-5-5 --judge-effort xhigh --waves 3 --wait
nightweaver pursuit RUN_ID
nightweaver pursuit RUN_ID --add-waves 2
```

**The pursuit loop**

- **Goal pursuit.** A judge, an evidence ledger, waves, keep-best with green builds ranked first, typed stop rules and `--add-waves`. [Details](#pursue-a-goal-until-it-is-met)
- **A judge that is not told the pass mark.** Every judge scores against one fixed rubric. A judgment that would finish the run is confirmed by a second, independent judgment of the same build before `MET` stands (`confirm`, `confirm_margin`, `confirm_judge`). [Details](#the-pursue-block)
- **Design-direction tournament.** Before milestone 1, `pursue.explore` (`oneshot --explore N`) builds N deliberately different versions, judges them pairwise in both orders and promotes the winner. [Details](#explore-design-directions-before-building)
- **Plateau rescue.** When scores stop improving, one read-only call diagnoses the stall, divergent strategies are built from the best build, and one replaces it only if it wins (`pursue.rescue`, `--rescue-strategies`). [Details](#rescue-a-plateau)
- **`nightweaver wish`.** One line becomes a brief, acceptance requirements, checks, a model roster and a pursuit run. [Details](#start-from-one-line)

**Browser evidence**

- **Interaction sweep.** A browser gate clicks a page's controls itself and photographs every state, so reviewers and judges see what sits behind a control instead of only the first load. [Details](#interaction-sweep)
- **Smoke gates.** Web repositories without tests get a browser smoke gate. A `--web` / `--no-web` hint settles whether a new repository's goal is a page. A gate's `root` field serves a site from `public/` or `docs/` as it is deployed. Browser gates fall back to Edge or Chrome when the bundled Chromium is missing. A visual wish on a tested repository gets a baseline-probed `Visual smoke` gate beside its tests. [Details](#smoke-gates-and-the-web-hint)

**Unattended runs.** These fix what an audit of 118 runs on one machine, 13 of them real work, showed, plus what the adversarial review found. [Details](#run-unattended)

- A provider usage limit parks the run until the provider's reset time and resumes it automatically. Before, the run blocked within seconds.
- Failure identity is the phase, the failed checks and the build tree, not the wording of a message. The repeated-failure stop that never fired in those runs now does. When a worker changes nothing after a review rejection, that attempt is rejected as `no_progress` without re-running the checks or the reviewer. It still uses an attempt, and the run blocks after `retry.repeated_failure_limit` identical failures (default 3) or when attempts run out.
- A reviewer receives the staged diff (the first 16 KB inline and the whole patch, up to 400 KB, in a saved file it is told to read), the attempt's screenshots, its own earlier findings and the local model's advice. Approvals with prose before the JSON verdict are no longer read as rejections, and an answer that cannot be read fails closed. A reviewer that fails is retried without costing the worker an attempt.
- A worker that goes silent for 900 seconds is retried in a fresh session without costing an attempt (plans written by 0.19). Resumed Codex sessions count only their own tokens. `git_timeout` governs git, and a checkout of a large repository survives a runner crash. Chromium `debug.log` files and similar tool droppings are deleted, so no check passes on a file the accepted commit lacks.
- Claude can run unattended with a long-lived token, in Nightweaver's own config directory so an operator's interactive `CLAUDE.md` cannot steer it. Gates, git and `command` routes get an environment with no credential-shaped variable.
- Each provider call records the model that was actually served, and a route served a different model than it asked for raises `model_mismatch`.
- Opt-in phone notifications over ntfy, with quiet hours, an on-demand digest (`notify digest`; nothing schedules it) and automatic release of messages held overnight. [Details](#phone-notifications)

**What has been measured.** The [research record](docs/v0.19-research.md) holds the numbers and their limits.

- An adversarial review put 53 candidate defects to three skeptics each. 51 were upheld (6 blocker, 21 major, 24 minor) and fixed in five fix groups, and every fix was attacked again by two skeptics. The interaction sweep and the plateau rescue had their own reviews, with 22 and 10 upheld findings, all fixed.
- One live run built a page from a repository holding only a README in about 70 minutes. It accepted four milestones, rejected one milestone once for missing evidence, which the worker repaired, and judged the goal `MET` at 88/100 on the first wave. That judge had been shown the target score of 88 and returned exactly 88, a flaw this release removes. The run did not use later waves, the rescue, exploration, the sweep, the confirmation, `wish` or notifications.
- A blind head-to-head on the same brief compared that build with standalone Codex and standalone Claude builds. Two LLM judges, each in both orders, picked Nightweaver's build in all 8 pairwise calls it was in. Scored alone against the fixed rubric, the three builds averaged 91 (Nightweaver), 90.5 (Codex) and 86 (Claude). Nightweaver took 4.3 times the Codex build's wall time and 11.6 times its observed tokens (mostly cached input; 16.1 times counting only non-cache tokens), measured while the three builds ran at overlapping times on one machine. It is one brief and one build per contender, and both judges come from the builders' families, so it is one data point, not a general result.

Before this release no matched evidence existed that a OneShot beats standalone Codex or Claude Code. The one matched pair was parity on acceptance at about 2.8 times the time and 4.8 times the tokens, on a trivial task. [The 40 ranked changes, field evidence, research sources, adversarial review, trial results and what is not yet built](docs/v0.19-research.md)

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
- Native Codex, Claude Code, DeepSeek, Gemini CLI, and Antigravity (`agy`) adapters. Use your existing CLI logins or select a specific installed profile for a role (Antigravity keeps its sign-in in the CLI's own store and takes no profile). DeepSeek runs through Claude Code's tools with a separate API key and configuration directory. An optional local Ollama model supplies advisory feedback. You can also plug in a custom executable.
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

## Use Gemini and Antigravity

Google has two command-line agents, and Nightweaver drives both. Both are Google models and both take the Gemini credential variables.

**`gemini`, the Gemini CLI (`@google/gemini-cli`).** On Windows Nightweaver runs the package's own JavaScript entrypoint under node with no shell: the `bin` entry of its `package.json` (`bundle/gemini.js` in 0.70, `dist/index.js` in older packages), found beside `gemini.cmd`. It sets `GEMINI_CLI_TRUST_WORKSPACE=true` for the call, because a headless run in a folder the CLI does not trust exits at startup and a Nightweaver worktree never is trusted. A trusted workspace lets the CLI read that repository's own `.gemini` settings, hooks, MCP servers and `GEMINI.md`, so run it on projects you trust, as with every provider. An explicit `GEMINI_RESTRICTED_MODE=true` is left alone and blocks the route with that cause named. The CLI's Google-account login has been Enterprise-only since June 2026, so sign in with a paid `GEMINI_API_KEY` (or Vertex: `GOOGLE_API_KEY` with `GOOGLE_GENAI_USE_VERTEXAI=true`) in the process environment. Gemini has no key-file option. 0.70.0 does not support the Gemini 4 models and has no reasoning-effort setting, so `effort` is rejected on this kind with a pointer to `antigravity`. `doctor` shows how the route will sign in as `auth` (`api_key`, `google_cloud`, `oauth_login` or `none_found`) and never the secret.

**`antigravity`, the Antigravity CLI (`agy`).** This is Google's supported route to Gemini 4 Neon at a chosen reasoning level. Install it from Google, run `agy` once and sign in (Neon needs Google AI Pro or Ultra). The installer puts `agy.exe` in `%LOCALAPPDATA%\agy\bin` and does not add it to `PATH`; Nightweaver looks there first, then on `PATH`, or use the route's `executable`. The API-key alternative needs both halves: `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the environment and `"modelProvider": "gemini"` in `~/.gemini/antigravity-cli/settings.json`. The variable alone does nothing: agy then ignores it, prints a sign-in link and waits a minute for a code. Nightweaver does not add the setting for you, because that would move a route that is signed in with a subscription onto per-token billing just because the variable is set for some other tool. `AGY_ADC_AUTH=true` selects Application Default Credentials.

```json
{"worker": {"kind": "antigravity", "model": "gemini-4-neon", "effort": "xhigh"}}
```

- **Effort.** `low`, `medium`, `high`, `xhigh` or `max`, checked per model when the plan loads, against what `agy models` lists. Gemini 4 Neon and Hydrogen accept `low` to `xhigh`, Carbon, Argon and the 3.x Flash models `low` to `high`, and Gemini 3.1 Pro only `low` and `high`; none takes `max`. A route that names no model gets agy's default, `gemini-3.1-pro`, so it is held to `low` and `high` too, and the refusal says to name a model. A model that agy lists no levels for (a Claude model on a signed-in account) is left to agy. A model slug that names its level (`gemini-4-neon-xhigh`) cannot be given a different `effort`, nor a level its model does not have. The served-model check treats `gemini-4-neon` and `gemini-4-neon-xhigh` as one model and still flags a different level or family.
- **Modes.** Every role runs `--mode accept-edits`, passed explicitly so that agy's own default (`auto`, which falls back to the default mode on a host with no terminal sandbox) never decides. The worker may edit; in a headless run any command that needs review is denied, so the checks run as gates rather than inside the worker, and the worker keeps `--disable-slash-commands`, which stops a task that starts with `/` from running as a command. Every other role (reviewer, judge, planner, replanner, brief, directions, comparison, ranking, diagnosis, probe) also passes `--settings` pointing at a file under the runtime's `provider-temp` folder with `permissions.deny` set to `write_file(*)`, `write_file(/)`, `write_file(.)`, `command(*)` and `mcp(*)`. On agy 1.5.17 those rules ended `edit_file`, `write_file` and `run_command` steps in an error ("Matches user-configured deny rule") while the run itself went on and answered, and the same task with no rules edited the files. `mcp(*)` is agy's documented rule form for MCP tools and has not been run live. Plan mode is not used: it only works while the model obeys, and for a task-like prompt agy answers with an `implementation_plan.md` instead of the JSON the role asks for. `--sandbox` restricts terminal commands only (an edit went through under it). A read-only prompt that starts with `/` is prefixed with `Task:` so it cannot run as a slash command (the flag that stops that is not used beside `--settings`, which has not been tried together). agy loads a rule whatever its class is called, so a misspelt class would fail open. Before a read-only role starts, Nightweaver runs `agy -p /permissions --settings FILE`, which costs no model turn, and does not start the role when agy does not list every rule as loaded (`configuration`; a check agy cannot answer lets the call go on, and a clear check is remembered for five minutes). After the call, a stream that shows a completed file-writing tool (`write_file`, `edit_file`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`, `sed_file`, `notebook_edit`), a command or other runner (`run_command`, `send_command_input`, `notebook_execution`, `run_workflow`, `call_mcp_tool`, `generate_image`, `delete_knowledge`) or any tool whose name reads as a write, outside agy's own conversation folder, is refused as `read_only_violation` and its answer discarded. A file tool counts as agy's own only when every path it names, resolved (`..` and case included), lies inside `<home>/.gemini/antigravity-cli/brain`; a command never does. agy writes a subagent as one step (`invoke_subagent`) and none of its own, so when one ran Nightweaver lists the worktree before and after the call and refuses it if a file changed. A read-only role that tried an edit or command outside agy's folder, was refused and still answered is not taken as a result (`read_only_refused`): an answer written after refusals can describe the refusals, so it is retried as a transient failure (the fallbacks first) instead of reaching the verdict check. Behind all of these, Nightweaver's own snapshot check still compares the worktree before and after a review. Set `NIGHTWEAVER_ANTIGRAVITY_SANDBOX=1` to add `--sandbox`, which on Windows needs Windows 11 24H2 or later.
- **Answers.** A role with a schema gets agy's `structured_output` (agy re-asks until the model's answer fits; its `response` field joins every message of the run and holds the JSON twice after a re-ask, so it is not used for those roles). A role without one gets `response`, and when that is blank the last message with text. A read-only role that finishes with status `SUCCESS` and nothing to read fails as `no_answer`: the message gives the status, how many messages had text, the actions agy refused under the rules and, when agy wrote an `implementation_plan.md` (named in the stream and never read, because it is a plan to make edits and not the answer), its path. The engine retries it as a transient failure.
- **How the task is passed.** `--print` takes its prompt as a flag value and ignores stdin, and `--input-format stream-json` rejects a prompt on the command line, so the task goes in on stdin as one stream-json user message. Structured roles pass `--json-schema <file>` (the schema is written under the runtime's `provider-temp` folder), a resumed call passes `--conversation <id>`, and evidence and image folders are added with `--add-dir`.
- **Images.** agy has no image attachment option. A judge or reviewer sees an image only as a path in the prompt that its file-reading tool opens from an added folder, and nothing verifies that it did.
- **Credentials.** An agy route keeps only `GEMINI_*` and `GOOGLE_*` variables (and `AGY_ADC_AUTH`); the login itself lives in the Windows credential store, so a route has no `profile_dir` or key file.
- **Doctor.** The report gives the agy version, `auth` (`cli_login`, `api_key` or `adc`), `signed_in` (true, false, or null when agy could not say, from `agy models`) and, when signed in and the route names a model, `model_listed`. A signed-out agy prints a sign-in link and waits about a minute for a code that an unattended run can never give, so Nightweaver asks `agy models` before each call (a success is remembered for five minutes) and blocks as `authentication` when it is signed out. `read_only_rules` is `enforced` when `agy -p /permissions --settings FILE` lists every read-only deny rule, `missing` when it lists the rules without them and null when agy could not say (an unrecognised answer, or its settings turn slash commands off). A `--live` probe that answers `READY` with `read_only_rules` null is `unverified` with reason `read_only_unverified` and `doctor` exits 3, because a reviewer, judge or planner route would then rest on the stream and snapshot checks alone; the plain report warns the same way. Doctor asks agy about its rules afresh and does not use the five-minute memory of a call. `--live` probes in the same read-only configuration as a reviewer or judge, so a route that would give a reviewer nothing is caught: an agy probe that finishes with status `SUCCESS` and nothing to read is blocked as `no_answer` with agy's own account of why, and any other answer than `READY` is `unexpected_response` (`empty_answer` is the same finding for the other providers, whose clean exit with no answer used to read as `unexpected_response`). `auth` is `api_key` only when settings.json selects the Gemini API, because only then does agy use the key. With a key in the environment and no `modelProvider`, `auth` is `cli_login` and `api_key_ignored` is true, and the route is blocked unless agy is signed in some other way. With `modelProvider` set and no key in the environment, the route is blocked at once, naming the missing variable. An `adc` route is not asked: it fails within seconds with `authentication required`, which is classified.
- **Live-verified on agy 1.5.17.** Nine small signed-in runs (a scratch repository, the task on stdin as Nightweaver sends it) settled what 0.19.1 had to guess. The stream is `init` (conversation id, model), `step_update` (state `ACTIVE`, `DONE` or `ERROR`; types `user_input`, `agent_response`, `tool`, `system_message`, `finish`; an `agent_response` carries `text_delta` chunks, or its whole text in the `DONE` event when none was streamed, and its own `usage`) and `result` (`status`, `response`, `structured_output`, `num_turns`, `usage`). A success has no `finished_reason`. `init.model` is the model asked for with `--model`, which is what the served-model check reads. `usage` counts input and output tokens with the thinking tokens inside the output and the cache reads apart. On a resumed conversation `result.usage` is cumulative, so Nightweaver counts the sum of the call's own step usage; `result.duration_seconds` is also cumulative-looking there and is not used. A result counts as failed when its status is not `SUCCESS` or its `finished_reason` names an error, a timeout or an interruption. Actions refused under a deny rule are tool steps in state `ERROR`; they are not failures of the call, but a read-only role that answers after one is not taken as a result (`read_only_refused`). A subagent is one `subagent` step with `subagent_info` and none of the subagent's own steps. The tests replay the real captures (`tests/agy_streams`, with ids and local paths replaced). Not run live: a read-only instruction with no deny rules, `mcp(*)`, completed `notebook_edit`, `sed_file` and `multi_replace_file_content` steps (the model reported them unavailable) and the deny rules together with `--disable-slash-commands`; a subagent's write was refused by the rules in a live read-only call.

On both kinds a failure that another attempt cannot fix is recognised from the provider's own error text and exit code, and never uses up a repair attempt. A missing sign-in or key (Gemini exit 41, "Please set an Auth method", "authentication required", "Please sign in", an invalid key) and an untrusted folder or configuration error (Gemini exits 52 and 55) block the run with the remedy. "Exhausted your capacity" or "daily quota", `RESOURCE_EXHAUSTED` and a 429 park the run until the reset the message names, or for a backoff when it names none. Capacity texts that the Gemini CLI itself treats as terminal ("capacity exceeded", `MODEL_CAPACITY_EXHAUSTED`, "No capacity available for model") and its 429 footer ("request a quota increase") park the same way. A 403 `PERMISSION_DENIED`, which is what a key without access to a paid-only model such as Neon gets, a 404 and an unknown model block as model-access problems. agy checks `--model` and `--effort` itself before it calls anything and exits 1 with `invalid model selection (...)`: an unknown model blocks as `model_access` and an effort the model lacks, or a slug that conflicts with `--effort`, as `configuration`, each quoting agy's words. An overloaded model is retried as a transient failure. agy states an HTTP status as `Error 429, Message: ...` and, in its `AGY_ERROR` line, as `"error_code": 429`; both are recognised.

## Open the desktop dashboard

Install the .NET 8 SDK with the Windows Desktop runtime and run the following. Select **New OneShot** to choose a repository, inspect the detected checks and enter the goal. The same dialog accepts an existing plan template. **Start a plan** runs an explicit `plan.json`. Set `NIGHTWEAVER_PYTHON` to the path of your Python executable if `python.exe` is unavailable on `PATH`.

```powershell
dotnet run --project Nightweaver.Desktop -c Release
```

The compiled desktop app accepts `--home <runtime-directory>` and `--python <python.exe>` for shortcuts that need explicit paths.

The activity list names goal pursuit and unattended-run events in plain words, for example "Goal judged 72/100", "Waiting for usage reset until 18:40", "Worker stalled; retrying fresh" and "Reviewer shares the worker's vendor". Rescue, exploration, confirmation (`goal_confirming`, `met_unconfirmed`, `confirmation_skipped`) and `model_mismatch` events appear there under their event names. The New OneShot dialog does not offer pursuit, exploration or `wish` yet. Start those from the command line and watch the run in the dashboard.

## Use it on a project

Choose the repository root with the committed base you want. Set `repository`, `title`, `objective`, `worker`, `gates`, and ordered `milestones` in a local JSON plan. A milestone can specify a different `worker`, its own extra `gates`, and its allowed `paths`. `protected_paths` defends tracked checks and project boundaries during promotion. Run with trusted plans and projects. Custom commands run with your Windows permissions, and provider tool calls use the provider's permissions.

Set `expected_base` to a full Git commit hash to reject the wrong starting version. Nightweaver records the resolved checkout and serializes runs that share a Git directory, including linked worktrees.

### Register the canonical checkout

```powershell
python -m nightweaver project bind journal 'C:\Projects\field-journal'
python -m nightweaver project list
```

Set `"project": "journal"` in a plan or OneShot template to use that checkout. Omit `repository`, or supply it as an explicit assertion that must match the binding. A second clone is rejected even at the same commit. Bindings live in the selected Nightweaver runtime. Use `project bind journal <new-root> --replace` to redirect future runs. Already registered runs keep their original checkout and base.

- **OneShot.** Create a template with the repository, title, worker, and trusted checks. `python -m nightweaver goal template.json "Your goal" --planner claude --start` makes a fresh plan of up to five milestones and starts it. Omit `--start` to print the proposed plan first. A long goal can come from a file instead: `--objective-file goal.txt` (also on `oneshot`) reads UTF-8 text of up to 1 MB, which gets around the 32,767-character Windows command line. Use `--profile-dir` to select an existing planner configuration directory.
- **Checks.** Each gate has a `name`, an executable `argv` array, an optional `timeout` in seconds (a gate that runs out of time is run again once with twice the time before the run blocks; see [Run unattended](#run-unattended)), and an optional `artifacts` array of paths relative to the worktree. Put whole-project checks under `final_gates`. These run on the final candidate in addition to its regular checks. Use the `acceptance` array for requirements the worker and reviewer should carry across milestones, including visual criteria.
- **Profiles.** The `codex` and `claude` workers and reviewers accept `model`, `executable`, and `profile_dir`. Log into each chosen profile with its own CLI. The engine passes the matching configuration directory to that process. Use `reviewer` with `{"kind":"claude"}` or `{"kind":"codex"}` to require a fresh review. A custom `command` reviewer receives the brief on stdin and must emit a JSON `verdict` (`PASS` or `REVISE`) and `reason`.
- **Recovery.** `python -m nightweaver resume <run-id>` starts an interrupted or paused run again. After correcting an external blocker, use `resume <run-id> --retry`. Interrupted workers recover without using up a repair attempt, and their original attempt logs remain available. Use the dashboard or `events` to inspect the run.

### Check provider readiness

```powershell
python -m nightweaver doctor --plan plan.json
python -m nightweaver doctor --plan plan.json --planner deepseek --live
python -m nightweaver doctor --planner claude --profile-dir 'C:\Profiles\claude-work' --live
```

The report resolves the plan's checkout and identifies the executable, profile, model and configured reasoning effort for each role. A non-live report checks local configuration. With `--live`, Nightweaver sends a small test prompt per distinct model route. Live requests use your normal subscription limits or API balance. For Ollama, the live report also records the responding model and whether it returned thinking. Custom commands are checked for executable availability and run only as part of the plan.

Set `effort` on a Claude, Codex, DeepSeek or Antigravity worker or reviewer (the Gemini CLI has no effort setting). Use `--effort` for a CLI planner. For example, Opus 5.5 and Astra 6.2 accept `xhigh`. DeepSeek's highest native level is `max`. Antigravity checks the level per model when the plan loads, so Gemini 4 Neon accepts up to `xhigh` and no Gemini model takes `max` ([Use Gemini and Antigravity](#use-gemini-and-antigravity)). Explicit settings apply to the child process, including resumed calls.

The command exits with status 3 if a required model route is blocked, a live request could not be verified, or, with `--plan`, git resolves no author or committer identity in the plan's repository (the report says which `git config` commands fix it). On a run, a missing CLI, missing selected profile, or recognized account, model, or usage-limit error blocks with the affected role and profile. Correct that route and retry the existing run. [Claude profile settings](https://code.claude.com/docs/en/settings) · [Ollama model listing](https://docs.ollama.com/api/tags)

## Pursue a goal until it is met

A OneShot normally ends when its planned milestones are accepted. Add `--pursue` to keep going until an independent judge accepts the result.

```powershell
nightweaver oneshot C:\src\my-project "Build a polished pricing page" --pursue --judge claude --judge-model claude-opus-5-5 --judge-effort xhigh --waves 3 --wait
```

`--judge` chooses the judging provider. The default is a different model family from the worker: Claude, or Codex when the worker is Claude. `--judge-model`, `--judge-effort` and `--judge-profile` pick its model, reasoning effort and signed-in profile. The judge also plans each wave unless `--planner-kind` names another provider, with `--planner-model`, `--planner-effort` and `--planner-profile`. `--waves` sets the additional waves allowed after the initial plan (1–20, default 3). `--target-score` sets the score that counts as done (default 85). `--reference IMAGE` gives the judge a target to compare screenshots with, repeated up to six times. `--explore N` builds N design directions before milestone 1 and `--rescue-strategies N` sets the plateau rescue's strategy count (`0` turns it off). These flags require `--pursue`. `--planner-model`, `--planner-effort` and `--planner-profile` also require `--planner-kind`, and `--explore-workers` also requires `--explore`. `--reviewer` adds a reviewer for each milestone, with or without `--pursue`. Every provider call counts against `budgets.max_calls`. Each wave costs a planner call, a worker call for each attempt at each milestone, a reviewer call for each attempt if a reviewer is set, an advisory call for each reviewed build if a local advisory model is set and a judge call. A judgment that would finish the run adds a confirmation call. The live trial's one wave made 16 calls: 5 worker, 5 reviewer, 1 judge and 5 advisory. With `--pursue`, a OneShot whose budgets are still the default (15 calls, 1 hour) gets 80 calls and 6 hours, and `wish` uses the same limits. Set `budgets` in a template for other limits. `plan-lint` counts the confirmation in `minimum_calls_for_acceptance`.

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
| `target_score` | 85 (0–100) | The score a `MET` verdict must reach. Every ledger check must also pass. The controller applies it; no prompt ever shows it. |
| `confirm` | true | A judgment that would finish the run is confirmed by one more independent judgment of the same build. |
| `confirm_margin` | 3 (0–20) | The confirmation must say `MET` at `target_score` minus this or better. |
| `confirm_judge` | the judge | Another route for the confirmation, which falls back to `judge_fallbacks`. |
| `plateau` | 2 (1–5) | Consecutive judgments that fail to beat the best score before the pursuit stops. |
| `regression_margin` | 5 (0–50) | A wave scoring this far below the best build is discarded and the best build restored. |
| `references` | none | Up to six `.png`, `.jpg`, `.jpeg` or `.webp` images, relative to the plan file. |
| `criteria` | the plan's `acceptance` | What defines done. The judge answers each one. |
| `explore` | off | Builds divergent versions of milestone 1 first and keeps the winner. See [Explore design directions](#explore-design-directions-before-building). |
| `rescue` | on: 3 strategies, 1 rescue | What happens when the score plateaus. See [Rescue a plateau](#rescue-a-plateau). |

The judge reads the source, the check logs and up to twelve distinct screenshots from the ledger checks, plus any reference images. The ledger runs the checks that apply to the finished build: the plan's checks, its final checks and the checks retained from earlier milestones, not a check local to an earlier milestone. A ledger check that times out is run again with twice the time, like any gate. One that times out for good has not failed, so the judgment does not happen: the run blocks in `judging` with the same advice as a verification, and `--retry` re-runs the ledger. Each judgment keeps its own evidence folder, so one build's screenshots never reach another's judgment. A judge only sees stills: motion and timing are judged from source and logs, and a browser gate without a [sweep](#interaction-sweep) shows only each page as first loaded.

Every judge scores against one fixed rubric: 95–100 exceptional, 85–94 finished with only polish-level gaps, 70–84 solid with visible gaps, 50–69 partial, below 50 major gaps. In the live trial, a judge that was shown the pass mark (88) returned exactly 88, so `target_score` stays a threshold in the controller and no prompt shows it. `MET` needs every criterion fully met. When a judgment would finish the run, a second judge call looks at the same build in a fresh session with the screenshots in reverse order and without the first verdict. `MET` stands only if the confirmation also says `MET` at `target_score - confirm_margin` or better. Otherwise the pursuit records `met_unconfirmed`, continues as `NOT_MET` with the gaps of both judgments and scores the build by the lower of the two. By default the confirmation uses the judge's own route, so it guards against one noisy call but shares that model's blind spots; `confirm_judge` names another route. A failure of the confirmation route (a usage limit, a sign-in, a missing executable) is reported against that route, not the judge, and uses the same fallbacks and parking as the judge. When the call, time or token budget cannot fund the confirmation call, the judgment that met every condition stands without it: the run records `confirmation_skipped` with the reason, `pursuit` and `metrics` show it, and `plan-lint` reports the shortfall beforehand.

Builds are ranked by whether every ledger check passed, then by score, so a build that fails its own checks never replaces a green one. A judgment lists at most 30 gaps and 60 criteria, and `criteria` or `acceptance` may name at most 60. Planned waves inherit every plan milestone's paths together, or no path limit when any milestone had none, and the planner is told those paths and the protected paths.

A pursuit stops with `wave_limit` when `max_waves` is spent, `plateau` when progress stalls, or `milestone_limit` at 50 milestones. It leaves the best build on `nightweaver/<run-id>`. A milestone-limit stop cannot be extended with `--add-waves`.

### Explore design directions before building

For a goal judged by looking at it, the first build sets the look of everything after. `pursue.explore` builds several deliberately different versions of milestone 1 before one is kept. It is off unless set. `oneshot --pursue --explore N` turns it on with N directions (2–4), `--explore-workers codex,claude` names the workers that take the directions in turn (default: the primary worker), and `wish` turns it on, with three directions, for a visual goal.

1. **Directions.** One read-only planner call names N divergent directions, each with a name and a brief. Two unreadable answers skip the tournament and milestone 1 is built as planned.
2. **Build.** Each direction is built once, one after another, in its own worktree from the base commit, and must pass milestone 1's checks. A direction that passes is committed and saved at `nightweaver/<run-id>-direction-NN`. If none passes, milestone 1 is built as planned. A direction whose checks time out for good is recorded as unscored (`direction_unscored`, status `unscored`, never failed) and left out of the comparison; the report counts it apart. If every direction is unscored the run blocks in `exploring`, with the time-limit advice, and `--retry` builds the directions again.
3. **Compare.** The judge route compares the passing directions, each pair in both orders (`"comparisons": "pairwise"`, the default), or ranks them all in one call (`"single"`). It is told that every candidate passed the same checks and to weigh visual quality first. A win scores one point and a tie half a point per call. A pair whose two orders disagree is reported as position bias. Two unreadable answers record an unscored tie.
4. **Promote.** The top-ranked direction is promoted as milestone 1, and the pursuit goes on from there. A single passing direction is promoted without a judge call. The winner is promoted after its checks and the judge comparison (after its checks alone when only one direction passed). The milestone reviewer, if configured, is not run on a direction winner, so no reviewer looks at it before the next goal judgment.

```json
"explore": {"directions": 3, "workers": [{"kind": "codex"}, {"kind": "claude"}], "comparisons": "pairwise", "brief": "Differ in palette, layout and motion, not only in color."}
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `directions` | 3 (2–4) | Directions planned and built. |
| `workers` | milestone 1's worker | Up to four workers that take the directions in turn. |
| `comparisons` | `pairwise` | `pairwise` judges each pair twice, in both orders. `single` ranks every passing direction in one call. |
| `brief` | none | Up to 4000 characters of guidance on how the directions should differ. |

Exploration costs one planner call, N worker calls and, with three passing directions, six judge calls (twelve with four); `single` costs one judge call. `pursuit` shows each direction, whether it passed, its score and the winner, and `report` adds a Design exploration section. A hook for the owner to choose between the two best directions exists but returns no choice yet, so the judge always decides. Nothing measured so far shows that exploring beats building one direction and criticising it, and neither the live trial nor the head-to-head used it.

### Rescue a plateau

When the score stops improving, repeating the same kind of wave rarely helps. Before a pursuit stops with `plateau`, Nightweaver runs one plateau rescue:

1. **Diagnose.** One read-only call explains why progress stalled and proposes N genuinely different strategies, at least one a bold rethink. It sees the goal, the criteria, every judgment with its top gaps, the milestones built in each wave, recent rejection reasons and the best build's screenshots. The planner diagnoses unless it comes from the worker's vendor family, in which case the judge does. Two unreadable answers skip the rescue and the pursuit stops as before; two failed calls (a timeout or a crash) block the run instead, so `--retry` diagnoses again with the rescue unspent.
2. **Fork.** Each strategy gets one attempt in its own worktree at the best commit, by a rotating worker (default: the last milestone's worker). It runs the last milestone's checks and, on a pass, is committed on the best build and saved at `nightweaver/<run-id>-rescue-wNN-II` (a second rescue at the same wave adds its number, `-wNN-r2-II`, so the first one's refs and evidence stay). A strategy whose checks time out for good is recorded as unscored (`rescue_candidate_unscored`) and left out of the comparison. If every strategy is unscored the run blocks in `rescuing` with the rescue unspent, and `--retry` builds them again.
3. **Select.** The judge compares the best build with each passing strategy in both orders. A strategy qualifies only by scoring strictly more than half the points over a pair the judge read in both orders, so an answer that depends on the order shown never wins, and neither does a strategy whose other order was unreadable. If the judge cannot be read at all, the run blocks for `--retry` and the rescue is not spent. The controller runs the last milestone's checks on the best build as well, so both candidates are shown screenshots from the same checks; if one of those checks times out for good the run blocks in `rescuing` and `--retry` checks the best build again. Qualifiers with equal scores meet each other, and the lower number breaks any remaining tie.
4. **Promote or stop.** A winner is appended as the milestone `Rescue: <strategy>` and promoted after its checks and the judge comparison, then judged as the next wave. The milestone reviewer, if configured, is not run on a rescue winner, so no reviewer looks at it before that judgment. If none wins, the pursuit stops with `plateau` on the best build, exactly as it would have.

```json
"rescue": {"strategies": 3, "max_rescues": 1, "workers": [{"kind": "codex"}, {"kind": "claude"}]}
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `strategies` | 3 (2–4) | Strategies the diagnosis proposes and the rescue builds. |
| `max_rescues` | 1 (0–3) | Rescues one run may start. `0` turns the rescue off and keeps the earlier behavior exactly. |
| `workers` | the last milestone's worker | Up to four workers that build the strategies in turn. |

`oneshot --pursue --rescue-strategies N` sets the strategy count, and `--rescue-strategies 0` turns the rescue off (`--rescue` still names an alternate repair worker). A rescue costs one diagnosis call, one worker call per strategy and two judge calls per passing strategy, and the judgment of the winner (plus its confirmation) must still fit after that; when `budgets.max_calls` has fewer calls left than that minimum, or the time or token budget is spent, the rescue is skipped and the pursuit stops as a plateau, so `--add-waves` still works. `pursuit` shows the diagnosis, the strategies, their scores and the winner, and `metrics` reports `rescue`: how many were attempted and won, the winning strategy and the score before and after the next judgment, so you can see whether rescues pay off.

### Read and extend a pursuit

```powershell
nightweaver pursuit RUN_ID
nightweaver pursuit RUN_ID --add-waves 2
```

`pursuit` prints each wave with its milestones, the judgment, the top gaps, any failed ledger checks, any revert, and the design exploration and any rescue. Add `--json` for the same data as JSON. `--add-waves N` raises `max_waves` by N (1–20). A run that stopped after judging a build goes back to the planner with that build's gaps and a fresh plateau count, because judging the same build again would add no evidence; resume it or let the dispatcher continue it. Milestones of a discarded wave are listed as discarded (not accepted) by `handoff`, `pursuit` and `timeline`, and `fork` and `replay` leave them out; `metrics` and `report` count only the build's own milestones. A run that discarded a wave under an earlier version is read from its `pursuit_reverted` events and recorded the next time it resumes. `report` adds a Goal pursuit section and `handoff` lists the scores and open gaps. `doctor` and `preflight` check the judge, its fallbacks, the confirmation judge and the planner.

### Start from one line

`nightweaver wish` turns one line into a complete pursuit run. The target is a repository folder or a bound project name.

```powershell
nightweaver wish C:\src\my-project "Make the pricing page feel finished" --preview
nightweaver wish C:\src\my-project "Make the pricing page feel finished" --start --wait
```

`--preview` writes the brief and the template and stops before the pursuit planner is called, but it still makes the one brief-writer model call (step 1 below) and spends usage on it. With neither flag, `wish` saves the same files and prints the exact commands to start the run. `--start` plans with the pursuit planner and starts a dedicated runner (`--wait` returns the outcome, `--guard-restarts N` adds crash recovery). `--waves`, `--target-score` and `--reference IMAGE` work as they do for `oneshot --pursue`.

1. **Brief.** A read-only brief-writer call (Claude `claude-opus-5-5` at `xhigh`; change it with `--brief-kind`, `--brief-model` and `--brief-effort`) reads the wish, a repository map, README, DESIGN, AGENTS and CLAUDE excerpts (12 KB in all) and the checks discovery found, and may read the repository with read-only tools. It returns an objective, 4 to 8 observable acceptance requirements, a `visual` flag, a `web` flag and any conflicts it resolved.
2. **Roster.** `--roster default` uses Codex (`gpt-6.2-astra`, `xhigh`) as the worker, Claude (`claude-opus-5-5`, `xhigh`) as reviewer and judge and DeepSeek (`deepseek-v4-pro`, `max`) as the wave planner, plus a local advisory model when an Ollama server on `127.0.0.1:11435` (not the default 11434) lists `qwen3.8:27b` within 2 seconds (think `xhigh`, 4096 output tokens, 600 seconds, not required). Otherwise it is left out and the summary says why. `--roster fast` keeps every CLI default and has a Codex worker and a Claude judge, with no reviewer and no advisory.
3. **Pursuit settings.** Up to 3 additional waves of 2 milestones, target score 88, a stop after 2 flat judgments, 80 model calls, 6 hours, 3 attempts per milestone and 30 minutes per call. A visual goal also gets a three-direction [exploration](#explore-design-directions-before-building), whose winner is promoted without the milestone reviewer, so when a direction wins, the wish's only initial milestone is not reviewed.
4. **Saved files.** `<home>/wishes/<id>/` holds `brief.json`, `template.json`, `objective.txt` and the raw brief-writer log. The printed summary shows the brief, the checks, the roster and the limits. If the plan is refused after the brief is written, the files stay and the message says where.

**Visual evidence for a wish.** Only a browser gate gives the judge and the milestone reviewer screenshots, so a visual wish gets one when its discovered checks have none.

- A visual wish on a repository whose discovered checks include no browser gate, such as one with committed tests, gets a `Visual smoke` browser gate beside them, never instead of them. It runs on the first of `index.html`, `public/index.html`, `docs/index.html`, `dist/index.html`, `build/index.html`, `site/index.html` and `www/index.html` that is tracked at HEAD, served from that page's folder, with the [sweep](#interaction-sweep) on.
- The brief writer's `web` flag says whether a committed page is what the wish changes. A repository that imports a desktop or game toolkit (`tkinter`, `pygame`, `PyQt`, `PySide`, `kivy`, `curses` and the like) is taken not to have one unless the brief says `web: true`.
- A page is skipped when a bare file server cannot show it as the site: one that only redirects (a `meta refresh` or a tiny `location` assignment), template source with front matter, generated output (a `generator` meta tag, Sphinx, MkDocs, Doxygen and similar), and `dist/` or `build/` pages when `package.json` has a `build` script, because nothing in the run rebuilds them and the screenshots would never show the worker's edits.
- The gate is run on the committed baseline in a throwaway checkout that is deleted afterwards (its evidence stays in `visual-probe/` beside the brief), and run a second time if the first run fails for a reason other than horizontal overflow. It is added only when it passes there, or fails only on horizontal overflow, which the worker can fix. A page that calls a backend, a bundler's bare module imports or any other console error on the untouched baseline would fail every milestone, so no gate is added for it.
- With no usable page, no browser on the machine, a `dev`, `start`, `preview` or `serve` package script, or a failing baseline, nothing is added. The summary then notes that the judge (and the reviewer, if any) will have no screenshots and suggests a template with a browser check that owns the server, and `--start` goes on without the gate.
- The acceptance line that the page loads cleanly at both sizes is added unless one brief line already names both viewports, no console or page errors and no horizontal overflow.
- For a new repository the brief's `visual` flag decides, since there is no page to inspect. A non-visual wish for a new repository is refused after the brief is written, with nothing saved, because there is nothing to check yet.

The tests exercise the brief writer, the roster and the Visual smoke gate with scripted routes. Neither the live trial nor the head-to-head started from a wish.

### Run unattended

- **Usage limits.** When a provider reports a usage limit, Nightweaver reads its reset time, parks the run and retries 90 seconds after the reset. The worker attempt is refunded. If the message gives no time, the retry comes after 30 minutes, doubling up to 2 hours. `retry.max_parks` (default 8 consecutive waits, `0` turns waiting off) and `retry.max_park_seconds` (default 21600, range 60–604800) bound the waiting. A reset that falls after the run's time budget blocks with a clear message. Parked runs resume through the runner, the dispatcher and the guard. A parked inline runner keeps the repository lock while it waits.
- **Time budget.** `budgets.max_seconds` is active time: the time since the run started less the stretches it sat blocked (waiting for a person) or paused. Time parked on a usage limit still counts, because the run is waiting on its own clock. The state keeps the ledger of those stretches, and a run made by 0.19 has it read once from its events (an `active_time_derived` event). `budget` and `metrics` show active, idle and wall time side by side.
- **Gate timeouts.** A gate the controller ends at its time limit is not a failure of the code. Repositories grow past a limit set early, and a busy machine is slow. It is run again once in a fresh evidence folder with twice the time (never above 14,400 seconds), recorded as `gate_timeout_retried`; `retry.gate_timeout_retries` (0 to 3, default 1, 0 for the 0.19 behavior: the first timeout blocks at once, with the same words) sets how often, and the raised limit holds for the rest of that verification, so its other stability samples do not pay the old limit again. A proof gate whose run on the baseline commit times out counts too. A gate that times out for good is never a rejection of the worker's change: a verification blocks naming the gate, the limits it ran with and what to do. A run keeps the plan it was created with, so raise `timeout` (up to 7200 seconds) in the plan and add that plan as a new run, or, if the machine was only busy, resume with `--retry`. The same gate is unscored, not failed, in the judge's ledger and among the candidates of an exploration or a rescue, as described under [pursuing a goal](#pursue-a-goal-until-it-is-met).
- **Silent workers.** Plans written by 0.19 mark themselves (`idle_default`), and their workers, reviewers, judges and planners get a `budgets.idle_timeout` of 900 seconds unless the plan sets one (1–14400). A plan saved by 0.18 keeps the behavior it was approved with; add `budgets.idle_timeout` to opt in. A worker that goes silent is retried in a fresh session and its attempt is refunded, at most twice in a row.
- **Reviewer failures are not worker failures.** A reviewer that stalls, times out, crashes, answers unreadably twice or passes without a reason is retried like any transient provider failure (`retry.transient_attempts`, after any `reviewer_fallbacks`). The worker's attempt is never charged. If the retries run out, the run blocks in `reviewing` with a `Reviewer unavailable` cause, and `--retry` resumes the review. An answer that holds several verdict objects that disagree, or one that cannot be read (cut off, a stray quote, a trailing comma) next to one that can and says something else, counts as unreadable, wherever the broken one stands, and so does an object-shaped fragment left dangling after the verdict. A format reminder such as `"PASS" or "REVISE"` describes nothing.
- **Big repositories.** A top-level `git_timeout` (30–3600 seconds, default 90) governs git calls. `git worktree add` always gets at least 900 seconds, and a timed-out checkout ends git's whole process tree, is unlocked, removed and pruned, and is explained, so `--retry` starts clean. A runner that dies during the checkout does not take git with it: git finishes and releases its lock, and a resume that finds a checkout still locked as initializing waits for it, or clears it away when no process holds it any more. Every run applies its own `git_timeout` from its first git call, also when a restart resumes it in judging, planning or promoting.
- **Tool droppings.** A new `debug.log`, `chrome_debug.log`, `*.dmp` file or `.playwright-mcp/` folder that the base lacks is not a candidate change. These files are deleted from the milestone worktree before the checks run, so no check passes on a file the accepted commit lacks. A milestone that names the file, or a folder holding it, in its `paths` keeps it, and so does a tracked file the milestone moved. A check that fails after a deletion says which files went and how to keep one, in the rejection and in the next worker prompt. Failure fingerprints and the no-progress check use the same filtered build. Only these names count as droppings.
- **Git identity.** The accepted commits are made by Nightweaver, and git stops with "Author identity unknown" in a repository where it resolves no author or committer identity. Launching refuses such a repository up front, naming the `git -C <repo> config` commands that fix it, quoted for cmd and PowerShell. A run that started before the check existed does not lose its work at promotion: the commit is made as `Nightweaver <nightweaver@localhost>` for whatever git cannot resolve, and `milestone_accepted` records `identity_fallback`. A configured name or email stays: with only `user.name` set, the commit keeps that name and takes the fallback email, and `identity_fallback` lists which half was filled.
- **Claude sign-in.** A Claude login can expire while a run is unattended. Run `claude setup-token` once. Keep the result in the user environment variable `CLAUDE_CODE_OAUTH_TOKEN`, which Nightweaver also reads from the registry when a process started before it was set, or in a file named by the route's `oauth_token_file`. The route's `oauth_token_file` wins over the variable. A running dispatcher or guard prefers the registry's value over its older process copy, so a token you rotate reaches new Claude calls without a restart. A route with its own `profile_dir` uses that profile's login and never receives the ambient or registry token (its own `oauth_token_file` still applies). Only Claude routes receive the token: a `command` route gets it only with `inherit_credentials`, gates and git never do, and key-shaped tokens are masked in gate logs. `doctor` names the source in use. An expired login blocks with the `claude setup-token` remedy instead of quietly leaving review to the worker's own vendor.
- **Claude's own config directory.** With a long-lived token, Claude workers, reviewers, judges and planners run with `CLAUDE_CONFIG_DIR` set to `<home>/profiles/claude`, so the operator's interactive `CLAUDE.md`, skills and settings do not steer an unattended run. A standalone Claude baseline once built nothing because it obeyed an interactive rule to delegate to background subagents, which print mode then killed. The directory is not used when the route sets `profile_dir`, when `CLAUDE_CONFIG_DIR` is already set, or when `NIGHTWEAVER_CLAUDE_SHARED_CONFIG=1`. Without a token, a Claude route uses the CLI's default profile, so the interactive configuration applies. A Claude session resumes only under the config directory that created it, and a session saved under another directory starts a fresh one, which includes runs begun by 0.18.
- **Served model.** Each provider call records the model that was actually served as `served_model` on `provider_call_finished`: for Claude and DeepSeek from the model on each assistant message of the call's log, for Gemini from its init event and result stats, for Antigravity from the init event of `agy`'s stream (confirmed on agy 1.5.17), and for Codex (whose `--json` stream names none) from the session rollout's turn context. A route that asked for a particular model and was served another emits `model_mismatch` once per route and served model in a run, after normalizing case, vendor prefix, dated snapshots and family aliases. `insights` and the report list it as a problem and it is a default notification event. It records the difference. It does not change the route or stop the run.
- **Independence.** When a reviewer or judge comes from the same known vendor family as the worker (Codex, Claude, DeepSeek or Google, which covers both `gemini` and `antigravity`, so a Gemini CLI worker with an Antigravity judge is recorded; a `command` route has no known vendor and never warns), the run records `independence_degraded` once for that pair and continues.
- **Credentials.** Each model process keeps only its own vendor's credentials: Claude keeps `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_*` (plus AWS or Google credentials when `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX` is on), Codex keeps `OPENAI_*` and `CODEX_*`, Gemini and Antigravity keep `GEMINI_*` and `GOOGLE_*`, DeepSeek keeps `DEEPSEEK_*`. Gates, judge-ledger gates, git, suite adapters and `command` routes (which include external contenders) get an environment without any variable whose name says it holds a credential (ending in `_TOKEN`, `_API_KEY`, `_SECRET`, `_PASSWORD`, `_CREDENTIALS`, `_ACCESS_KEY` and similar, with or without `_FILE`, plus `AWS_BEARER_TOKEN_*`) nor Claude Code's session socket, so worker-written test code cannot read them. The scrub works by name. A secret in a file or in a variable with an unremarkable name is not removed, and a check that needs a credential from the environment will not find it. A `command` route that really needs one, such as a Claude coordinator that signs in with `CLAUDE_CODE_OAUTH_TOKEN`, sets `"inherit_credentials": true`; `examples/create_comparison.py` does this for its AGTX Claude coordinator.

### Phone notifications

Nothing is sent until you run `nightweaver notify setup`. Notifications use [ntfy](https://docs.ntfy.sh): Nightweaver posts to a topic and the ntfy phone app subscribes to it.

```powershell
nightweaver notify setup
nightweaver notify test
nightweaver notify status
nightweaver notify digest --hours 12
nightweaver notify off
```

- **Setup.** `setup` writes the configuration to the runtime's `providers.json` with fresh random topic names (a message topic and a private reply topic), prints how to subscribe and sends nothing. The server is `https://ntfy.sh` unless you pass `--server`, and a server that is not https is flagged because messages then cross the network unencrypted. `--events` chooses events, `--quiet-hours HH:MM-HH:MM` sets the night window (default 23:00-07:00, `''` turns it off), `--max-per-night N` sets the allowance (default 3), `--no-images` never attaches screenshots and `--rotate` replaces the topics. `status` shows the configuration with the topics partly masked, `test` sends one message and `off` removes the configuration.
- **Privacy.** An ntfy topic has no password: anyone who knows the topic name can read what is posted to it. The generated names are random, so treat them as secrets, do not paste them into chats, issues or screenshots, run `setup --rotate` if one leaks, and use your own server if you do not want a public one to see the messages. A message carries the run's title, a one-line summary and the run id. Raw error text is cut to its plain leading words, and known failures become fixed phrases, so file contents, paths, tokens and provider log text are not sent.
- **Events.** By default: `goal_met`, `run_completed`, `pursuit_stopped`, `blocked`, `provider_unavailable`, `provider_parked`, `model_mismatch`, `budget_exhausted`, `repeated_failure_stopped`, `verification_inconsistent`, `local_advice_required`, `guard_stopped` (only when the restart limit is reached) and `dispatcher_run_failed`. These cover the stops a live runner, guard or dispatcher can report, though a failed send or a quiet-hours hold can delay a message. A runner that dies with neither a guard nor a dispatcher is not reported. `notify digest` lists it as still running for as long as its last update falls inside the digest window. Also available: `wave_completed`, `milestone_accepted`, `judge_invalid`, `plan_invalid` and `independence_degraded`. A list saved before the newer stops existed is upgraded; a customised list is kept, and `setup` names what it leaves out.
- **Quiet hours.** During quiet hours at most `--max-per-night` messages go out, and a usage-limit wait never uses the allowance and appears only in the digest. The rest are held and sent as one message soon after quiet hours end, by whichever dispatcher or run is active then, or by `notify digest`, which sends (or with `--print` prints) a summary of the last 12 hours by default.
- **Failures.** A failed send never raises, retries in a loop or stops a run. The run records one `notify_failed` event, and the held message waits for the next release.
- **Decision inbox.** The notification module can send a question with up to three buttons and read the tapped answer from the private reply topic. No run asks the owner a question yet. The design-direction tournament's owner choice is not connected to it, so treat the inbox as a building block, not a feature you can use.

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

### Smoke gates and the web hint

A OneShot for a repository with no committed tests gets a `Browser smoke` gate when the repository is a static web project (an `index.html` at the root, in `public/` or in `docs/`) or a new repository holding only a README, license or ignore files whose goal is a web page. The gate loads the page at desktop (1440x900) and phone (390x844) sizes and keeps the screenshots, so a goal can start from nothing.

- **Is a new repository's goal a page?** The OneShot reads what the objective asks for. A page, site, landing page, dashboard, game or calculator is a web goal, while a command line tool, script, library or scraper never is, whatever pages it handles. This is a keyword heuristic over the objective's first sentence, so an unusual phrasing reads as not a page and the OneShot asks for `--web`. `--web` or `--no-web` overrides the heuristic, and `oneshot` says on stderr when it judged a goal to be a page. `wish` has no such flag. The brief writer's visual flag decides there.
- **Site root.** A page in `public/` or `docs/` is served with that folder as the site root, set by the gate's `root` field, so root-absolute asset URLs resolve as they do when deployed. A file that folder does not have is served from the rest of the checkout, so `../dist/lib.js` or `/docs/style.css` still load. In your own gate, `root` is a folder in the checkout, needs a local `page` inside it and cannot be combined with `offline`.
- **Never in place of real tests.** A repository that has tests discovery cannot run is never given a smoke gate, because a smoke check would stand in for its real tests. That covers a `go.mod` beside Go files, `Cargo.toml`, `pom.xml`, a `Gemfile` that names a test gem, `tox.ini`, a `*_test.*` or `*.spec.*` file, a `tests/` folder holding anything but documents and pictures, a `.bats`, `.robot` or `.feature` file, a Makefile `test` or `check` target, a test runner's config and similar. A script merely named like a test, such as `typing-test.js`, counts only when its text calls a test runner. Such a repository keeps the "No supported committed test suite found" failure, which shows how to add a template gate.
- **Apps with a server.** A repository whose `package.json` has a `dev`, `start`, `preview` or `serve` script is not given a smoke gate either, because a bare file server cannot render it. The failure message points to a browser gate that owns the server.
- **Visual wishes.** A visual `wish` on a repository that has tests gets its own baseline-probed `Visual smoke` gate. See [Start from one line](#start-from-one-line).

### Edge or Chrome fallback

- When the bundled Chromium is missing, a browser gate that uses the default retries with Microsoft Edge and then Chrome. The channel that ran and the reason are recorded in `browser.json` and the gate receipt's `browser_runtime`.
- An explicit `channel` or a `PLAYWRIGHT_BROWSERS_PATH` pin still fails closed, and so does a gate with a `visual_match` step, because pixel baselines differ between browser builds. `preflight` and the browser check `goal --start` makes before planning both report it.
- A non-bundled browser takes one throwaway screenshot before the timed cases, because Edge draws its first screenshot slowly.
- The favicon exemption covers only the browser's own failed same-origin `/favicon.ico` fetch when no page of the case (one the journey leaves, or a popup) names that file. Another origin, another status, a script's or the page's own request and a declared icon still count as console errors.
- `doctor` reports `browser` as `ready`, `ready_fallback` or `unavailable` from the install folders without starting a browser.

### Interaction sweep

Add `"sweep": {"max_controls": 8}` (1 to 16, default 8) to a browser gate, and the OneShot smoke gate carries `"sweep": {}`. After the desktop viewport (the one named `desktop`, else the first) loads cleanly, the supervisor clicks up to that many visible, enabled controls and photographs every state, so reviewers and judges see what sits behind a control instead of only the first load. Without a `sweep` key a browser gate behaves as before.

- **What it clicks.** In page order: `button`, `[role=button|tab|radio|switch|checkbox]`, `input[type=button]`, radio and checkbox inputs, `summary`, `[aria-pressed]`, `[aria-expanded]`, `[onclick][tabindex]`, hash-only anchors that act as widgets, and up to 6 other options of each `select`. A radio or checkbox hidden behind a styled label is clicked through its label. After each click it waits until the page stops changing and saves a `sweep-NN-<name>.png`. It lists the page again after every click, so controls an intro overlay or a menu was hiding are swept once they appear. It clicks and selects. It does not type or drag, and it stops at the control limit or a 45-second budget.
- **What it leaves alone.** Real links, submit buttons inside forms, disabled, hidden, covered or off-screen controls, duplicates of the same role and name, and anything with a word in its name, or in its `id`, `name`, `data-testid`, `title` or `aria-label` (split into words, so `btnDelete` counts), that starts with delete, remove, reset, clear, erase, log out, sign out, buy, pay, purchase, submit, send, subscribe, cancel, empty, discard, archive, drop, destroy, wipe, revoke, deactivate, disconnect, publish, deploy, confirm, checkout, order, donate, transfer or withdraw. The match is on any word, so it also skips a control such as `Night mode reset`.
- **Containment.** While the sweep runs, every write (anything but GET and HEAD) and every navigation to another site is answered locally with an empty success, so nothing leaves the machine, and each row counts its `blocked_requests`. Add `"allow_writes": true` to let writes to the page's own origin through.
- **What a click can do.** A click that navigates away (even a moment later), opens a popup, a native dialog or a modal is recorded, and the original page is reloaded before the next control. A gate with `steps` cannot replay them, so there the sweep ends with a note instead of reloading. A popup is credited to the control whose click led to it, however late it comes: it follows the page's own timers (`setTimeout`, `setInterval`, animation frames, and the code after an `await` of one) back to the click, so a popup that opens three seconds later, or on every tick of an interval, is that control's and not the next one's. A popup that follows a network round trip is credited to whichever control was clicked last. Every popup, and any it opened in turn, is closed after each control; a page that opens popups faster than they can be closed ends the sweep with a failure that says so. A click that hangs the page ends the sweep with a failure that names the control, instead of hanging the gate, and so does a browser that stops answering altogether (a page that ran out of memory can leave it so): every call the sweep makes to the browser has a deadline of its own, 10 seconds past the call's timeout, and a call that misses it is abandoned. A console error, page error or new horizontal overflow caused by a click fails the gate like a load-time one, and the failure names the control (`After clicking 'Night': ...`).
- **Evidence.** The sweep screenshots are hashed with the other gate artifacts. `browser.json` gains a `sweep` list, and the gate receipt gains a `browser_sweep` summary and a `browser_sweep_coverage` (found, clicked, not clicked, left alone, never reached, why it stopped). The trace and the request budget cover the page load only. Reviewers and judges see the first load of each viewport first (labelled with the steps it followed, or the point of failure), then the swept states in order, each labelled with the control it shows, then one line of sweep coverage so a partial sweep never reads as a full one.
- **Not used in the live trial.** The sweep was added after it. The tests exercise the sweep on local test pages.

## Runtime data and local advice

Run data, model output, and evidence live under `%LOCALAPPDATA%\Nightweaver` or the directory chosen through `--home` or `NIGHTWEAVER_HOME`. Treat them as local project data. The engine creates a result branch locally. Review its commits before merging them into your branch or pushing them to a remote.

Upgrading from the original Rookline release preserves its existing runtime directory, run history, evidence, provider configuration, and result branches. New runs use `nightweaver/<run-id>`. The old `python -m rookline` command and `ROOKLINE_HOME` remain supported for existing scripts. No history or worktrees are moved automatically.

For local advice, set `think` to a value supported by the installed Ollama model and allow room for both thinking and its final answer with `max_tokens`. Defaults are thinking off, 100 output tokens and a 90-second timeout. Set `required` to `true` when every accepted milestone needs a local response. A failed required call blocks at review and can be retried with `resume --retry`.

[Design rationale and current limits](docs/design-rationale.md)

## Run the integration tests

```powershell
python -m unittest discover -s tests -v
dotnet build Nightweaver.Desktop -c Release
```

The suite has 1,088 tests. Version 0.19 added 656 in seventeen modules, counted from the test methods. `test_release_019_pursuit` has 25, `_browser` 35, `_runtime` 55, `_repair` 24, `_notify` 54, `_wish` 25, `_explore` 37, `_sweep` 68, `_rescue` 35, `_wish_visual` 25, `_isolation` 4, `_stall` 8 and the review-fix modules `_fix_pursuit` 62, `_fix_repair` 53, `_fix_browser` 45, `_fix_env` 50 and `_fix_notify` 51. Version 0.19.1 added 174 in four: `test_release_0191_core` 28, `_gemini` 83, `_runtime` 47 and `_unscored` 16. Version 0.19.2 added 56 in `test_release_0192_agy`, which replays the streams agy 1.5.17 really wrote. They use scripted workers, judges and planners, faked gate timeouts, mocked provider processes and those captures, with no paid model requests. To run only those (the pattern matches the 0.19.1 modules too):

```powershell
python -m unittest discover -s tests -p "test_release_019*.py"
```
