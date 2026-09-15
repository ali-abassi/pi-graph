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

## Studio visual system — 2026-09-15

**User-stated:** Improve the overall UI; the orange treatment is disliked. Make
per-action and overall-run evaluations visible.

**Agent-selected, Standard effort:** one materially redesigned workspace with
existing graph, history and inspector interactions. Compare three directions:
(1) graphite instrument panel, (2) light editorial notebook, (3) saturated control
room. Choose (1): long graph/evidence sessions benefit from quiet surfaces and
precise semantic signals. The notebook sacrifices graph density; the saturated
control room repeats the rejected accent-heavy treatment.

**Thesis:** For an agent author and a human inspecting a run, Studio feels like a
quiet local development workspace so the execution path and evaluation gaps are
readable immediately. It prioritizes content and evidence over decorative chrome,
using neutral surfaces, native UI type, restrained blue selection and a graph
paired with explicit evaluation coverage.

### Reference ledger

- https://styles.refero.design/style/90ce5883-bb24-4466-93f7-801cd617b0d1
  and its rendered 1600×1000 preview at
  https://images.refero.design/styles/refero.design/image/eee18c0c-a85f-4a66-83f6-f9f38d284825.jpg
  - Observe: near-black layers, white mixed-case headings, quiet hairline dividers,
    compact navigation; the work object dominates the embedded product view.
  - Explain/evidence: preview and CSS-role export show surface luminance separating
    regions without bright borders; medium sans text carries hierarchy.
  - Use: restrained surface steps and deliberate type roles.
  - Avoid: promotional spacing and tiny metadata; no borrowed lime accent.
  - Depart: our main object is a dependency graph, not a task document; evaluation
    coverage is a first-class evidence table with missing checks explicitly shown.

### Ten levers

1. Register: compact desktop product workspace; comfortable evidence reading.
2. Composition: history → selected workflow/graph → inspector; evaluations follow
   the graph, while low-level provenance is disclosed on demand. At ≤900px, history
   is a drawer and the inspector stacks; 390px is the minimum proof viewport.
3. Type candidates: platform system sans + system mono (chosen for local-native
   belonging and readable compact controls); IBM Plex Sans (good technical voice,
   rejected extra font delivery); serif headings + mono (rejected editorial density).
   Native installed fonts only, no redistributed fonts. Body/UI 13–14px regular,
   headings 20–24px semibold, metadata/code 11–12px mono. Mixed case labels.
4. Palette: graphite canvas #111318, surfaces #181b21/#20242c, primary #edf0f5,
   muted #a5adbb, dividers #303641. Primary action is light neutral; blue #9bbcff
   marks selection/focus. Green #86d9ac, red #ff9494 and amber #e4c17a mean outcomes.
5. Rhythm: 4/8/12/16/24px; controls align to 40px desktop and ≥44px touch targets.
6. Shape: 6px controls, 10px panels; status text is not a sea of pills.
7. Elevation: surface contrast and borders, no glows or glass overlays.
8. Imagery: the real graph is the only visual; existing mark and functional icons.
9. Components: one clear Run action, restrained secondary controls, visible focus,
   readable tables, native disclosure for provenance/setup instructions.
10. Invariants: no orange; no all-caps microtype as primary navigation; mono only for
    code/IDs/data; missing evaluations never green; node success never means an
    unconfigured semantic review passed. No fake scores or decorative charts.

### Proof and evaluation rules

Inspect real completed, failed and empty runs at 1280×900 and 390×844; keyboard
selection/tabs, Fit/zoom, launch feedback, and content overflow. Evaluation tests
cover unavailable evidence, passing/failed gates, schema failures, cached judges,
malformed judge JSON, score thresholds and run-level QA. Active-agent critical
review is used; no delegated or independent-human review is claimed.
