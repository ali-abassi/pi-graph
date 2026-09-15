# Design constraints

**Observed:** `steps.yaml` and the canonical Python runner own execution;
Studio and harness integrations consume the same state and evidence. Local
macOS and Linux are supported. Source: `AGENTS.md` and `SKILL.md`.

**Agent-selected working policy, 2026-09-15:**

- Use one standard-library subprocess lifecycle for shell and Pi calls.
- Start each invocation in its own session; its PID is the stable process-group
  identifier even if the leader exits before its descendants.
- On timeout, TERM the group, allow bounded cleanup, then KILL remaining group
  members and reap the leader. Keep the group registered throughout cleanup.
- Preserve the caller's existing timeout exception and ledger behavior.
- Lead onboarding with a free local run, then explain model-backed work.
- Keep advanced functionality available without adding new commands or layers.
