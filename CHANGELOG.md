# Changelog

All notable changes to pi graph are documented here.

## Unreleased

### Agent ergonomics

- Full model/tool/agent/judge/QA and optimization-promotion journeys now run token-free in CI through one deterministic fake-Pi protocol harness; malformed, unsettled, provider-error, extension-error, retry-error, and blank streams are classified and evidenced.
- `piw optimize mechanisms`, `checkout`, `diff`, and `submit` remove manual incumbent copying and mechanism/parent reconstruction. Checkout is no-overwrite and byte exact; diff is free; submit infers the one changed mechanism; stale parents fail closed.
- Optimize responses now include executable `next_actions` argv arrays with current parent IDs and required placeholders while retaining compact `next` IDs.
- `piw eval` reports paired item regressions/improvements, Wilson pass intervals, median/p95 cost and latency, small-corpus warnings, and an explicit recommendation that may remain inconclusive.
- `path`, every `show` mode, `schedule`, and `automation` now return bounded parseable objects under `--json`; external scheduler text is wrapped rather than passed through as fake JSON.
- Doctor verifies the scheduler through the same `loops` CLI adapter used by `schedule`, rather than an unrelated daemon endpoint.
- `piw optimize scaffold` generates a schema-valid starter optimization contract from a workflow: one mutable mechanism per model step, a frozen default evaluator/parser beside the contract, digest-only holdout metadata, and conservative budgets. `piw optimize init` accepts it unchanged.
- Every command now emits a structured `{schema: pi-graph.error.v1}` error document on stdout under `--json`; usage errors included. Typo'd subcommands suggest the nearest valid name.
- New `piw models` lists valid model ids from `pi --list-models`; `--check <id>` pre-flights an id (with a near-match suggestion) before any paid run.
- `piw eval --json` exits non-zero when items fail, aligning the receipt with shell semantics; the receipt names its artifacts via `results_path`.
- `detail`, `compare`, `set`, and `run --node` failures now list valid run/step ids or point at `piw runs`.
- `piw batch` and `piw eval` take `--input-name` for the per-item staged filename; `--input-file` still works there but warns, since it never meant a path.
- Help text documents workflow discovery scope (git root, examples/templates, roots registry, `PI_GRAPH_ROOTS`) and that any workflow positional accepts a directory or steps.yaml path.

### Deterministic workflow optimization

- `piw optimize` now freezes an experiment contract, evaluates the untouched baseline through the canonical batch path, accepts one-mechanism candidates, and makes uncertainty-aware keep/revert decisions with byte-perfect rollback.
- Experiments use one-writer locks, immutable source/runtime fingerprints, a fsynced hash-chained ledger, finite candidate/time/token/cost/failure/plateau stops, committed-prefix recovery, and bounded machine-readable responses.
- Recovery blocks authoring, stop, and promotion while a candidate is pending; it completes committed keep decisions without contradictory rollback and repairs terminal receipts only from intact committed evidence.
- Private holdout bytes remain unstaged until terminal promotion, can be reserved at most once, and produce a signed local receipt without committing, pushing, merging, deploying, or writing production state.
- `piw version --json` and doctor expose source/install fingerprints; installs receive an atomic integrity manifest. `piw eval --json` now emits one JSON receipt.

### Durable local runs

- New runs freeze exact workflow/input identity and atomically project a
  schema-versioned manifest/state beside a contiguous, repairable trace.
- `piw resume WORKFLOW RUN` and the Pi tool resume only the committed unfinished
  boundary under a one-writer lock; workflow drift is explicit and audited,
  while input drift is never forceable.
- Legacy run artifacts/list/detail/`--from` behavior remains compatible.
- Retry output isolation, conditional dependency validation, skipped-run
  verification, immutable input, and atomic ledger regressions are fixed.

### First-class Studio visualization

- Studio discovers durable history after restart and renders the selected run's
  frozen graph, authoritative progress/provenance, committed trace, and bounded
  node output/stderr/attempt evidence as one synchronized workspace.
- Run/node/trace search, graph fit/reset, URL-restored selection, keyboard tabs
  and node navigation, visible text states, responsive mobile layout, and a
  copy-only recovery command make evidence usable rather than decorative.
- Legacy, incomplete, and corrupt bundles remain visibly read-only; exact-ID,
  symlink, response-size, Host, token, body, and session defenses fail closed.

## 0.1.0 — 2026-07-19

First public release.

### Workflow runner

- `steps.yaml` as the single execution contract: nodes, `needs:` dependencies,
  shell `cmd:` steps, model `prompt:` steps, `when:` routing, and `gate:`
  assertions that must pass before a node is considered done.
- Per-node QA: an independent `judge:` with a score threshold and a bounded
  improve/retest loop.
- Retry policies with eligibility classes and recorded backoff.
- Content-addressed caching that skips the model call and the judge while still
  re-running the gate.
- Per-node evidence for every run: input, output, model, attempts, gate result,
  QA trail, tokens, cost, and latency.
- Model pins are verified against what the provider actually served; a drifted
  model fails the step instead of silently returning another model's answer.

### Scale

- `piw batch` runs one frozen graph across a corpus with isolated per-item
  workspaces, ordered outputs, and per-item receipts.
- The batch manifest pins SHA-256 digests of both workflow and corpus;
  `--resume` refuses to continue if either changed.
- Fail-closed dispatch ceilings (`--max-tokens`, `--max-cost`) computed over
  usage recorded in every attempt, including failed ones.
- `batch-cancel` terminates active item process groups instead of orphaning
  them.

### Inspection and evaluation

- `piw detail` for a whole run or one node, `piw compare` for two runs,
  `piw stats` for aggregates, and `piw eval` to compare models over a corpus
  while judges stay fixed.
- `piw schema --json` exposes the complete authoring contract.
- Optional local Studio (`piw ui`) as a graph runner and flight recorder over
  the same `steps.yaml`. The Studio validates the `Host` header so a rebound
  DNS name cannot read the run token or reach the endpoint that spends money.

### Reusable actions

- Versioned action templates that `piw add` expands into ordinary workflow
  nodes. Each declares its effect class, retry safety, idempotency, and cost
  shape.

### Notes

- Deterministic orchestration pins configuration and preserves evidence; it
  does not make LLM output identical between runs.
- The advanced factory, certification, peer-review, and product-planning layers
  are not part of this release and live in a separate repository.
