# Live stability checks

A failed browser journey can be a code defect, a timing problem, or a change in the surrounding environment. Nightweaver 0.8 can repeat a check while the candidate is still available for repair. The result records what happened on the same tracked source. Determining the underlying cause still requires inspecting the evidence.

Add `stability_runs` to a repeatable command or browser gate in a plan or OneShot template. The default is one invocation. Values from two to five enable sampling. A gate configured for two samples can invoke its check at most four times per verification batch. The existing run budget also applies.

```json
{
  "name": "Gallery journey",
  "stability_runs": 2,
  "argv": ["python", "checks/gallery.py"],
  "timeout": 90
}
```

| First round | Confirmation round | Controller action |
| --- | --- | --- |
| Every sample passes | — | Advance to the next check or review. |
| Every sample fails | — | Send the failure to the worker under the existing repair budget. |
| Mixed results | Every sample passes | Advance and count a recovered check. Keep the original failure. |
| Mixed results | Every sample fails | Send the confirmed failure to the worker. |
| Mixed results | Mixed results | Hold this candidate at verification. Record `verification_inconsistent` and both rounds of logs. Matched benchmark scoring excludes this outcome. |

Each live sample starts a fresh check process on the same candidate worktree. The controller checks the Git tree between samples. Ignored files, dependencies and external services can retain state, so choose checks with repeatable setup. Pin external verifier scripts using `pinned_inputs` when their content determines acceptance. Missing pins, unusable artifacts and source changes take their own error paths.

## Restart and recovery

The event log records a batch ID, attempt, sample number, contract digest and candidate tree. Each invocation has a separate evidence directory. After an interruption, the controller checks the saved log and artifact hashes before using a completed sample. An invocation interrupted before its receipt was recorded runs again. A normal restart preserves earlier samples, including failures.

If two rounds disagree, inspect the check with `nightweaver diagnose-run RUN`. Once the cause has been addressed, `nightweaver resume RUN --retry` starts fresh verification on the current candidate. It can do this even when the last allowed worker call has already been spent.

## Reading the evidence

- `nightweaver metrics RUN` reports confirmation rounds and recoveries. `gate-stats RUN` distinguishes unusable receipts with the `unscored` count.
- `nightweaver trace RUN --output trace.jsonl` includes sample numbers and verification batch IDs.
- `nightweaver replay-checks RUN` uses each accepted gate’s configured sample count. `--repeat N` overrides that count for a diagnostic replay. Replay uses independent checkouts and reports the observed distribution directly. It does not make a live promotion decision.

The accepted check receipt contains the passing round. When a confirmation recovers, its first receipt also links the complete earlier observations under `prior_mixed_round`. For repository-relative verifier pins, preflight now hashes the bytes Git would check out at the baseline, including its Windows line-ending rules. The live check hashes the actual candidate file. [Git checkout filters](https://git-scm.com/docs/git-cat-file#Documentation/git-cat-file.txt---filters)

## Research behind the policy

Playwright distinguishes a first-pass success from a test that passes only on retry. pytest-rerunfailures also supports surfacing a retry-dependent result to CI. Practitioners describe intermittent timeouts in CI even when local runs are stable. These informed the separate recovery count and the decision to preserve the first failure. [Playwright retries](https://playwright.dev/docs/test-retries) · [pytest-rerunfailures](https://github.com/pytest-dev/pytest-rerunfailures) · [Practitioner discussion](https://www.reddit.com/r/Playwright/comments/1q5nypo/playwright_tests_flaky_in_ci_but_stable_locally/)

The FlakeFlagger study found that repeated executions could still miss known flaky tests. A short confirmation round supplies evidence for a bounded operating decision. Its passing count is not a probability of future success. [ICSE study](https://2021.icse-conferences.org/details/icse-2021-papers/115/FlakeFlagger-Predicting-Flakiness-Without-Rerunning-Tests)
