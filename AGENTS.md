# pi workflows

pi workflows is the canonical source for the deterministic workflow product.
Codex, Claude Code, and any other agent harness are integrations; none of them
owns the runner, workflow schema, CLI contract, or agent skill.

## If you are using pi workflows

Use it when a task has required order, repeatable units, branches, retries,
quality gates, cost limits, or enough volume that an agent should not remember
the process. Skip it for a one-step task that ordinary tools can finish safely.

### Required operating loop

1. **Contract:** name one input unit, the final artifact, required steps,
   mechanical gates, optional semantic QA, external effects, volume, and budget.
2. **Reuse first:** inspect `piw actions --json`; expand an existing action with
   `piw create --action` or `piw add` before writing new nodes.
3. **Validate free:** run `piw validate <workflow> --json`. Do not spend tokens
   while validation is red.
4. **Canary:** run one representative input. Preserve the returned run id.
5. **Inspect:** use `piw detail <workflow> <run>` for the whole trace, then
   `--step <id> --io` for every paid, judged, failed, or effectful node.
6. **Improve one variable:** use `piw set` to change one prompt, model, reasoning
   level, gate, or per-node judge; rerun it with `piw run --node <id>`.
7. **Compare:** use `piw compare <workflow> <baseline> <candidate>`. Keep a
   candidate only when gates and QA hold and cost, tokens, or latency improve.
8. **Evaluate:** use `piw eval` on a fixed corpus before changing the default
   model policy. Keep judges fixed while generators vary.
9. **Scale:** canary `piw batch --limit`; then run the frozen graph with
   `--require-all`, failure ceilings, token/cost ceilings, and `--output-step`.

Use `--json` whenever another agent consumes the result. A successful process
exit is not enough evidence: completion requires the expected node artifacts,
gates, QA verdict, and run ledger. A failed run is evidence to inspect, not a
reason to bypass a gate or silently drop an item.

### Minimal command loop

```text
piw actions --json
piw create <name> --action <action>
piw validate <workflow> --json
piw run <workflow> --input-file <path> --json
piw detail <workflow> <run> --json
piw detail <workflow> <run> --step <id> --io --json
piw set <workflow> <id> --model <model> --thinking <level>
piw set <workflow> <id> --judge-prompt-file <path> --judge-score <score>
piw run <workflow> --input-file <path> --node <id> --json
piw compare <workflow> <baseline> <candidate> --json
piw eval <workflow> --inputs <corpus> --input-file input.txt --models <ids>
piw batch <workflow> --inputs <corpus> --limit 5 --require-all --max-cost <usd> --output-step <id>
```

Done means the expected artifacts exist, required nodes reached valid terminal
states, gates passed, QA passed when configured, and the ledger supports every
completion and cost claim.

## If you are changing this repository

### Product invariants

- Code owns control flow. Models may generate or judge artifacts, but cannot
  decide whether a gate passed or silently skip declared nodes.
- Versioned `steps.yaml` is source truth; `schemas/workflow.schema.json` is its
  machine-readable contract. `steps.layout.json` is presentation state owned
  by the graph canvas.
- Validate before paid execution. A failed run exits non-zero and preserves its
  artifacts, event log, stderr, and ledger.
- Every model, tool, shell, file, schedule, and external-effect boundary must be
  explicit and inspectable.
- Workflow inputs are immutable per run. Concurrent runs must never share an
  input staging file.
- The product works with no integration installed. An optional harness may add
  a TUI tool, a visual graph, or durable scheduling without changing semantics.
- Keep prompts compact. Use deterministic checks for schemas, routes, gates,
  budgets, permissions, and completion.

### Canonical commands

```text
./bin/piw doctor
./bin/piw schema --json
./bin/piw ls
./bin/piw graph <workflow>
./bin/piw validate <workflow>
./bin/piw run <workflow> --input-file <path>
./bin/piw detail <workflow>
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

Read `SKILL.md` before changing product behavior. Preserve
unrelated working-tree changes. Do not commit generated runs, cache entries,
credentials, authentication state, or customer data.
