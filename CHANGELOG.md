# Changelog

All notable changes to pi graph are documented here.

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
