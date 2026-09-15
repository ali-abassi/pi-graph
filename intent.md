# Current outcome

**Agent-selected working policy, 2026-09-15:** Harden timeout cleanup for shell
and model processes, and make the first successful run require no model account.

## Acceptance criteria

- A timed-out shell or model attempt terminates descendants in its owned POSIX
  process group even after the leader exits, including children ignoring TERM.
- Timeout cleanup preserves diagnostics and existing failure/retry semantics.
- Cancellation reaches model processes through the existing child registry.
- Successful subprocess stdout, stderr, and exit codes remain intact.
- The README starts with a runnable shell-only example and clearly identifies
  model setup and external scheduling as optional boundaries.
- Focused real-subprocess regressions and the repository verification script pass.

**Observed limits:** POSIX process groups do not contain deliberately detached
sessions or remote effects. No paid model evaluation, hosted deployment, or
new UI is part of this candidate.
