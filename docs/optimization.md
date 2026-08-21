# Deterministic workflow optimization

Pi Graph can wrap an existing `steps.yaml` in a bounded experiment without creating a second workflow runner. Agents propose candidate workflow files; deterministic code freezes the contract, runs the canonical batch engine, checks evidence, keeps or restores exact bytes, and controls the one-time promotion decision.

## Lifecycle

```bash
# 1. Freeze the workflow, evaluator, parser, development corpus, boundaries,
#    model/tool/runtime pins, budgets, and private-holdout digest.
piw optimize init review/steps.yaml \
  --contract review/optimization-contract.json \
  --out review/optimization/run-001 --json

# 2. The untouched baseline must pass through the exact candidate path first.
piw optimize baseline review/optimization/run-001 --json

# 3. An agent may propose one mechanism from contract.boundaries.mutable.
piw optimize candidate review/optimization/run-001 \
  --file /tmp/candidate.yaml \
  --parent baseline \
  --mechanism synthesis-prompt \
  --hypothesis "Require claim-level evidence before synthesis" --json

# Use the returned incumbent id as the next --parent. Rejected candidates are
# restored byte-for-byte; their evidence remains append-only.
piw optimize status review/optimization/run-001 --history-limit 20 --json
piw optimize stop review/optimization/run-001 --reason "candidate budget complete" --json

# 4. Only a non-baseline development winner may consume the private holdout.
#    This command can reserve it once. An interrupted attempt is never replayed.
piw optimize promote review/optimization/run-001 \
  --holdout-file /private/holdout.jsonl --json
piw optimize receipt review/optimization/run-001 --json
```

`promoted` means the local evidence supports promotion. The optimization controller itself does **not** invoke commit, push, deploy, merge, or production-write operations. Workflow commands and agent nodes still run with the invoking user's normal authority; `authorized_effects` and `network` are audited declarations, not an OS sandbox. Use an external sandbox/container when strict effect or network isolation is required.

## Frozen contract

The complete machine contract is [`schemas/optimization-contract.schema.json`](../schemas/optimization-contract.schema.json). Important sections are:

- `objective`: primary metric, direction, minimum trusted gain, definition of done, and non-goals.
- `boundaries.mutable`: explicit mechanism IDs and JSON Pointers. A candidate must derive from the current incumbent and change exactly one declared pointer.
- `boundaries.protected_files`: evaluator, rubric, schema, parser, and other immutable files with SHA-256 fingerprints and frozen restoration copies. Drift blocks a candidate and restores declared protected bytes, but this is not a general filesystem sandbox.
- `development`: visible JSONL corpus, frozen hash, and exact row count.
- `holdout`: private JSONL hash and count only. Its path and bytes are not put into the manifest or normal evidence.
- `evaluation`: evaluator/parser argv arrays, source hashes, hard gates, repeats, seeds, uncertainty rule, and tie-breakers.
- `execution`: provider/model/thinking/tools/environment/dependencies, isolated batch settings, and cache policy.
- `budgets`: finite candidates, wall time, tokens, cost, failures, plateau, per-candidate limits, and promotion limits.
- `promotion`: holdout score, allowed development-to-holdout drop, and gate requirements.

All controller-launched evaluator/parser/gate commands are argv arrays—never shell strings—and their resolved executable bytes are fingerprinted. Workflow `cmd:` nodes retain their documented shell semantics. Evaluators write `pi-graph.optimization-metrics.v1`; Pi Graph rejects missing coverage, changed gate sets, non-finite values, usage below canonical child-run totals, or per-arm budget overruns.

## Durable evidence

An experiment contains immutable `contract.json` and `manifest.json`, baseline and active workflow snapshots, the frozen development corpus, candidate snapshots, canonical child batch runs, evaluation outputs, a hash-chained `events.jsonl` plus commit marker, projected `state.json`, and a final receipt.

The ledger is append-only, fsynced, sequence checked, and hash chained. Recovery accepts only its committed prefix. A torn final line is discarded before the next append; interior corruption fails closed. One nonblocking owner lock prevents concurrent writers.

Use:

```bash
piw optimize resume EXPERIMENT --json
```

Candidate interruption restores the parent snapshot without redispatch. A holdout interruption emits non-promotion evidence and permanently refuses another holdout invocation.

## Version and install identity

```bash
piw version --json
piw version --compare-root ~/.pi-graph --json
piw doctor --json
```

The identity includes the product version, source revision, bounded product-tree digest, runner and batch hashes, runtime versions, install-manifest integrity, and explicit drift codes. `install.sh` writes the installed manifest atomically. Agents should refuse paid optimization when `version` or `doctor` reports integrity drift.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Command completed; candidate may be kept or normally discarded |
| `1` | Valid non-promotion or hard-gate outcome |
| `2` | Usage, contract, transition, or missing-evidence error |
| `3` | Fingerprint/integrity drift |
| `4` | Another writer owns the experiment |
| `130` | Reserved for interruption; never infer that an operation can be replayed |

Every `--json` lifecycle response uses `pi-graph.optimize-response.v1` and includes the state, counters, paths, next legal actions, and a bounded structured error when applicable.
