<p align="center">
  <img src="docs/assets/pi-workflows-hero.svg" alt="pi workflows — deterministic graphs, probabilistic work" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ali-abassi/pi-workflows/releases"><img alt="release" src="https://img.shields.io/github/v/release/ali-abassi/pi-workflows"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-11110f"></a>
</p>

# pi workflows

**Your agent does not get to skip steps.**

Have you ever given an agent five exact steps, then watched it skip two, reorder
the rest, and confidently say it was done? The model thought it knew better.
Now the result is incomplete, the evidence is missing, and you have to inspect
everything yourself.

Never let that happen again. **pi workflows** turns required work into a
deterministic graph that code—not the model—controls. Every declared step runs
only after its dependencies, every branch follows typed rules, and every gate
must pass before the workflow can claim success. Models still research, write,
review, and code; they just cannot silently rewrite the process.

Write the workflow once in readable YAML. Run it for one task or 1,000 inputs.
Inspect every action, retry only failed work, compare models node by node, and
see exactly where the time and tokens went.

> Deterministic means the graph, transitions, gates, and completion rules are
> code-owned. Live model outputs can still vary, so pi workflows lets you pin
> models, validates outputs, and preserves evidence instead of pretending LLM
> answers are identical.

> pi workflows is an independent community project. It uses Pi's public package,
> skill, JSON-mode, and model interfaces; it is not an official Pi project.

## What your agent can no longer skip

- **The order.** Dependencies, parallel work, routes, retries, timeouts, and stop
  conditions are enforced by the runner.
- **The quality checks.** Schemas and shell gates decide whether a node passed;
  a confident model cannot wave a failed check through.
- **The evidence.** Every run keeps inputs, outputs, attempts, errors, tokens,
  cost, timing, cache state, and Git history.
- **The scale contract.** The same validated graph can run across 1,000 isolated
  inputs with bounded concurrency, resumable receipts, and ordered outputs.
- **The cost controls.** Pin models and reasoning per node, compare candidates
  against fixed judges, and optimize the actual hotspot instead of guessing.
- **The reusable work.** Start from inspectable action templates for review,
  extraction, coding, evidence, JSONL, failure triage, and exact-item pipelines.
- **Portable by default.** The CLI and YAML format work without Agent X or
  Loops. Pi, Codex, and Claude Code discover the same installed skill.

```mermaid
flowchart LR
  A["Agent authors steps.yaml"] --> V["Validate graph + contracts"]
  V -->|pass| R["Run deterministic DAG"]
  V -->|fail| A
  R --> G["Mechanical gates"]
  G -->|retry within budget| R
  G --> E["Evidence ledger"]
  E --> O["Evaluate + optimize"]
  O --> A
```

## Where it shines: one exact workflow, 1,000 items

This is the primary use case. Tell an agent, “run these five steps exactly for
these 1,000 records.” The agent validates the graph once, canaries a few items,
then starts a detached bulk job. pi workflows—not the agent—owns the queue,
ordering, retries, isolation, and completion accounting.

```bash
cd examples/workflows/13-thousand-item-pipeline
python3 generate_corpus.py --count 1000

# Cheap canary before committing the whole corpus
piw batch steps.yaml --inputs corpus.jsonl --limit 5 --require-all

# Run all 1,000 in the background with 16 isolated items in flight
piw batch steps.yaml --inputs corpus.jsonl --parallel 16 \
  --require-all --stop-after-failures 3 \
  --max-tokens 2000000 --max-cost 25 --output-step publish --detach --json

# The launch receipt returns this exact status command
piw batch-status /path/to/batch-dir --json
```

```mermaid
flowchart LR
  A["Agent validates steps.yaml once"] --> F["Freeze graph + corpus digests"]
  F --> Q["Bounded item queue"]
  Q --> I1["item 0001 · steps 1→5"]
  Q --> I2["item 0002 · steps 1→5"]
  Q --> IX["item 1000 · steps 1→5"]
  I1 --> R["Per-item artifacts + events + ledger"]
  I2 --> R
  IX --> R
  R --> P{"1,000 complete contracts?"}
  P -->|yes| D["Batch passes"]
  P -->|no| X["Resume only failed or unfinished items"]
```

Every item receives its own immutable input, attempt directory, event stream,
artifacts, gate results, token/cost ledger, and result receipt. The batch
manifest pins the workflow and corpus digests; `--resume` fails closed if
either changed. Item-level Git history is off by default at bulk scale because
the event and artifact ledgers already preserve the execution proof; opt in
with `--git-history` when that extra storage is worth it.

`--parallel` controls concurrent **items**. `workers:` in `steps.yaml` controls
parallel nodes **inside each item**. Their product is the maximum potential
step concurrency, so increase them deliberately. Parallel batch execution
fails closed when a workflow contains `agent: true` or `produces:` because
those nodes may race in the shared workspace. `--allow-shared-workspace` is an
explicit opt-in for workflows that provide their own isolation or resource lock.

`piw batch-cancel <batch-dir>` asks the controller to terminate every active
item process group before it records `cancelled`; undispatched items remain
`not_run` instead of continuing as orphan processes.

`--max-tokens` and `--max-cost` are fail-closed **dispatch ceilings** over
usage recorded in every attempt ledger, including failed attempts. They are
not provider-side reservations: already-running items drain, and the receipt
reports any overshoot. `--output-step <id>` writes an input-ordered,
exact-cardinality `outputs.jsonl` plus a digest manifest, so downstream agents
can distinguish successful output from failed, skipped, and never-run items.

## Studio UI

The optional local Studio is a graph runner and flight recorder over the same
canonical `steps.yaml`. It does not introduce a second engine or hidden graph
format.

```bash
piw ui examples/workflows/11-parallel-analysis-qa/steps.yaml \
  --input-file examples/workflows/11-parallel-analysis-qa/input.txt
```

<p align="center">
  <img src="docs/assets/pi-workflows-studio.png" alt="pi workflows Studio showing a parallel analysis graph, node inspector, and flight recorder" width="100%">
</p>

Click a node to inspect its runtime contract. Start a run to watch nodes move
through running, cached, passed, failed, or skipped states. The result view shows
the final artifact and identifies the highest-cost node as the first optimization
target.

## Install

Prerequisites: macOS or Linux, Python 3.11+, Node.js 20+, and Pi.

```bash
git clone https://github.com/ali-abassi/pi-workflows.git
cd pi-workflows
./install.sh
piw doctor
```

The installer creates an isolated runtime at `~/.pi-workflows`, exposes `piw`
from `~/.local/bin`, registers the native Pi package, and links the same skill
for Codex and Claude Code. Set `PI_WORKFLOWS_HOME` or
`PI_WORKFLOWS_BIN_DIR` to choose other locations.

Pi packages execute code with your user permissions. Review third-party
workflows before running them; see [`SECURITY.md`](SECURITY.md).

## Your first graph

YAML is the human-first source of truth. It is versioned, comments stay intact,
and multiline prompts remain readable. JSON Schema provides editor completion
and an authoritative machine contract.

```yaml
version: 1
workflow: release-notes
model: openai-codex/gpt-5.6-luna
thinking: low

input:
  required: true
  description: Git diff or release summary

steps:
  - id: draft
    prompt: |
      Write concise release notes from this untrusted input:
      {input}
    gate: test -s "$OUT"

  - id: final
    needs: [draft]
    cmd: cp "$RUN/draft.md" "$OUT"
    gate: test -s "$OUT"
```

```bash
piw validate steps.yaml
piw graph steps.yaml
piw run steps.yaml --input-file changes.txt
piw detail steps.yaml
piw stats steps.yaml
```

## Start from tested actions

Users and agents do not need to rebuild common graph fragments. Actions are
versioned authoring templates that expand into normal `steps.yaml` nodes—there
is no hidden action runtime or second graph format.

```bash
piw actions
piw actions parallel-review
piw create review --action parallel-review
piw add review/steps.yaml extract-action-items \
  --id extract --needs parallel-review-verdict
piw validate review/steps.yaml
```

The included catalog covers typed extraction and routing, parallel independent
review, bounded judge/refine, repository change + diff verification, evidence
synthesis, canonical JSONL, the five-stage exact item pipeline, explicit agent
handoffs, batch-readiness review, failure triage, and adversarial repair. Each
action advertises effects, retry safety, idempotency expectations, and cost
shape before expansion. See
[`docs/actions.md`](docs/actions.md) for every input/output/failure contract.

```mermaid
flowchart LR
  I(["immutable input"]) --> D["draft · LLM"]
  D -->|"gate passes"| F["final · command"]
  D -.->|"bounded retry"| D
  F --> E[("run evidence")]
```

## Node system

pi workflows has four execution runtimes and one final review boundary. Use the
weakest runtime that can finish the step.

| Runtime | Declaration | Model call | Best for |
|---|---|---:|---|
| Command | `cmd: ...` | No | Scripts, APIs, transforms, deterministic checks |
| LLM | `prompt: ...` | Yes | One isolated completion with no tools |
| Tool | `prompt:` + `tools:` | Yes | One completion with an explicit Pi tool allowlist |
| Agent | `prompt:` + `agent: true` | Yes | A full Pi agent loop with project context |
| Final QA | top-level `qa:` | Yes | Independent review after the graph completes |

The dynamic behavior comes from composable graph capabilities, not a long list
of cosmetically different nodes:

| Capability | Declaration | Authority |
|---|---|---|
| Parallel fan-out | dependency-ready roots + `workers` | runner |
| Join | `needs: [a, b]` | runner |
| Typed route | source `schema:` + branch `when:` / `from:` | code |
| Mechanical verification | `gate:` | code |
| Bounded recovery | `retries`, `retry_on`, delay/backoff/jitter + `timeout` | runner |
| Semantic improvement | `judge:` | model advises; gate decides |
| Final review | top-level `qa:` | structured verdict |
| Cost avoidance | content-addressed prompt cache | runner |
| Artifact history | `produces:` + `preview:` | runner |

Agents can inspect every field, runtime input, and capability without guessing:

```bash
piw schema          # concise catalog
piw schema --json   # authoritative JSON Schema + capability metadata
```

See [`docs/workflow-format.md`](docs/workflow-format.md) for the complete format
and [`docs/node-system.md`](docs/node-system.md) for current boundaries and the
roadmap for human checkpoints, subworkflows, and dynamic map nodes.

## Branches stay deterministic

```yaml
  - id: classify
    prompt: 'Return JSON only: {"kind":"bug"|"feature"}'
    schema:
      kind:
        type: string
        enum: [bug, feature]
    gate: python3 -c "import json; json.load(open('$OUT'))"

  - id: fix_bug
    needs: [classify]
    from: classify
    when: {op: equals, path: /kind, value: bug}
    agent: true
    prompt: Fix the reported bug and run the relevant tests.
    gate: npm test
```

```mermaid
flowchart LR
  C["classify · typed LLM"] --> R{"kind == bug?"}
  R -->|yes| B["fix_bug · agent"]
  R -->|no| S["skip fix_bug"]
  B --> T["npm test · gate"]
```

Validation fails when a route reads a field its source does not declare, so a
valid-but-wrong model response cannot silently send work down the wrong path.

## Test, evaluate, optimize

```bash
# Run one action fresh; upstream artifacts may come from cache
piw run steps.yaml --node draft

# Run the exact graph across a corpus; fail if any declared step is skipped
piw batch steps.yaml --inputs items.jsonl --require-all --parallel 8

# Compare models while holding the workflow and judges fixed
piw eval steps.yaml --inputs evals.jsonl --input-file input.txt \
  --models openai-codex/gpt-5.6-luna,openai-codex/gpt-5.6-terra

# Inspect pass rate, cache hits, tokens, cost, and latency by node
piw stats steps.yaml
```

Passing prompt outputs are content-addressed. Cache hits skip model and judge
calls but rerun the mechanical gate, keeping reuse cheap without trusting stale
artifacts blindly.

## Examples

The [`examples/`](examples/) catalog contains 14 runnable workflows, from two
shell steps to parallel agents, typed routing, tool allowlists, bounded judge
loops, final QA, caching, cost analysis, and an exact five-step 1,000-item bulk
pipeline.

```bash
python3 scripts/run_example_suite.py --validate-only  # free contract check
python3 scripts/run_example_suite.py                  # live Luna-medium suite
```

The live suite preserves a report, every log, and every ledger under the
gitignored `examples/.artifacts/` directory. See
[`examples/README.md`](examples/README.md) for runnable commands and Mermaid
graphs of the sequential, parallel, conditional, and judged examples.

## Core commands

```text
piw create <name>                 scaffold a valid workflow
piw create <name> --action <id>   scaffold from a tested action template
piw actions [id]                  list or inspect reusable actions
piw add <workflow> <action> ...   expand an action into ordinary YAML nodes
piw schema [--json]               inspect every field, node, and runtime input
piw ls [--json]                   discover workflows
piw graph <workflow> [--json]     inspect the DAG
piw validate <workflow> [--json]  fail closed before a paid run
piw run <workflow>                execute and stream node evidence
piw batch <workflow>              run the graph over an isolated input corpus
piw batch-status <dir>            inspect a detached bulk job
piw batch-cancel <dir>            stop a detached bulk job
piw ui <workflow>                 open the optional local graph studio
piw detail <workflow>             inspect the latest run
piw show <workflow> <step>        print one artifact
piw stats <workflow>              pass, cache, token, cost, and timing counters
piw schedule <workflow> ...       add an optional durable Loops schedule
piw doctor [--json]               verify the installation and integrations
```

Every inspection command supports `--json`. Failed validation and failed runs
exit non-zero and preserve their artifacts, stderr, event log, and cost ledger.

## Integrations

- **Pi:** the `pi_workflows` native tool can create, inspect, validate, run, and
  schedule workflows. Its `schema` action exposes the authoring contract.
- **Agent X:** invokes the same installed CLI and runtime; Agent X remains the
  coding and orchestration harness.
- **Loops:** adds the durable schedule and shared graph canvas. `steps.yaml`
  remains source of truth.
- **Codex and Claude Code:** discover the same workflow skill and CLI, so either
  agent can author, test, run, inspect, and improve a graph.

## Develop

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML>=6,<7' 'ruamel.yaml>=0.18,<0.19' 'jsonschema>=4.23,<5'
npm ci --ignore-scripts
npm test
npm run check
npm run test:examples
```

The core runner is intentionally small. The workflow factory, certification,
replay, peer-review, and product-planning harnesses are advanced opt-in layers
under [`references/`](references/).

## License

[MIT](LICENSE) · Contributions are welcome; see
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CHANGELOG.md`](CHANGELOG.md).
