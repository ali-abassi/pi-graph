# Pi Graph robustness and visualization progress

Updated: 2026-08-21

## Durable workflow foundation

- Fixed retry output isolation: failed-attempt bytes cannot leak into a later successful attempt; rejected evidence is archived per attempt.
- Made run input immutable with exclusive creation, `0600`, fsync, and mismatch refusal.
- Made skipped-run verification understand recorded conditional skips.
- Validated top-level conditional `from:` dependencies consistently in the runner and graph model.
- Made ledger replacement atomic and failure-preserving.
- Added schema-versioned durable local bundles: immutable workflow/input identity, atomic manifest/state projections, contiguous committed trace, one-writer fencing, bounded tail repair, explicit unfinished-boundary resume, audited workflow drift, and unforceable input drift.
- Added CLI and Pi tool resume support while preserving legacy list/detail/`--from` behavior.
- Independent fault review found and closed initialization, state-projection, manifest-projection, trace-tail, schema-validation, and cascaded-skip crash windows with behavioral regressions.
- Pre-visualization completion gate passed under Python 3.12, 3.13, and 3.14 with 75 Python tests, 3 extension tests, and all 9 repository checks.

## First-class visualization mission

- Audited 39/39 Studio/runtime/package surfaces. The SVG DAG and canonical launch path were strong; missing product contracts were durable history, frozen historical topology, committed trace/provenance, integrity/recovery states, synchronized node evidence, restart replay, maximum-content bounds, and truthful narrow-screen access.
- Locked and pre-validated `docs/visualization-contract.md` before presentation edits. Thesis: every graph node is a claim about one selected run, synchronized to the durable evidence that proves it.
- Built one evidence-first Studio workspace: exact run history and URL restoration; frozen proof graph; manifest/state progress and provenance; committed trace; per-node output/stderr/attempts/failure reasons; run/node/trace search; zoom/fit/reset; keyboard tabs and graph navigation; copy-only recovery command; canonical launch replay.
- Added explicit normal, empty, loading, reconnecting, running, completed, failed, interrupted, skipped, cached, legacy, degraded, and unsupported states with visible text—not color alone.
- Added bounded read-only APIs: at most 200 exact child runs, 500 committed events, 2 MiB response/file bounds, 8 MiB cumulative evidence guard, schema/fingerprint verification, exact-ID and symlink rejection, and no bundle repair/locking/mutation.
- Preserved loopback Host rejection, mutation token, CSP/no-store/nosniff, body cap, active/session limits, canonical runner semantics, and machine JSON contracts.
- Browser evidence exists under `evidence/visualization/` for baseline, normal, empty, degraded, interrupted maximum-content, canonical launch/restart, desktop `1440×900`, and minimum `390×844` states.
- Independent rendered/security review found concrete gaps in large-graph navigation, mobile hidden-focus behavior, maximum fixtures, schema/fingerprint enforcement, safe file reads, request/session bounds, async selection races, and fingerprint-to-parse file swaps. Every reproduced blocker now has a focused regression and verified repair.

## Final gate

- Python 3.12, 3.13, and 3.14 each pass 80 Python tests, 3 extension tests, and all 9 repository checks.
- Browser matrix passes normal, empty, degraded, and 200-node maximum states at `1440×900` and `390×844`, with zero page overflow or console errors. Boundary-node search centers exactly (`delta: 0`).
- Final interface-contract validator passes for the Studio surface.
- `npm pack --dry-run` includes the Studio/server/docs and excludes mission, fixture, and browser-evidence material.
- External project references are absent from repository content; the old comparison document was removed.
- Final independent read-only audit: PASS. Exact fingerprinted workflow/input bytes are consumed without reopening attacker-controlled evidence paths.
- `git diff --check`: PASS.
- No push, merge, release, or deployment has occurred.
