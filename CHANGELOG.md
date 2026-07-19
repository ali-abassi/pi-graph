# Changelog

All notable changes to Pi Workflows are documented here.

## 0.3.0 — 2026-07-19

- Made agent-triggered bulk execution a first-class product path: frozen graph
  and corpus receipts, bounded item concurrency, isolated attempts, detached
  launch, compact status, cancellation, failure ceilings, and drift-safe resume.
- Fixed the previous batch runner dropping corpus content instead of passing it
  through the workflow's immutable `{input}` and `$INPUT` contract.
- Added per-item proof of expected, passed, failed, skipped, and terminal nodes;
  `--require-all` makes “run these exact five steps” mechanically enforceable.
- Added the runnable five-step/1,000-item example and a scale verification path.
- Disabled per-item Git repositories by default in bulk mode to avoid thousands
  of redundant commits; events, artifacts, and ledgers remain complete, with
  `--git-history` available when desired.
- Removed hosted CI automation. Workflows are triggered by agents or operators;
  Pi Workflows itself owns execution and evidence.

## 0.2.0 — 2026-07-19

- Added the optional `piw ui` Studio: canonical graph rendering, exact node
  contracts, immutable input, live run states, evidence, artifacts, and cost
  hotspot inspection with no second workflow engine.
- Added the Pi-symbol-plus-workflows identity, product screenshot, Mermaid graph
  gallery, stronger positioning, and a clearer community-project boundary.
- Exposed ten composable graph capabilities in `piw schema --json` and documented
  the four execution runtimes plus honest next-node boundaries.
- Hardened the local UI with startup validation, localhost-only serving, a run
  token, CSP, bounded request/session state, and the same fail-closed runner.
- Made `piw validate` enforce the versioned JSON Schema before execution, so
  unknown fields, wrong types, and illegal node-field combinations fail closed.
- Fixed relative `--input-file` paths and made the CLI fail fast when a runner
  exits before emitting a terminal event instead of waiting until timeout.
- Restored the latest run automatically in Studio and added its screenshot to
  Pi package gallery metadata.
- Kept clean installs and example certification isolated from nested run/cache
  state, including non-copyable Git fsmonitor sockets from prior evidence runs.

## 0.1.3 — 2026-07-19

- Corrected the public determinism claim: graph transitions are mechanical for
  validated outputs, while live LLM outputs and output-dependent branches can
  vary and must be pinned, gated, and evidenced.

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
  deterministic runner, and Loops/Agent X integration boundary.
