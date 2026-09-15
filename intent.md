# Current outcome

**User-stated, 2026-09-15:** Make opening a graph node and reading its prompts and contents easier for a UI user.

**Agent-selected scope:** A native, read-only node detail dialog opened by graph activation or an explicit Open node button. Preserve the existing inspector and runner.

## Acceptance

- A visible graph hint and Open node action make inspection discoverable.
- Clicking a node or pressing Enter/Space opens its prompt or command at a readable width.
- Distinguish the frozen template from the bounded recorded model prompt and output; absent evidence is explicit.
- Escape/Close returns focus to the graph; keyboard and 390px mobile work.
- Existing node selection, evaluations and run history remain usable; full repository gate passes.

**Limits:** No prompt editing, new execution, paid calls, or additional backend/API behavior.
