# Evaluating actions and complete runs

Studio's **Evaluations** section reads the selected run's frozen contract and
recorded evidence. It does not invent a quality score from execution status.
Select an action row to open its output, failure, and judge-attempt evidence.

## 1. Define an action contract

Use a command gate for facts that code can check and a schema for output shape:

```yaml
- id: calculate
  cmd: python3 calculate.py
  gate: python3 check_calculation.py "$OUT"
  schema:
    answer: number
```

The runner executes the action, then its gate, then its schema. A failure stops
that action's descendants; independent branches may finish. Gates must check the
actual claim, not merely that some output exists.

For a whole-run deterministic check, add a final command node whose explicit
`needs` includes the required outputs. Compare the final artifact against the
acceptance criteria and gate the verification result. A final-node pass verifies
only those written criteria, not general semantic quality.

## 2. Add semantic evaluations where needed

An action's `judge` evaluates that action's output. Top-level `qa` evaluates the
completed run's artifacts. Both make additional model calls. See the existing
`11-parallel-analysis-qa` example for a complete run-level QA configuration, or
`10-judged-checklist` for a bounded per-action judge.

Before live evaluation, freeze the cases, references, rubric, model, thresholds,
attempt and cost/time budgets, and stop rules. Calibrate a semantic rubric against
a human-labeled representative set and record agreement. A displayed model score
is a recorded opinion; its presence does not establish calibration or independence.
Use a separate evaluator from the generator and preserve criterion-level evidence.

Start with one representative case. Inspect its actual output and evaluator
response before a batch. Keep development cases and held-out cases separate.
Do not change the evaluator while comparing candidates.

## 3. Run and inspect

```bash
./bin/piw validate path/to/steps.yaml --strict --json
./bin/piw run path/to/steps.yaml --input-file case.txt --json
./bin/piw detail path/to/steps.yaml RUN_ID --json
./bin/piw detail path/to/steps.yaml RUN_ID --step ACTION_ID --io --json
./bin/piw compare path/to/steps.yaml BASELINE_RUN CANDIDATE_RUN --json
./bin/piw ui path/to/steps.yaml
```

For fresh model-policy comparisons, `piw eval --help` describes the paired-input
runner. It disables caching for comparison. A green average must not hide failing
cases; inspect per-case regressions and the held-out result before promotion.

## Reading the evaluation display

- **Passed / failed:** supported by the relevant recorded boundary or score.
- **Not evaluated:** the configured check was not reached, its result is absent or
  malformed, or no fresh evaluation was recorded for a cached output.
- **Skipped:** the action did not execute; excluded from applicable-check totals.
- **Not configured:** no such evaluator exists; excluded from check totals.
- **Overall run review:** only the exact recorded QA verdict determines this state.
  A successful execution does not substitute for missing QA.

Current cache behavior: command gates rerun on prompt-cache hits; schemas and
model judges do not. Studio does not claim they ran again. Inspect the original
run or regenerate with `piw run ... --node ACTION_ID` when a fresh check is needed.
The inspector retains raw evaluator output so malformed reports can be diagnosed.

The operations example demonstrates real gated execution and a composed incident
packet. Its prose was reviewed by the active coding agent, not by a calibrated
independent judge; its absent judge and QA checks remain visible as unconfigured.
