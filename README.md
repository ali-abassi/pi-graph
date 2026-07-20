<p align="center">
  <img src="docs/assets/pi-graph-hero.svg" alt="pi graph" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ali-abassi/pi-graph/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ali-abassi/pi-graph/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/ali-abassi/pi-graph/releases"><img alt="release" src="https://img.shields.io/github/v/release/ali-abassi/pi-graph"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-11110f"></a>
</p>

# pi graph

**pi graph gives coding agents a reliable way to create deterministic
workflows that execute every required step and prove what happened.**

Deterministic YAML workflow graphs for coding agents. Your agent authors
`steps.yaml`; from then on code owns order, gates, retries, and budget. The
model does the work inside each node and cannot skip one. Every run leaves
per-node evidence.

`piw` orchestrates. [Pi](https://github.com/earendil-works/pi) — an open-source
CLI coding agent — executes model nodes using the provider account you already
pay for. Shell-only workflows need neither.

## Install

```bash
npm install -g @earendil-works/pi-coding-agent   # model runtime
pi                                                # then /login, pick provider, /exit
git clone https://github.com/ali-abassi/pi-graph.git
cd pi-graph && ./install.sh
piw doctor                                        # installed piw, not ./bin/piw
```

macOS/Linux · Python 3.10+ · Pi 0.80.10+ for model nodes ·
`piw` installs from this clone, not npm · `./install.sh --uninstall` reverses it.

## The loop

```bash
piw create review --action parallel-review              # scaffold a valid graph
piw validate review/steps.yaml                          # free; no model call
piw run review/steps.yaml --input-file task.md
piw detail review/steps.yaml RUN_ID --step parallel-review-verdict --io
piw set review/steps.yaml parallel-review-verdict --model MODEL --thinking low
piw run review/steps.yaml --input-file task.md --node parallel-review-verdict
piw compare review/steps.yaml BASELINE_RUN CANDIDATE_RUN
```

## Node kinds

| Kind | Declared by | Behavior |
|---|---|---|
| `command` | `cmd:` | Shell execution. No model, no variance. |
| `llm` | `prompt:` | One isolated completion, no tools or project context. |
| `tool` | `prompt:` + `tools:` | One completion with an explicit tool allowlist. |
| `agent` | `prompt:` + `agent: true` | Full agent loop with normal tools and repo context. |
| `qa` | top-level `qa:` | Independent review after the graph completes. |

Every node takes `gate:` (shell assertion, exit 0 passes), and optionally
`needs:`, `retries:`, `judge:`, `schema:`, `when:`, `produces:`, `timeout:`.

Pin a model as `provider/id` — the first two columns of `pi --list-models`,
joined with a slash. A drifted model **fails the step** rather than silently
answering with another.

**Run `piw schema` for the full contract, or `piw schema --json` for the
machine-readable form.** That is the authoritative reference, not this file.

## Commands

| | |
|---|---|
| `ls` `graph` `schema` `actions` | Inspect what exists |
| `create` `add` `set` `validate` | Author and check, without spending |
| `run` `batch` `batch-status` `batch-cancel` | Execute |
| `detail` `runs` `show` `compare` `stats` | Evidence after the fact |
| `eval` `reports` | Compare models over a corpus, judges fixed |
| `ui` `doctor` `path` | Studio, health, locations |

`--json` on every inspection command. Non-zero exit on failure.

## Scale

```bash
piw batch steps.yaml --inputs corpus.jsonl --limit 5 --require-all      # canary
piw batch steps.yaml --inputs corpus.jsonl --parallel 16 \
  --require-all --stop-after-failures 3 --max-tokens 2000000 \
  --max-cost 25 --output-step publish --detach --json
```

`--inputs` takes JSONL (`{"content": "...", "id": "optional"}`) or a directory
(one file per item, filename stem becomes `id`). Each item gets an isolated
workspace, attempt directory, event stream, ledger, and receipt. The manifest
pins SHA-256 of workflow and corpus; `--resume` refuses if either changed.
Batch is per-item and never aggregates — combine `outputs.jsonl` with a second
workflow.

## Why not LangGraph, Temporal, or n8n?

They orchestrate **code**; this orchestrates **model calls**. Temporal/Prefect
give durable execution but have no per-node model, judge, or token ledger.
LangGraph/CrewAI are libraries you write agents in — here the graph is a YAML
file an agent authors, validates for free, and is mechanically prevented from
deviating from. n8n/Dify target humans wiring SaaS nodes, not an agent
inspecting and improving a graph from a terminal.

If your steps are deterministic code, use a real orchestrator.

## Reference

| | |
|---|---|
| [`SKILL.md`](SKILL.md) | Operating contract for an agent using this |
| [`docs/workflow-format.md`](docs/workflow-format.md) | Every `steps.yaml` field |
| [`docs/node-system.md`](docs/node-system.md) | Node kinds, gates, routing, QA |
| [`docs/actions.md`](docs/actions.md) | Reusable action templates |
| [`docs/integration-contract.md`](docs/integration-contract.md) | Harness and scheduler integration |
| [`examples/`](examples/) | Runnable workflows, simplest first |
| [`AGENTS.md`](AGENTS.md) | Working inside this repo |

`PI_GRAPH_ROOTS` (discovery paths), `PI_GRAPH_HOME`,
`PI_GRAPH_BIN_DIR`, `PI_GRAPH_PYTHON`, `PI_GRAPH_MODEL`,
`PI_GRAPH_QA_MODEL`.

## Develop

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
npm ci --ignore-scripts
npm run verify          # tests, typecheck, examples, live run, regression guards
```

Workflows execute shell and model calls with your permissions — review
third-party graphs before running them ([`SECURITY.md`](SECURITY.md)).

## License

[MIT](LICENSE) · [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`CHANGELOG.md`](CHANGELOG.md)
