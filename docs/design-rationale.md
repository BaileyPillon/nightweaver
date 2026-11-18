# Design rationale

Nightweaver addresses recurring failure modes in long-running coding work. Verify the exact project and version, pass the next agent the goal and the real failure, and make every advancement traceable to its evidence.

| Failure mode | What Nightweaver does |
| --- | --- |
| Several sessions or replicas are working on different versions of a project | Bind a project name to a canonical checkout and reject a mismatched clone. Pin the exact source and base when registering each run. Serialize linked checkouts via their shared Git directory and advance result branches atomically. |
| A worker says it finished, but no result can be reproduced | Run operator-authored checks against the staged implementation and detect tracked changes during checking or review. Record the exact tree, exit code, log and artifact hashes. |
| An agent fixes an assertion instead of fixing the product | Support tracked protected paths and optional tracked edit allowlists. Store the check commands in the run plan, outside the worker's checkout. |
| A browser flow works while the interface still looks wrong | Let the project require fresh screenshots and an independent review decision. Feed an actionable `REVISE` result into the next worker attempt. |
| A long tool call times out or the control surface closes | Keep the run independent of the dashboard. A dispatcher can recover enrolled runs at Windows sign-in and preserve the worktree, logs and repair budget after interruption. |
| One agent or session loses the original goal | Give each worker the objective, acceptance requirements, previous accepted commits, current check commands, and latest failure. Final checks verify the completed result against whole-project requirements. |
| A local model approves a broken result | Gate promotion on command checks and any required reviewer verdict. Store local Ollama output as advice. |
| Multiple CLI accounts or model subscriptions are in use | Report the selected provider, profile and model for each role. Offer a live model request before execution and classify recognized authentication, subscription, model access and usage errors during a run. |

## Current limits

- The dispatcher runs one queued job at a time. Prerequisites wait for existing runs to finish, and projects keep their own result branches and pinned baselines. Automatic restart at sign-in requires installing the scheduled task.
- Each attempt has a timeout and each milestone has an attempt limit. Optional run budgets persist across restarts. Token accounting depends on reported telemetry and is not a billing cap. A blocked run stays inspectable and can be explicitly retried without refilling its run budget.
- Recognized provider failures can use an explicitly configured fallback route. Temporary service/usage failures have bounded durable retries. Without an available fallback, authentication failures block with a login recovery message.
- Git links, allowed tracked paths and Job Objects provide ownership and recovery boundaries. They do not sandbox arbitrary user-supplied executables. Use trusted checks, working directories, adapters, and project content.
- Declarative browser gates cover local static pages or existing loopback apps, responsive viewports and interaction assertions. Screenshots and traces preserve failed attempts as well as successes. The project still supplies selectors, application startup, trusted checks and the visual brief; screenshot presence alone is not visual acceptance.
- Git tracks accepted source code. Untracked local state, web deployments, and physical-device or production behavior require explicit project checks and separate operational steps.

## Research upgrade

The [ranked upgrade record](upgrade-plan.md) maps ten features and ten improvements to primary upstream repositories. The [0.3 guide](v0.3-guide.md) documents budgets, browser journeys, fallback, Gemini, feedback, forks, reports and evaluation boundaries. Remaining opportunities include calibrated visual preference scoring, application-server lifecycle recipes, and recorded CLI/model versions for stronger reproducibility.
