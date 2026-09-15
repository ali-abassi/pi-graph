# Operations brief → incident response

Two composable workflows, 18 nodes total. The six-service input is synthetic;
execution, model calls, artifacts, and persisted run history are real.

## Run from the repository root

```bash
workflow=examples/workflows/16-operations-brief
./bin/piw validate "$workflow/steps.yaml" --strict --json
./bin/piw run "$workflow/steps.yaml" --input-file "$workflow/input.txt" --json
./bin/piw ui "$workflow/steps.yaml" --input-file "$workflow/input.txt"
```

The first graph normalizes and validates JSONL, computes revenue/reliability/support
in parallel, joins facts, routes escalation at a 2% aggregate error rate, runs two
isolated Luna completions in parallel, then writes `report.html`. Click **Fit** to
see the graph; select a node and **Evidence** to inspect its output. Open the HTML
file in the returned `run_dir` to read the report.

It makes at most two workflow model invocations, with no model-node retries and a
120-second timeout per call. Pi/provider internal retries and billing are outside
that invocation limit. Authentication must already be configured. The gates verify
shape, references, arithmetic boundaries, and file integrity; they do not certify
AI prose. Review prose against the facts before using it. The first tested candidate
confused aggregate and service totals; explicit per-service metrics fixed the
observed attribution error. This is evidence from one corrected case, not a
calibrated semantic evaluation.

## Compose the second workflow

Use the exact `run_dir` returned above:

```bash
run_dir=/absolute/path/from/the/run/result
./bin/piw detail "$workflow/steps.yaml" "$(basename "$run_dir")" --json
./bin/piw detail "$workflow/steps.yaml" "$(basename "$run_dir")" --step summary --io --json
./bin/piw validate "$workflow/incident.yaml" --strict --json
./bin/piw run "$workflow/incident.yaml" --input-file "$run_dir/facts.md" --json
./bin/piw ui "$workflow/incident.yaml" --input-file "$run_dir/facts.md" --output packet
```

The second graph costs no model tokens. It reads the computed facts, sets triage
priority, prepares response steps, unassigned operating roles, and communication
requirements in parallel, and joins a JSON packet. The packet is a proposed plan;
it does not assign people, mitigate a service, or send messages.

## Adapt it as Codex

1. Discover existing fragments with `./bin/piw actions --json` and field definitions
   with `./bin/piw schema --json`. This example reuses `canonicalize-jsonl`.
2. Edit the ordinary YAML and adjacent Python stages. Explicit `needs` owns ordering;
   only `prompt` nodes call a model. Keep external effects out until separately scoped.
3. Keep aggregate totals and per-entity values explicitly named. Pass only the small
   computed facts packet to models, not raw input or an entire transcript.
4. Strictly validate before running. Inspect the returned run, model inputs/outputs,
   trace, and ledger. A green node proves its declared gate, not general correctness.
5. Correct invalid input by starting a new run: saved inputs are immutable. For an
   interrupted run, use `piw resume WORKFLOW RUN_ID --json`.
6. Repeat unchanged input to reuse cached AI text while recomputing command stages
   and rechecking gates. Use `--node summary` to regenerate that model step explicitly.

No global installation update, new database, worker service, or hosted backend is
required. Run-directory snapshots freeze YAML and input; adjacent helper files are
not a deployment sandbox. Keep this directory at a reviewed Git revision for replay.
