# Pi Workflows product contract

Pi Workflows turns an agent-authored, versioned `steps.yaml` graph into repeatable work:
the same declared nodes, dependencies, routes, gates, budgets, and approval
boundaries execute in the same order while model output remains honestly
probabilistic.

## Users

- A coding agent creates, validates, runs, inspects, and improves a workflow.
- An operator understands the graph, configuration, live state, artifacts,
  cost, and failure without reading implementation code.
- Agent X can invoke one workflow during a task or schedule it as a durable
  automation through Loops.
- An agent can freeze one graph and run it over hundreds or thousands of inputs
  without owning the queue or remembering which items finished.

## Product surfaces

1. `steps.yaml` — portable authored contract.
2. `schemas/workflow.schema.json` — complete node, field, runtime-input, and
   failure contract for agents and editors, exposed by `piw schema --json`.
3. `piw` — stable human and machine-readable CLI. Every inspection command has
   `--json`; failures use non-zero exit codes and actionable errors.
4. Reusable actions — versioned input/output/failure plus effect, retry-safety,
   idempotency, and cost contracts that expand at authoring time into ordinary
   inspectable v1 nodes; no hidden action runtime.
5. Bulk controller — a frozen graph/corpus contract, bounded item queue,
   isolated attempts, resumability, detached execution, status receipts, and
   fail-closed aggregate completion over the canonical runner.
6. Studio — an optional localhost graph, node inspector, run control, evidence
   stream, artifact view, and cost hotspot surface over the canonical runner.
   It is launched with `piw ui` and owns no workflow semantics.
7. Pi package — a native Pi tool and skill registered from the product install.
   The product installer remains the distribution path because it also creates
   the required isolated Python runtime; `pi install` alone is not a complete
   Pi Workflows installation.
8. Codex and Claude Code skills — one shared `SKILL.md`, discovered from their
   documented user skill locations.
9. Loops adapter — shared graph/configuration display, live events, run
   inspection, and durable schedules. Loops owns scheduling; Pi Workflows owns
   workflow semantics.

## Definition of excellent

- A fresh install can create, validate, run, inspect, and schedule a workflow.
- Concurrent runs have isolated inputs and event streams.
- Common graph fragments can be discovered, inspected, expanded, edited, and
  validated without inventing their failure and evidence contracts again.
- A 1,000-item batch can run detached, expose compact progress, stop on a
  configured failure ceiling, resume only unfinished items, and prove every
  item reached a terminal state for every declared node.
- Static validation catches malformed DAGs, impossible routes, output-contract
  mismatches, unsafe missing gates, and invalid model/tool configuration before
  a paid call.
- Run evidence includes per-node state, attempts, resolved input, output,
  verifier result, tokens, cost, time, cache behavior, and final QA.
- Retries classify the failure, honor declared eligibility, and record bounded
  replay-stable pacing rather than blindly repeating permanent errors.
- Agents receive the smallest useful command/tool result and can branch on
  structured status instead of parsing decorative prose.
- The graph UI makes agent configuration legible: runtime kind, model,
  reasoning, tools, dependencies, route, retries, timeout, output contract,
  gate, judge, artifacts, and latest result.
- Automations are workflows plus an explicit trigger, stop policy, workspace,
  timeout, and recent-run evidence—not a second execution engine.

## Non-goals

- Pretending model generations are deterministic.
- Replacing ordinary scripts for tasks that do not benefit from a graph.
- Letting the visual editor become a second source of workflow truth.
- Requiring Agent X, Loops, Codex, or Claude Code for core execution.
