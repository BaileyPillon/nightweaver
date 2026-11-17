# Design rationale

Rookline addresses recurring failure modes in long-running coding work. Verify the exact project and version, pass the next agent the goal and the real failure, and make every advancement traceable to its evidence.

| Failure mode | What Rookline does |
| --- | --- |
| Several sessions or replicas are working on different versions of a project | Pin the source repository and base commit when registering a run. Give each milestone an isolated checkout. Advance the result branch with a compare-and-swap Git update. |
| A worker says it finished, but no result can be reproduced | Run operator-authored checks against the staged implementation and detect tracked changes during checking or review. Record the exact tree, exit code, log and artifact hashes. |
| An agent fixes an assertion instead of fixing the product | Support tracked protected paths and optional tracked edit allowlists. Store the check commands in the run plan, outside the worker's checkout. |
| A browser flow works while the interface still looks wrong | Let the project require fresh screenshots and an independent review decision. Feed an actionable `REVISE` result into the next worker attempt. |
| A long tool call times out or the control surface closes | Keep the run independent of the dashboard. Store transitions before launching work, keep raw logs, and recover the same worktree after interruption. |
| One agent or session loses the original goal | Put the project objective, the current milestone, the check commands, and the most recent failure into each worker handoff. Use the prior provider session within a milestone when available. |
| A local model approves a broken result | Gate promotion on command checks and any required reviewer verdict. Store local Ollama output as advice. |
| Multiple CLI accounts or model subscriptions are in use | Allow per-role provider and profile selection. Authentication remains with the installed CLI. |

## Current limits

- Version 0.1 executes ordered milestones sequentially in a run. It supports multiple independent run records and checks branch movement before promoting. There is no cross-project dependency graph or automatic scheduler after Windows sign-in.
- Each attempt has a timeout and each milestone has an attempt limit. A blocked run stays inspectable and can be explicitly retried. Provider authentication, rate limits, and machine availability still affect progress.
- Recognized authentication failures block the run immediately with a login recovery message. A renewed profile can resume through the existing worktree.
- Git links, allowed tracked paths and Job Objects provide ownership and recovery boundaries. They do not sandbox arbitrary user-supplied executables. Use trusted checks, working directories, adapters, and project content.
- Browser verification is project-specific. Rookline stores the resulting artifacts and review evidence. The project supplies the checks and the visual brief.
- Git tracks accepted source code. Untracked local state, web deployments, and physical-device or production behavior require explicit project checks and separate operational steps.

## Next priorities

Add a durable multi-project queue with restart-at-sign-in, lane ownership and explicit dependency edges. Improve provider failure classification so authentication errors, model availability, check failures, and reviewer revisions produce different recovery paths. Add reusable browser acceptance templates with richer screenshot comparisons and make it easier to inspect a milestone's rejection and promotion evidence side by side.
