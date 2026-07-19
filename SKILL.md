---
name: pi-workflows
description: "Create or use deterministic Pi workflow graphs for repeatable multi-step work, automations, scheduled loops, explicit gates, branching, model/tool configuration, run evidence, caching, cost control, and independent QA. Use when ordering, validation, routing, retries, artifacts, or a schedule must be mechanically reliable; skip for a one-step task that ordinary tools can finish directly."
---

# Deterministic Workflows

The failure this prevents: an agent given N steps skips some, reorders them, or declares done early. The structural fix: **the model never owns control flow** — a runner calls the model once per step, gates decide pass/fail, artifacts chain between steps. The graph transition logic is deterministic for the same validated node outputs; model outputs can vary, so pin models, gate them, and retain run evidence instead of promising identical paths across live LLM calls.

CLI: `piw`. Runner: `scripts/run_steps.py`. No compilation or certification is
required for the normal path. Run `piw schema` for the concise node/input
catalog or `piw schema --json` for the complete machine-readable contract.
Before rebuilding a common graph fragment, run `piw actions`; templates expand
into ordinary inspectable nodes with `piw create --action` or `piw add`.

## Stage 1 — Understand the task

Before writing any yaml, answer these from the user's request (infer where obvious, ask only what materially changes the build):

1. **Task + volume:** what is one unit of work, and how many times will it run (once / daily / 500×)? Volume decides how much to invest in gates and caching.
2. **Input → output contract:** what goes in (a file? a record?) and what artifact comes out? Name the output step.
3. **Quality bar:** what does "good" mean, and is it checkable by command (gate), by rubric (judge), or only by a human (checkpoint)?
4. **Cost/model policy:** which steps deserve a strong model? Default cheap (`luna`), pin up (`sol`) only for reasoning/synthesis nodes; judges/QA on a different model than generators.
5. **Failure policy:** what should happen when a step can't pass — halt (default), keep-best, or human checkpoint?
6. **Externalities:** web research, APIs, repo mutation? Those become `cmd:` steps (controller-owned, keys never in model context) or `agent: true` steps with effect-gates.
7. **Trigger:** manual, interval, or daily? Scheduling belongs to Loops; the workflow remains independently runnable.

One paragraph of these answers is the workflow contract. Then build.

## Stage 2 — Build the workflow

Start from the action catalog when one contract fits:

```bash
piw actions --json
piw create review --action parallel-review
piw add review/steps.yaml extract-action-items --id extract --needs parallel-review-verdict
```

The catalog includes typed extraction/classification, parallel review,
judge/refine, evidence synthesis, repo change + diff verification, canonical
JSONL, an exact five-stage item pipeline, typed handoffs, batch-readiness
review, failure triage, and adversarial repair. Every action declares effects,
retry safety, idempotency expectations, and cost shape. Expansion is
authoring-time only; always inspect and validate the resulting `steps.yaml`.

Write a `steps.yaml` (full example: `templates/idea-to-plan.steps.yaml` — research, GO/NO-GO gate, debate, per-page copy, review→patch→apply quality loop, checklist output):

```yaml
version: 1
workflow: spec-review
model: openai-codex/gpt-5.6-luna      # cheap default; per-step overrides below
thinking: low
workers: 6                             # DAG parallelism
input:
  required: true
  description: One immutable review request
system: |                              # chain hygiene for writing chains (optional)
  You are writing ONE section of a single consistent document...
qa:                                    # independent reviewer, different model
  model: openai-codex/gpt-5.6-terra
  prompt: |
    ... Output JSON only: {"verdict": "pass"|"fail", "issues": ["..."]}
    {artifacts}
steps:
  - id: intake
    prompt: |
      Normalize the requirements in {input}
    gate: test -s "$OUT"
  - id: fetch                          # pure code step — no model, zero variance
    cmd: ./scripts/fetch.sh > "$OUT"   # env: OUT, RUN, STEP
  - id: extract
    prompt: |
      Extract requirements as a numbered list. <input>{step.fetch}</input>
    gate: grep -qE '^1\.' "$OUT"       # exit 0 = pass
  - id: plan
    model: openai-codex/gpt-5.6-sol    # reasoning node: pin up
    thinking: high
    needs: [extract]
    prompt: |
      Plan from: {step.extract}
    gate: test -s "$OUT"
    retries: 2
    retry_on: [model_error, gate_failed, schema_failed]
    retry_delay_seconds: 1
    retry_backoff: exponential
  - id: copy
    needs: [extract]                   # parallel with plan
    prompt: |
      Write landing copy from {step.extract}
    judge:                             # generate -> score -> feedback -> regenerate
      model: openai-codex/gpt-5.6-terra
      prompt: |
        Score 0-10 vs <rubric>. Output JSON: {"score": N, "feedback": "..."}
        <candidate>{out}</candidate>
      score: 8.5
      max_iters: 3
      keep_best: false
  - id: implement
    agent: true                        # full agent loop: pi default tools + repo AGENTS.md
    needs: [plan, copy]
    prompt: |
      Implement the plan in {run}/plan.md.
    gate: npm test                     # gate checks the EFFECT, never the transcript
```

Mechanics (the load-bearing rules):

- **One pi call per step** (`--mode json`, isolated: no session/project trust/extensions/skills/context files or startup refresh), model + thinking pinned per step. Every event line must parse, the requested provider/model must match, and `agent_settled` must follow the final successful assistant message before its text becomes the artifact.
- **Four execution runtimes, weakest that works:** `cmd:` = code (zero variance) · `prompt:` = one isolated completion · `prompt:` + `tools: "read,bash"` = an allowlisted Pi completion · `prompt:` + `agent: true` = a full tool loop with repo context, gated on the produced effect. Another harness = `cmd: codex exec "..."`.
- **DAG execution:** deps = `needs: [ids]` ∪ `{step.x}` refs; no `needs` key → implicit dep on the previous listed step (linear yamls stay valid). A `cmd:` reading `$RUN/<id>.md` MUST declare it in `needs`. Failed step skips only its descendants. Forward refs rejected at load.
- **Inputs and placeholders:** `piw run x --input ...` or `--input-file ...` creates immutable `input.txt` inside that run. `{input}` inserts it as explicitly untrusted data; command steps receive `$INPUT`. `{step.<id>}` inlines an artifact; `{prev}` is the previous listed step; `{run}` is the run directory.
- **Gates decide, never the model.** `bash -c` with `$OUT/$RUN/$STEP`; exit 0 advances. Count matches with `grep -oE .. | wc -l` (`grep -c` counts lines). Prefer gates over judges whenever the check fits in a command.
- **Judge loop:** score threshold + bounded `max_iters`, judge feedback fed into regeneration, failed attempts snapshotted as `<id>.aN.md` (diffable evidence). `keep_best: true` retains the top candidate below target. Best-of-N = unreachable `score` + `keep_best`. Gate is the floor and runs first; judge pass never overrides a failed gate.
- **Classified retries:** failures are recorded as `command_exit`, `model_error`, `gate_failed`, `schema_failed`, or `judge_below_target`. Narrow with `retry_on`; pace transient retries with bounded fixed/exponential delay and replay-stable `retry_jitter`. Omit these fields to preserve immediate all-class v1 retries.
- **Document-quality loops: review → patch → apply** (see the template) — a reviewer names defects and their owning node, a patch step regenerates ONLY flagged sections, deterministic code merges with a no-shrink keep-or-revert guard and emits `changes.diff` + `changelog.md` (applied / rejected per finding). Never pay for whole-document rewrites per iteration.
- **Cache + surgical regen:** passing prompt-step outputs cached by content hash in `<yaml-dir>/cache/`; hit = model + judge skipped, gate re-runs. Upstream changes invalidate downstream automatically; `--regen <id>` forces one node fresh. `--no-cache` for paired experiments.
- **Iteration history (git):** every run dir is a git repo; the runner commits after each step (`<id>: PASS/FAIL`), after QA (`QA: pass/fail`), and commits any operator hand-edits before `--verify`. `git -C runs/<dir> log --stat` = the changelog between iterations; `git diff HEAD~1` = what the last cycle changed. Document loops additionally emit `changes.diff` + `changelog.md` (applied/rejected per finding) and failed judged attempts persist as `<id>.aN.md`.
- **Cost ledger:** every run writes `ledger.json` + a log table — seconds, tokens, real dollars per step. Read it after every run; the most expensive node is the next optimization target.
- **QA + verify:** `qa:` runs after the last step and on `--verify` (mechanical re-checks first, fail-fast; then the QA judge over `{artifacts}`). Implementer model is never sole approver. For an action-built workflow, QA evaluates that action's declared input/output/failure contract; it must not invent a downstream requirement such as implementing the proposal that was only reviewed.
- **Human checkpoint = 2-line step:** `cmd: test -f approvals/<name>.ok` with `retries: 0` — halts with a resume command; operator inspects artifacts, touches the file, resumes. LLM judges are smell tests; the human at the boundary is the gate of record.
- **System prompts:** top-level `system:` for writing-chain hygiene (steps only; judges/QA never inherit; `system: ""` opts a step out). Writing nodes want cheap models + low/off thinking; reasoning nodes (verdicts, deciders) want thinking high.

## Stage 3 — Test and iterate

1. **Smoke one unit.** Run the yaml on one real input. Read `log.md`, the ledger, and every artifact. A gate that never fails is a gate that checks nothing — try to make each one fail once.
2. **Fix generally, not locally.** Every failure the gates catch becomes a *general* prompt/gate fix in the yaml (never a hardcoded patch for one input). Tonight's rule: gate finding → template fix → every future run inherits it.
3. **Batch it:** canary first with `piw batch steps.yaml --inputs corpus.jsonl --limit 5 --require-all --output-step result`; then launch the full corpus with `--parallel N --require-all --stop-after-failures N --max-tokens N --max-cost N --output-step result --detach --json`. Poll the returned `piw batch-status <dir> --json` command. Every item has an immutable input and isolated attempts; the frozen graph/corpus receipt makes `--resume <dir>` reject drift and rerun only failed or unfinished items. `outputs.jsonl` preserves corpus order and exact cardinality, including explicit failure/not-run rows. Token/cost limits are recorded-usage dispatch ceilings, not provider-side hard caps: active items drain and any overshoot is receipted. This is the “run these exact steps 1,000 times” mode; exit 0 only when every item has a complete execution contract and passes.
4. **Pick the model with data:** `scripts/eval_models.py steps.yaml --inputs corpus.jsonl --input-file idea.md --models luna,sol,...` — swaps only the top-level default (judges stay fixed), `--no-cache`, paired inputs → `eval-report.md`: pass rate, QA rate, judge scores, tokens, cost, wall per model. Choose on evidence, not vibes.
5. **Open the Studio when a human drives it:** `piw ui steps.yaml --input-file idea.md` — the optional local graph, node inspector, immutable input editor, live flight recorder, final artifact, and cost hotspot view all use the same canonical runner. `--output <step-id>` picks the result artifact; the UI never owns workflow semantics.
6. **Optimize one node at a time:** inspect `piw detail <workflow> <run> --step <id> --io`; the QA/judge is often the token hotspot. Configure the node with `piw set` (including `--model`, `--thinking`, prompt, gate, and `--judge-*`), rerun it fresh with `piw run --node <id>`, then use `piw compare <workflow> <baseline> <candidate>`. Start bounded extraction, formatting, and independent review lanes on Luna low. Keep synthesis, routing verdicts, and QA at medium until paired no-cache runs prove they can move down. Freeze a 2-3 item corpus, change one setting, compare pass/QA quality plus tokens, cost, and wall time, then repeat on a fresh holdout. Keep only consistent wins; token savings alone do not justify a candidate that loses on cost, latency, or quality.
7. **Hill-climb prompts per `$improvement`:** freeze the judge, gates, and corpus; mutate prompts or `system:` one mechanism at a time; use paired no-cache runs and keep-or-revert with the run artifacts as evidence. Never tune the evaluator and candidate together.
8. **Automate only after proof:** `piw schedule <workflow> --interval-minutes N` or `--daily HH:MM`. Inspect with `piw automations`; pause, resume, run, or delete with `piw automation <action> <id>`.

## Heavy route: production workflow factory (opt-in only)

Only when the user explicitly asks for a **production, at-scale mutation workflow** with digest-bound approvals, certification fixtures, replay comparison, and version promotion: `scripts/draft_workflow_blueprint.py` → `compile_workflow.py` → `certify_workflow.py`, contracts in `references/workflow-factory.md` (also: conditional control-flow graphs, `references/peer-collaboration.md`, `references/product-planning-harness.md`). Never route a normal "don't skip steps" request through the factory.
