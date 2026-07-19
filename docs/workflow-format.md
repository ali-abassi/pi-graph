# Workflow format

Pi Workflows uses one human-authored file: `steps.yaml`. YAML is the canonical
syntax because comments and multiline prompts stay readable. The complete
machine contract is [`schemas/workflow.schema.json`](../schemas/workflow.schema.json),
which editors and agents can inspect with `piw schema --json`.

## Smallest useful workflow

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

## Execution runtimes

| Kind | How to declare it | Model | Use it for |
|---|---|---:|---|
| Command | `cmd: ...` | No | Scripts, APIs, transforms, deterministic checks |
| LLM | `prompt: ...` | Yes | One isolated completion with no tools |
| Tool | `prompt: ...` + `tools: "read,bash"` | Yes | One completion with an explicit tool allowlist |
| Agent | `prompt: ...` + `agent: true` | Yes | A full Pi tool loop with project context |
| QA | top-level `qa:` | Yes | Independent review after the graph completes |

Each step has exactly one of `cmd` or `prompt`. Use the weakest runtime that can
finish the work. A `gate` verifies the artifact or effect mechanically; a model
never decides whether its own gate passed.

Runtime kinds stay small on purpose. Dynamic behavior comes from composable
fan-out, join, route, gate, retry, judge, QA, cache, and artifact capabilities.
See [`node-system.md`](node-system.md) for the complete model and first-class
capabilities under consideration.

`tools:` is a Pi tool-selection boundary, not an operating-system sandbox.
In particular, allowing `bash` allows arbitrary commands with the current
user’s permissions, and `agent: true` enables Pi's full default tool loop. Run
untrusted or unattended workflows inside an OS/container sandbox with only the
required files, credentials, and network access.

Reusable actions do not add runtime kinds. `piw create --action` and `piw add`
expand a tested fragment into ordinary nodes before validation. See
[`actions.md`](actions.md).

## Inputs available inside nodes

Prompt nodes can use:

| Value | Meaning |
|---|---|
| `{input}` | Immutable run input, labelled as untrusted data |
| `{step.ID}` | Complete artifact from an earlier node |
| `{prev}` | Artifact from the previous listed node |
| `{run}` | Absolute run-directory path |

Command nodes and gates receive:

| Environment variable | Meaning |
|---|---|
| `$INPUT` | Path to immutable `input.txt` |
| `$PI_WORKFLOWS_INPUT` | Alias of `$INPUT` |
| `$OUT` | Path where this node's primary artifact belongs |
| `$RUN` | Absolute run-directory path |
| `$STEP` | Current node id |

Judge prompts additionally receive `{out}`. Final `qa.prompt` receives
`{artifacts}`. Both also receive `{run}`.

## Bulk execution

`piw batch` freezes the validated `steps.yaml`, snapshots a corpus digest, and
runs that exact graph once per item through the canonical runner. Each item has
an immutable input and numbered attempt directories. The aggregate receipt
records expected, passed, failed, skipped, and terminal steps per item.

Use `--require-all` when the contract says every declared node must run; a
deterministic route that skips a node then fails that item intentionally. Use
`--resume <batch-dir>` after interruption or repair: already-passed items are
not repeated, while graph or corpus drift is rejected before execution.

Parallel items share the declared workflow `cwd`. To prevent obvious
cross-item mutation races, `piw batch --parallel N` rejects workflows containing
`agent: true` or `produces:` when `N > 1`. Use
`--allow-shared-workspace` only after the workflow provides an independent
workspace or resource lock. Cancellation is controller-coordinated: active item
process groups are terminated and evidenced as `cancelled`, while items that
were never dispatched remain `not_run`.

`--max-tokens N` and `--max-cost N` stop new dispatch from recorded cumulative
usage. Historical failed attempts count. In-flight work may finish past the
threshold, so the receipt exposes token/cost overshoot rather than describing
these as hard provider caps. `--output-step <id>` validates the selector before
paid work and exports one `outputs.jsonl` row per corpus item in original order,
plus `outputs.manifest.json` with completeness counts and a SHA-256 digest.
These settings are frozen into the batch manifest and cannot drift on resume.

## Dependencies, routes, and output contracts

- `needs: [a, b]` waits for both nodes. `needs: []` creates a root node.
- Omitting `needs` preserves the simple linear case by depending on the previous
  listed node.
- Referencing `{step.a}` automatically adds `a` as a dependency.
- `schema:` declares a flat JSON output contract that code validates.
- `when:` reads typed JSON from `from:` and deterministically takes or skips a
  branch.

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

Validation fails when a route reads a field its source does not promise, so a
valid-but-wrong model response cannot silently skip the intended branch.

## Complete field reference

Retry behavior can be narrowed and paced per step:

```yaml
retries: 3
retry_on: [model_error, schema_failed]
retry_delay_seconds: 1
retry_backoff: exponential
retry_max_delay_seconds: 8
retry_jitter: 0.2
timeout: 120
```

Every failure is classified as `command_exit`, `model_error`, `gate_failed`,
`schema_failed`, or `judge_below_target`. Omitted `retry_on` retains the v1
compatibility behavior of retrying every class. Jitter is derived from step id
and attempt, so pacing is reproducible rather than random; retry decisions and
delays are emitted into the event/log evidence.

Run `piw schema` for the concise node/input catalog or `piw schema --json` for
the authoritative JSON Schema, including every top-level, step, judge, QA,
condition, and runtime-input field.
