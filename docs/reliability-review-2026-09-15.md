# Reliability review: subprocess ownership and first run

## Scope and source research

The selected mechanic is bounded cleanup of workflow subprocess groups when a
timeout or cancellation occurs. GitHub literal searches: `os.killpg` in
`prefecthq/prefect`, and `start_new_session` in `snakemake/snakemake`.
Both repositories were shallow-cloned outside this checkout and their source,
licenses, test presence, and CI inspected. Exa was unavailable.

| Rank | Candidate | Activity and reality | License | Decision |
| --- | --- | --- | --- | --- |
| 1 | Prefect `c3496c2ad603d994de162cd5db954219d3a5ef3c` | Active Sep 15; concrete group termination and descendant regression tests | Apache-2.0 | Reuse the mechanism, no dependency or copied code |
| 2 | Snakemake `91763d644db0a6051c40014fa8ffad340f7d39a0` | Active Sep 11; implemented shell registry, tests and CI | MIT | Avoid immediate-process termination for this boundary |

### Source teardowns and steal-list

- **Steal now — stable group identity.** Prefect
  [`commands.py`](https://github.com/PrefectHQ/prefect/blob/c3496c2ad603d994de162cd5db954219d3a5ef3c/src/integrations/prefect-shell/prefect_shell/commands.py)
  `_signal_process_tree` falls back to the leader PID after it exits because
  `start_new_session=True` made that PID the group identifier. Descendant tests
  live in the adjacent `tests/test_commands.py`. High confidence; adapt in a
  small standard-library helper, roughly 30–50 lines, zero dependencies.
- **Steal now — distinguish the group from its leader.** Prefect
  [`_process_manager.py`](https://github.com/PrefectHQ/prefect/blob/c3496c2ad603d994de162cd5db954219d3a5ef3c/src/prefect/runner/_process_manager.py)
  `_PosixProcessGroupTerminationScope` signals a group for TERM and KILL.
  A leader exiting does not prove its children exited. High confidence; same
  helper, no separate abstraction required.
- **Avoid — direct PID cleanup.** Snakemake
  [`src/snakemake/shell.py`](https://github.com/snakemake/snakemake/blob/91763d644db0a6051c40014fa8ffad340f7d39a0/src/snakemake/shell.py)
  registers active `Popen` instances but its `kill` and `terminate` call the
  immediate process methods. This source does not establish the descendant
  guarantee needed here. No port recommended; not a judgment on other executors.

There is no basis to claim ecosystem consensus from two projects. Neither is
an abandoned stub. Pi Graph already has a group registry for shell commands;
extend that existing mechanism to model calls instead of adding orchestration.

## Official behavior and local findings

Python 3.14.7 is the local runtime. The
[subprocess documentation](https://docs.python.org/3.14/library/subprocess.html)
states that `run(timeout=...)` kills and waits for the child; `communicate`
timeouts require caller cleanup. `start_new_session=True` calls `setsid` before
execution. [`os.killpg`](https://docs.python.org/3.14/library/os.html#os.killpg)
signals a process group. These agree with the installed `subprocess.py`.

Observed before changes:

- `call_pi` used `subprocess.run`, with no owned process group. A timed-out
  tool subprocess could survive and retain stdout/stderr descriptors.
- `run_shell` looked up the leader again during timeout cleanup. If that
  leader had exited, it discarded diagnostics and left the group alive.
- The README required model login before showing a shell-only first success.

Smallest implementation: share process creation, registration, timeout cleanup,
and reaping; retain public timeout/retry contracts. Test real descendants,
including TERM-resistant children and exited leaders. Deliberate new sessions
and remote effects remain outside process-group containment. No live model
quality or hosted-service claim follows from these tests.
