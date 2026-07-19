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

## Node kinds

| Kind | How to declare it | Model | Use it for |
|---|---|---:|---|
| Command | `cmd: ...` | No | Scripts, APIs, transforms, deterministic checks |
| LLM | `prompt: ...` | Yes | One isolated completion with no tools |
| Tool | `prompt: ...` + `tools: "read,bash"` | Yes | One completion with an explicit tool allowlist |
| Agent | `prompt: ...` + `agent: true` | Yes | A full Pi tool loop with project context |
| QA | top-level `qa:` | Yes | Independent review after the graph completes |

Each step has exactly one of `cmd` or `prompt`. Use the weakest kind that can
finish the work. A `gate` verifies the artifact or effect mechanically; a model
never decides whether its own gate passed.

`tools:` is a Pi tool-selection boundary, not an operating-system sandbox.
In particular, allowing `bash` allows arbitrary commands with the current
user's permissions, and `agent: true` enables Pi's full default tool loop. Run
untrusted or unattended workflows inside an OS/container sandbox with only the
required files, credentials, and network access.

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

Run `piw schema` for the concise node/input catalog or `piw schema --json` for
the authoritative JSON Schema, including every top-level, step, judge, QA,
condition, and runtime-input field.
