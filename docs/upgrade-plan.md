# Nightweaver research upgrade

Research date: 2026-11-18. These are ranked for Nightweaver's current gaps and the owner's Windows, multi-provider, visual-quality and unattended-development requirements. This is a focused survey of relevant upstream projects, not a claim to have searched every GitHub repository. Implementations are original; upstream projects supply design references, not copied source.

Implemented in version 0.3.0. The [usage guide](v0.3-guide.md) describes every control and its limits; [source metadata](research-sources.json) records the repositories checked. Ranking favors independently accepted results, fewer operator interventions, recoverability and native Windows compatibility.

## Ten new features

| Rank | Feature | Acceptance |
|---|---|---|
| F1 | Persistent run budgets | Provider-call, elapsed-time and observed-token limits survive restarts and prevent further work when exhausted. |
| F2 | Explicit provider fallback | Worker and reviewer routes can switch to an operator-configured alternative; sessions never cross routes. |
| F3 | Repeatable benchmark suites | Freeze task baselines, create repeated configurations, retain every attempt, and compare acceptance and resource metrics. |
| F4 | Declarative browser journeys | Exercise local pages in multiple viewports, preserve screenshots and traces, and send actual failures back to workers. |
| F5 | Gemini CLI adapter | Support native headless worker/planner/reviewer routes, profile selection, structured output and usage receipts. |
| F6 | Fork from an accepted checkpoint | Start a new run at a selected accepted milestone without rewriting the original run or source branch. |
| F7 | Pinned context packs | Carry selected committed context files and a bounded repository map into fresh sessions with content hashes. |
| F8 | Durable operator feedback | Append corrections to a run, record their timing, and include them in subsequent worker/reviewer prompts. |
| F9 | GitHub issue intake | Read one explicitly selected issue into a local plan using an existing template; never post or mutate the issue. |
| F10 | Portable run reports | Export escaped, redacted standalone HTML and machine-readable metrics with the complete event history. |

## Ten improvements

| Rank | Improvement | Acceptance |
|---|---|---|
| I1 | Durable exponential retry delays | Transient failures use bounded persisted delays; the dispatcher remains free to serve other work. |
| I2 | Repeated-failure circuit breaker | Stop identical failing output with unchanged implementation rather than spending every remaining attempt. |
| I3 | Strict versioned plan validation | Reject misspelled fields, invalid types, malformed paths and unsupported versions before launch. |
| I4 | Defensive provider output parsing | Handle malformed/non-object JSONL, bounded lines and provider-specific usage without crashing or inventing success. |
| I5 | Secret-safe exported diagnostics | Redact common credentials in events, feedback and exports; raw local logs remain private. |
| I6 | Atomic evidence publication | Publish context, checks, benchmark manifests and reports by atomic replacement. |
| I7 | Bounded, verified artifacts | Reject escaping paths, non-files and oversized evidence; preserve hashes and failed-check evidence. |
| I8 | Process health limits | Stop idle or excessively noisy owned processes; emit heartbeats and preserve Windows Job ownership. |
| I9 | Resilient queue dispatch | Isolate bad runs, skip future retry deadlines and distinguish failed prerequisites from running prerequisites. |
| I10 | Complete cursor-based timelines | Paginate beyond the former 80-event window; derive metrics from complete history and update desktop controls. |

## Primary sources and adaptation

| Repository | Relevant evidence / adaptation |
|---|---|
| [fynnfluegge/agtx](https://github.com/fynnfluegge/agtx) | OneShot milestone waves and autonomous board control; preserve our explicit, independently checked acceptance boundary. |
| [OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) | Workspace separation and event-based execution; structure context and evidence without adopting its server stack. |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | Durable state, feedback and checkpoint replay; adapt to SQLite and native Git worktrees. |
| [temporalio/sdk-python](https://github.com/temporalio/sdk-python) | Durable retries, timeouts and heartbeats; use persisted deadlines without requiring a Temporal service. |
| [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | Simple trajectories and repeatable evaluation; retain original logs and explicit run budgets. |
| [SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench) | Reproducible starting states and external acceptance checks; do not equate our fixture tests with benchmark results. |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | Declarative comparison matrices and assertions; adapt to complete coding runs. |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | Explicit retry/fallback routing and usage accounting; retain subscription CLIs instead of replacing them with API billing. |
| [Aider-AI/aider](https://github.com/Aider-AI/aider) | Repository maps and bounded context; include a simple tracked-file map, not an AST symbol ranking claim. |
| [microsoft/playwright](https://github.com/microsoft/playwright) | Browser journeys, viewport isolation, screenshots and traces. |
| [google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) | Official headless JSONL contract, CLI flags and profile environment. |
| [cli/cli](https://github.com/cli/cli) | Structured, read-only issue intake through the user's authenticated GitHub CLI. |

## Boundaries

Benchmarks measure the configured workflow. A Nightweaver-wrapped worker is not automatically a standalone Codex/Claude/AGTX benchmark; an external contender must launch its own complete workflow through a command route. Token receipts are observed CLI telemetry, may be unavailable, and are not subscription charges. Token limits stop subsequent calls and can overshoot by the final in-flight call. Browser checks do not establish aesthetic merit. Gemini live access depends on a separately installed/authenticated CLI. No persistent Windows task or additional paid provider account is enabled by this upgrade.

## Verification map

- F1–F2, F6–F8 and I1–I2: `tests/test_features.py` exercises budgets across restarts, worker and reviewer fallback, durable deadlines, checkpoint forks, pinned context, feedback arriving during review and unchanged failures.
- F3 and F10: the fixture comparison retains both success and failure; reporting tests cover complete event history, escaping and redaction. Fixtures do not measure AI performance.
- F4 and I7: real browser tests require an initially failing mobile layout, successful repair, two viewport screenshots and traces for each completed check. Artifact limits and stale-evidence rejection are covered separately.
- F5 and I4: Gemini stream/profile/flag contracts, malformed JSONL and strict structured responses are tested. Account authentication is a separate live-readiness check.
- F9: a mocked contract test ensures issue intake uses `gh issue view` and retains trusted template checks; read-only intake was also exercised against a public AGTX issue.
- I3, I5–I6 and I8–I10: tests cover misspelled settings, Windows paths, atomic-write interruption, credential redaction, bounded child processes, queue isolation, failed prerequisites and event cursors. Existing Windows Job termination/restart tests remain in place.
- A live Codex worker plus fresh Codex reviewer completed a one-milestone smoke task with two recorded invocations and no human intervention. This verifies the adapter/evidence path, not comparative model superiority.
