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
- Each attempt has a timeout and each milestone has an attempt limit. A blocked run stays inspectable and can be explicitly retried. Provider authentication, rate limits, and machine availability still affect progress.
- Recognized authentication failures block the run immediately with a login recovery message. A renewed profile can resume through the existing worktree.
- Git links, allowed tracked paths and Job Objects provide ownership and recovery boundaries. They do not sandbox arbitrary user-supplied executables. Use trusted checks, working directories, adapters, and project content.
- Browser verification is project-specific. Nightweaver stores the resulting artifacts and review evidence. The project supplies the checks and the visual brief.
- Git tracks accepted source code. Untracked local state, web deployments, and physical-device or production behavior require explicit project checks and separate operational steps.

## Next priorities

Add screenshot comparisons and reusable browser flows. Show blocked prerequisites and their underlying failures directly in the dependent run. Track provider availability over time so one successful probe can be distinguished from reliable access.
