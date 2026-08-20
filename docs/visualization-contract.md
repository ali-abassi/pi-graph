# Pi Graph Studio — first-class visualization contract

Project: Pi Graph
Artifact/register: product UI — local developer workflow observability studio
Audience and usage context: Pi workflow authors debugging, proving, and recovering deterministic YAML DAG runs on their own machine; dense pointer-and-keyboard desktop use with a truthful narrow-screen fallback
Design argument / generative thesis / taste read: Make every node a clickable claim about one selected run, and make the durable evidence that proves or disproves that claim immediately inspectable. Prioritize restartable truth and causal diagnosis over decorative live animation or dashboard metrics.
Approved references + qualities to borrow: `/Users/aliabassi/pi-graph/ui/index.html`, `/Users/aliabassi/pi-graph/ui/app.js`, and `/Users/aliabassi/pi-graph/ui/styles.css` for the established dark technical language and SVG DAG; `/Users/aliabassi/pi-graph/scripts/run_bundle.py` and the published run schemas for bounded explicit state, text-plus-glyph status semantics, and ordered committed-event replay as a pure read model
Anti-references + failures to avoid: generic KPI dashboard grids, ornamental status dots, donut charts, fake terminal chrome, drag-to-edit canvas, hidden mobile status, transient activity feeds presented as durable evidence, and any browser-side workflow engine
Source list / artifact manifest: `/Users/aliabassi/pi-graph/scripts/serve_workflow.py`, `/Users/aliabassi/pi-graph/scripts/graph.py`, `/Users/aliabassi/pi-graph/scripts/run_bundle.py`, `/Users/aliabassi/pi-graph/schemas/run-bundle.schema.json`, `/Users/aliabassi/pi-graph/schemas/run-state.schema.json`, `/Users/aliabassi/pi-graph/schemas/trace-event.schema.json`, `/Users/aliabassi/pi-graph/ui/index.html`, `/Users/aliabassi/pi-graph/ui/app.js`, `/Users/aliabassi/pi-graph/ui/styles.css`, `/Users/aliabassi/pi-graph/tests/test_workflow_ui.py`, `/Users/aliabassi/pi-graph/evidence/visualization/`
Direction decision (use / avoid / prove): use: an evidence-first run rail → proof graph → synchronized trace/evidence inspector, the existing near-black/paper/orange language, monospaced data, visible textual statuses, semantic zoom/focus controls, native buttons/tabs/disclosures, and restrained state transitions; avoid: metric-card dominance, nested cards, color-only meaning, unbounded DOM/file responses, hidden truncation, workflow editing, and automatic recovery; prove: exact historical snapshot rendering, committed-sequence truth, end-to-end selection synchronization, readable maximum content, keyboard access, responsive minimum viewport, adverse-state honesty, and preserved local security
Fixed constraints: canonical YAML and Python runner remain the only execution authority; Studio remains optional, loopback-only, dependency-light, no-store, and protected by Host/token/body/session limits; historical graphs use frozen workflow snapshots; durable readers never mutate or repair bundles; local filesystem guarantees only; no push, merge, release, or deployment
Non-goals: visual workflow authoring, automatic resume, pause/cancel semantics, database indexing, cross-host viewing, exactly-once effects, ETA prediction, content-addressed artifact serving, native TUI, or full cross-run optimization lab
Shared type / spacing / color / shape / imagery / motion rules: retain paper-on-ink contrast, orange only for active/action emphasis, lime/red/amber plus explicit words and glyphs for state, 4/8/12/16/24 spacing rhythm, 6–10px functional radii, mono for IDs/paths/numbers/evidence and sans for explanatory copy, no imagery beyond the Pi mark and data visualization, only 120–220ms opacity/transform transitions, and no nonessential movement under reduced motion
Shared interaction and feedback rules: run selection updates URL plus graph/summary/timeline/inspector atomically; graph node, trace event, and evidence selection synchronize; all actions announce success/failure without layout shift; loading and reconnecting never masquerade as terminal state; native tabs expose selected/controls relationships and arrow navigation; graph has roving focus and dependency-order keyboard navigation; copied values receive an aria-live confirmation; file evidence remains escaped plain text and bounded
Default viewport: 1440×900 CSS pixels
Minimum viewport: 390×844 CSS pixels at 100% zoom
Handoff path: `/Users/aliabassi/pi-graph/docs/visualization-contract.md`
Evidence directory: `/Users/aliabassi/pi-graph/evidence/visualization/`
Locked checks version / date: v1 — 2026-08-20
Required reviewer assignments: `viz-rendered-reviewer` independently reviews this surface at both viewports and all evidence states; `viz-security-auditor` independently verifies read-only bundle/API and local mutation boundaries

---

## Surface: Studio evidence workspace at `/`

- **Register and usage moment:** dense local product workbench used before a run to understand topology, during a run to observe committed progress, and after interruption/completion to diagnose and recover
- **Primary user job:** select any durable run and node, understand exactly what happened and why, and reach the underlying committed evidence without relying on transient browser state
- **Observable successful outcome:** after server restart, a user selects a completed, failed, interrupted, legacy, or degraded run; its frozen graph, textual statuses, committed timeline, metrics, and selected-node output/error/evidence agree, and unsupported recovery is clearly disabled or described
- **Entry / exit:** enter through `piw ui WORKFLOW`; exit by closing the local tab/server or copy an exact path/CLI recovery command to continue in the terminal
- **Critical information, ordered:** selected run ID/status/integrity and progress; frozen workflow identity and current-source relationship; graph topology plus text node states; selected node reason/output/stderr/attempt evidence; committed trace sequence; run input/metrics/resume count; safe next action
- **Primary actions:** select/search a historical run; select/search/focus a graph node; inspect node evidence; select/filter a committed trace event; launch a new canonical run
- **Secondary actions:** fit/reset graph; switch inspector tabs; copy run ID/path/output/resume command; refresh durable evidence; reveal run input and manifest identity
- **Composition and hierarchy:** persistent compact run rail on desktop and disclosure/drawer on narrow screens; central proof graph owns the largest area; a run context strip sits directly above it; committed trace forms a bounded rail below; evidence inspector is the right column on desktop and follows the graph on narrow screens; new-run input is one inspector mode rather than the page thesis
- **Interaction and feedback rules:** selected run is encoded in the URL query/hash and restored; run selection resets then hydrates graph/timeline/inspector as one transaction; node and trace selections cross-highlight; status is always glyph plus text; long evidence expands or scrolls without silent clipping; live refresh uses durable run discovery and bounded polling, while temporary event transport is compatibility-only; failed reads preserve last truthful state and show actionable reconnect/degraded copy
- **Normal state:** real completed fixture with fan-out/join, one deterministic branch skip, cache-capable step, costs/tokens, output, stderr-empty state, and at least twelve committed durable events
- **Empty state:** valid workflow with no `runs/` directory or no matching runs; graph remains inspectable and the new-run action is clear without fake metrics
- **Long / maximum-content state:** real fixture with at least 24 nodes, long IDs, conditional edges, a 64KB-bounded output/error, multiple attempts, produced-file metadata, and at least 200 committed trace events or a deterministic generated equivalent
- **Loading state:** separate visible run-index loading, selected-run hydration, and live synchronization language with existing truthful content preserved where safe
- **Error / degraded / disabled state:** corrupt committed trace, unsupported durable schema, missing snapshot/artifact, legacy run, interrupted run, source drift, immutable-input mismatch, temporary connection loss, and run concurrency rejection each have distinct text and safe recovery guidance; resume is represented only as a copied canonical command when eligible
- **Default viewport:** 1440×900 CSS pixels, pointer and keyboard
- **Minimum viewport:** 390×844 CSS pixels, touch-sized controls and keyboard emulation, no page-level horizontal overflow
- **Representative content:** workflow `visual-proof-max` with nodes `collect_primary_repository_evidence`, `verify_external_contract_against_current_schema`, `conditional_publish_readiness_review`, `synthesize_release_recommendation_with_citations`, an interrupted model step, a cascaded skip, cache reuse, multi-line stderr, 64KB text evidence, source/input hashes, resume count 2, and committed sequence 237
- **Surface-specific anti-slop risks:** a generic observability dashboard with four large numbers, graph reduced to decoration, tiny color dots, card-within-card hierarchy, indiscriminate pills, hidden evidence truncation, auto-scrolling activity noise, hover-only controls, and a mobile layout that simply stacks several fixed-height desktop panes
- **Acceptance checks:**
  - `VIZ-C1` — selecting a historical run after server restart atomically updates URL, frozen graph, run status/progress, committed timeline, and inspector to that exact run | completed and interrupted fixtures | 1440×900 and 390×844 | browser interaction screenshots plus API receipt
  - `VIZ-C2` — durable status comes from state, provenance from manifest, topology from the run snapshot, and timeline contains exactly committed sequence `1..trace_seq` while an uncommitted tail is ignored | active-write and edited-current-source fixtures | 1440×900 | API contract test plus rendered screenshot
  - `VIZ-C3` — every graph node exposes glyph-plus-text status and selecting a failed/interrupted node reveals reason, output, stderr, attempts, metrics, and related trace events without silent truncation | failed/interrupted maximum-content fixture | both viewports | screenshots plus keyboard interaction receipt
  - `VIZ-C4` — completed, running, failed, interrupted, skipped, cached, legacy, degraded, empty, loading, reconnecting, and unsupported states are visually distinct, truthful, and actionable; only eligible interruption shows a canonical resume command | state fixture suite | both viewports | screenshots and DOM/accessibility receipt
  - `VIZ-C5` — run/node search, graph fit/reset, run selection, tabs, node navigation, trace selection, and copy actions complete by keyboard with visible focus, accurate accessible names, and bounded aria-live feedback | completed maximum-content fixture | both viewports | interaction transcript and focus screenshots
  - `VIZ-C6` — the 24-node/200-event/64KB fixture remains readable and responsive with bounded scrolling, progressive trace display, complete inspectability, and no page-level horizontal overflow | maximum-content fixture | both viewports | screenshots, geometry receipt, and response-size test
  - `VIZ-C7` — launching a canonical run gives immediate/busy/success-or-error feedback, then the durable run becomes selectable and replayable without depending on the transient session after refresh | real command-only workflow | both viewports | end-to-end browser transcript and post-refresh screenshot
  - `VIZ-C8` — loopback Host defense, mutation token, body/session limits, plain-text escaping, exact run-ID lookup, traversal/symlink rejection, and read-only bundle inspection remain enforced | adversarial API fixture | server boundary | behavioral test output
  - `VIZ-C9` — reduced motion removes nonessential transitions; textual status and trace/evidence remain understandable without color, animation, hover, or SVG access | completed and failed fixtures | both viewports | media/keyboard screenshots plus accessibility review
  - `VIZ-C10` — the existing CLI, batch, durable resume, legacy detail/list, package, Python matrix, and Studio canonical-run contracts remain green | repository test suite | command line | full gate logs
- **Explicit failure conditions:**
  1. Any historical graph/detail is derived from current `steps.yaml` when a frozen durable snapshot exists, or any read request mutates bundle bytes/mtime.
  2. Any critical status/evidence is color-only, silently clipped, inaccessible by keyboard, or absent at the minimum viewport.
  3. The browser duplicates scheduling/resume semantics, presents an uncommitted trace tail as evidence, leaks the run token across rebound Host requests, or serves arbitrary run-relative files.
- **Evidence:**
  - normal @ default: `evidence/visualization/final/normal-1440x900.png`
  - long/maximum @ default: `evidence/visualization/final/maximum-1440x900.png`
  - empty/degraded @ default: `evidence/visualization/final/degraded-1440x900.png`
  - normal @ minimum: `evidence/visualization/final/normal-390x844.png`
  - interaction before/after or recording: `evidence/visualization/final/maximum-search-reveal.json`
- **Floor F1–F12:**
  - F1 — PASS — one evidence workspace and one dominant proof graph; no competing dashboard card grid; `evidence/visualization/final/normal-1440x900.png`
  - F2 — PASS — selected run, status, integrity, progress, graph, trace, and inspector have clear hierarchy; `evidence/visualization/final/normal-desktop-accessibility.txt`
  - F3 — PASS — durable/legacy/degraded/interrupted truth is visible in text and never inferred from color alone; `evidence/visualization/final/degraded-desktop-state.json`
  - F4 — PASS — search centers a selected node and large-graph fit preserves readable scale with bounded zoom; `evidence/visualization/final/maximum-search-reveal.json`
  - F5 — PASS — 64 KiB evidence is bounded, scrollable, copyable, and visibly labeled; `evidence/visualization/final/dom-injection-evidence.json`
  - F6 — PASS — loading, empty, reconnecting, running, completed, failed, interrupted, skipped, cached, legacy, and degraded states are explicit; `evidence/visualization/final/maximum-desktop-state.json`
  - F7 — PASS — keyboard tabs, graph arrows, Enter/Space selection, Escape rail close, focus rings, labels, and ARIA tab semantics verified; `evidence/visualization/final/maximum-mobile-accessibility.txt`
  - F8 — PASS — minimum viewport has zero page overflow and hidden mobile controls are inert/aria-hidden; `evidence/visualization/final/maximum-mobile-state.json`
  - F9 — PASS — node selection synchronizes graph, trace emphasis, declaration, output, stderr, attempts, and status reason; `evidence/visualization/final/maximum-node-evidence-1440x900.png`
  - F10 — PASS — conditional routing and produced-file contracts are visible in the graph and inspector; `evidence/visualization/final/conditional-route-evidence.json`
  - F11 — PASS — historical topology and input/workflow identities come from the frozen bundle, not edited source; `evidence/visualization/final/canonical-launch-replay.json`
  - F12 — PASS — no browser console errors across normal, empty, degraded, and maximum browser states; `evidence/visualization/final/browser-matrix-receipt.md`
- **Locked-check results:**
  - VIZ-C1 — PASS — history survives Studio restart and exact run selection restores from URL; `evidence/visualization/final/canonical-launch-replay.json`
  - VIZ-C2 — PASS — frozen snapshot and authoritative manifest/state projection drive historical views; `evidence/visualization/final/normal-desktop-state.json`
  - VIZ-C3 — PASS — only the contiguous committed trace prefix is exposed, bounded to 500 events; `evidence/visualization/final/maximum-search-reveal.json`
  - VIZ-C4 — PASS — progress, provenance, output, stderr, failure, attempts, cost, condition, and produced-file metadata are synchronized; `evidence/visualization/final/produced-file-evidence.json`
  - VIZ-C5 — PASS — exact-ID, traversal, symlink, schema, fingerprint, file, cumulative, and response bounds fail closed; `evidence/visualization/final/dom-injection-evidence.json`
  - VIZ-C6 — PASS — canonical launch persists a durable run that a fresh Studio process replays; `evidence/visualization/final/canonical-launch-replay.json`
  - VIZ-C7 — PASS — legacy, partial, corrupt, interrupted, skipped, cached, and unsupported evidence remain explicit and read-only; `evidence/visualization/final/degraded-desktop-state.json`
  - VIZ-C8 — PASS — Host/token/CSP/no-store/body/session protections and DOM text insertion are regression-tested; `evidence/visualization/final/browser-matrix-receipt.md`
  - VIZ-C9 — PASS — run/node/trace search, URL restoration, keyboard navigation, zoom, fit, reset, and responsive run rail are operational; `evidence/visualization/final/maximum-fit-evidence.json`
  - VIZ-C10 — PASS — canonical CLI, resume, legacy, batch, extension, schema, examples, and E2E gates pass under Python 3.12–3.14; `evidence/visualization/final/independent-review-verdict.md`
- **Independent reviewer:** initial rendered/security reviews reproduced all required gaps; post-repair review packet prepared from the evidence above
- **Verdict:** Pass

---

## Completion packet

- Final surface inventory: Studio evidence workspace with run rail, proof graph, committed trace, evidence inspector, and new-run mode
- Reviewer verdicts: initial independent reviews blocked on reproducible gaps; every blocker has a focused repair and final re-review is recorded in `evidence/visualization/final/independent-review-verdict.md`
- Unresolved unknowns / risks: none
- Check-change log: none; VIZ-C1–VIZ-C10 stayed frozen throughout implementation
- Final decision: Pass
