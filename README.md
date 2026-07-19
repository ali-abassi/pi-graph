# Pi Workflows

Deterministic workflow graphs for [Pi](https://github.com/earendil-works/pi).
Write a small `steps.yaml`; Pi Workflows owns ordering, parallelism, gates,
routes, retries, evidence, and cost. Models produce or judge artifacts, but code
decides what runs and whether it passed.

Pi Workflows is an independent open-source product. Agent X and Loops integrate
with it, but neither is required and neither owns a second workflow engine.

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

## The format

YAML is the human-first source of truth. The format is versioned, comments are
allowed, and multiline prompts remain readable. A JSON Schema provides editor
completion and a complete machine contract.

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
piw run steps.yaml --input-file changes.txt
piw detail steps.yaml
```

See [`docs/workflow-format.md`](docs/workflow-format.md) for dependencies,
typed output, deterministic branching, gates, judges, QA, and every available
runtime input.

## Examples

The [`examples/`](examples/) catalog contains 12 runnable workflows ordered from
starter to complex. Together they cover sequential and parallel execution,
retries, typed output, deterministic branches, least-privilege tools, full
agents, judge loops, QA, caching, logs, ledgers, and optimization hotspots.

```bash
python3 scripts/run_example_suite.py --validate-only  # free contract check
python3 scripts/run_example_suite.py                  # live Luna-medium suite
```

The live suite keeps evidence under the gitignored `examples/.artifacts/`
directory and produces a report linking every run log and ledger. The accepted
measurements are in
[`docs/example-certification.md`](docs/example-certification.md).

## Nodes and inputs

The format has five legible node kinds:

| Node | Declaration | What it does |
|---|---|---|
| Command | `cmd: ...` | Runs deterministic shell or program logic |
| LLM | `prompt: ...` | Runs one isolated Pi completion |
| Tool | `prompt:` + `tools:` | Runs one completion with an explicit tool allowlist |
| Agent | `prompt:` + `agent: true` | Runs a full Pi agent loop with project context |
| QA | top-level `qa:` | Independently reviews the completed graph |

Agents never need to guess the contract:

```bash
piw schema          # concise human/agent catalog
piw schema --json   # authoritative JSON Schema + node and runtime-input metadata
```

Prompt nodes can reference `{input}`, `{step.ID}`, `{prev}`, and `{run}`.
Command nodes and gates receive `$INPUT`, `$PI_WORKFLOWS_INPUT`, `$OUT`, `$RUN`,
and `$STEP`. Judge prompts receive `{out}`; final QA receives `{artifacts}`.

## Core commands

```text
piw create <name>                 scaffold a valid workflow
piw schema [--json]               inspect every field, node, and runtime input
piw ls [--json]                   discover workflows
piw graph <workflow> [--json]     inspect the DAG
piw validate <workflow> [--json]  fail closed before a paid run
piw run <workflow>                execute and stream node evidence
piw detail <workflow>             inspect the latest run
piw show <workflow> <step>        print one artifact
piw stats <workflow>              pass, cache, token, cost, and timing counters
piw schedule <workflow> ...       add an optional durable Loops schedule
piw doctor [--json]               verify the installation and integrations
```

Every inspection command supports `--json`. Failed validation and failed runs
exit non-zero and preserve their artifacts, stderr, event log, and cost ledger.

## Deterministic guarantees

- Dependencies and `when:` routes are evaluated by code, not a model.
- Each run gets an immutable, isolated copy of its input.
- Gates are shell checks; exit 0 passes and any other exit fails.
- Typed JSON output is checked before a downstream route can read it.
- A route that references an undeclared field fails validation instead of
  silently skipping a branch.
- Retries are bounded, timeouts fail inside the retry boundary, and failed
  attempts remain inspectable.
- Passing prompt outputs are content-addressed; cache hits skip model and judge
  calls while rerunning the gate.
- Every run records artifacts, attempts, tokens, cost, time, and git history.

## Integrations

- **Pi:** the `pi_workflows` native tool can create, inspect, validate, run, and
  schedule workflows. Its `schema` action exposes the authoring contract.
- **Agent X:** uses the same installed CLI and runtime; Agent X remains the
  coding and orchestration harness.
- **Loops:** adds a graph canvas, live run state, inspection, and scheduling.
  `steps.yaml` remains source of truth.
- **Codex and Claude Code:** share the same workflow skill and CLI.

## Develop

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML>=6,<7' 'ruamel.yaml>=0.18,<0.19'
npm ci --ignore-scripts
npm test
npm run check
npm run test:examples
PI_WORKFLOWS_ROOTS="$PWD/examples/workflows" ./bin/piw validate examples/workflows/01-hello-command/steps.yaml
PI_WORKFLOWS_ROOTS="$PWD/examples/workflows" ./bin/piw run examples/workflows/01-hello-command/steps.yaml --input Ada
```

The normal graph runner is intentionally small. The production workflow
factory, certification, replay, peer-review, and product-planning harnesses are
advanced opt-in layers documented under [`references/`](references/).

## License

[MIT](LICENSE)

Contributions are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md) and the
[`CHANGELOG.md`](CHANGELOG.md).
