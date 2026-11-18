# Compare AGTX OneShot, standalone Codex and standalone Claude Code

From the Nightweaver checkout, generate a disposable Git project and its protected acceptance checks. Supply the installed upstream `skills/oneshot/SKILL.md`, the WSL distribution containing AGTX and its authenticated Codex worker, and the Windows Claude CLI profile that should run both Claude routes.

```powershell
python examples/create_comparison.py C:\Experiments\head-to-head --agtx-skill C:\src\agtx\skills\oneshot\SKILL.md --agtx-distro AGTX-Pilot --claude-profile C:\Profiles\claude --claude-model sonnet --seconds 540 --repetitions 2
python -m nightweaver --home C:\Experiments\head-to-head\runtime benchmark create C:\Experiments\head-to-head\suite.json
python -m nightweaver --home C:\Experiments\head-to-head\runtime benchmark run SUITE_ID
python -m nightweaver --home C:\Experiments\head-to-head\runtime benchmark report SUITE_ID --output C:\Experiments\head-to-head\comparison.html
```

The standard contenders are Claude-led AGTX OneShot with Codex workers, standalone Codex, and standalone Claude Code. `--also-codex-coordinator` adds a fourth configuration that runs the upstream skill text through Codex. The AGTX route supplies the installed skill as instructions in a headless session. Its board and workers use the installed AGTX binary. The project directory, AGTX configuration, database and both tmux servers are private to the trial. The board uses automatic trust for its disposable project. The coordinator saves its notes separately. WSL needs Linux Git, Python 3, tmux, AGTX and a working Codex login. The Windows host needs Git, Python, WSL and the selected coordinator CLI. See the [upstream OneShot workflow](https://github.com/fynnfluegge/agtx#oneshot-skill).

All contenders start at the same Git commit, receive the same product requirements, and have the same outer execution limit. AGTX transfers the Git bundle over WSL standard input, launches the board and lets workers commit on Linux. At export the bridge checks the baseline, verifies that every task is marked done, and requires the merged branch to advance. It applies the merged product diff to the Windows evaluation worktree. The ordinary Nightweaver checks enforce the permitted files and run the same assertions for each contender. The bridge reserves `.agtx` and `oneshot-state.md` for runtime metadata. Keep that namespace free when adding benchmark tasks. Generated private logs and the WSL sandbox are retained for diagnosis.

**Acceptance gap** is 100 times (left-only passes minus right-only passes) divided by scored pairs. Positive values favor the left contender. Each pair matches task, repetition and Git baseline. The report shows both the numerator counts in JSON and the number of scored pairs. An account or environment blocker makes that pair inconclusive. An unfinished pair remains pending.

**Median time ratio** is the median of right elapsed seconds divided by left elapsed seconds for paired successes. A value of 2 means the left workflow took half the time on the median matched success. The report shows the number of timed pairs and leaves the ratio empty when there are none. The clock includes orchestration overhead. The count of outer CLI invocations cannot measure how many model calls an AGTX worker fleet used.

This starter measures one library task. To evaluate your usual projects, add representative task plans under `tasks`, run multiple repetitions, and pin the model choices available on your accounts. Preserve blocked trials and their causes. A small pilot can establish whether a particular configuration passed and how long it took. A general ranking requires broader task coverage.
