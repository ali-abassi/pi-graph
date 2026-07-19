# Changelog

All notable changes to Pi Workflows are documented here.

## 0.1.2 — 2026-07-19

- Aligned package, skill, extension, JSON transport, and security behavior with
  the official Pi 0.80.10 documentation and pinned source.
- Isolated model nodes from project trust and startup network activity, pinned
  the observed provider/model, and required a valid settled JSON event stream.
- Bounded native extension output with Pi's truncation helpers while preserving
  full output in a temporary evidence file.
- Made `piw doctor` verify the active Pi skill registry and documented the
  supported full-product installer boundary.
- Removed Agent X's obsolete embedded workflow skill route in favor of the
  independently installed `pi-workflows` package.

## 0.1.1 — 2026-07-19

- Added 12 graduated, reusable workflows covering commands, sequential and
  parallel DAGs, retries, Luna completions, typed output, deterministic routing,
  tools, agents, judges, QA, caching, logs, and optimization analysis.
- Added a free example-contract check and a Luna-medium live certification
  harness with per-run evidence and deterministic hotspot reporting.
- Counted final QA usage in the canonical ledger and machine run summary.
- Removed stale private evaluation fixtures and repaired obsolete skill paths
  and public metadata.
- Added contributor guidance and published live certification evidence.

## 0.1.0 — 2026-07-19

- First public release of the standalone Pi Workflows product.
- Added the versioned YAML contract, JSON Schema, native Pi tool, installer,
  deterministic runner, Loops/Agent X integration boundary, and public CI.
