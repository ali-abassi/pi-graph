# Workflow builder research brief

## Decision

Decide which proven workflow-runtime and agent-orchestration ideas should become
Pi Workflows product primitives or reusable action templates, without bloating
the core node vocabulary or weakening its code-owned control-flow boundary.

## Scope

- Current open-source and commercial workflow engines with directly inspectable
  documentation or source: durable execution, dataflow/DAG engines, automation
  builders, and agent graph runtimes.
- Primary academic literature on agent orchestration, workflow planning,
  reliability, evaluation, cost/latency optimization, and failure recovery.
- The existing Pi Workflows schema, runner, Studio, batch controller, examples,
  and AgentX/Loops integration.

## Exclusions

- Copying vendor-specific connector catalogs wholesale.
- Turning the optional Studio into a second source of workflow truth.
- Advertising a dynamic node before its identity, retry, cancellation, budget,
  evidence, and recovery semantics are mechanically enforced.
- Treating LLM output as deterministic or academic benchmarks as product proof.

## Coverage matrix

| Question | Required evidence | Preferred sources | Freshness | Status |
|---|---|---|---|---|
| Which runtime semantics most improve reliability at scale? | Exact retry, replay, idempotency, durability, and concurrency contracts | Official docs/source | Current | verified |
| Which graph dynamics are broadly useful for agent work? | Map/fan-out, joins, routing, subflows, waits, compensation, human gates | Official docs/source | Current | verified |
| Which reusable action nodes remove repeated authoring? | Common templates plus input/output/failure contracts | Product docs/source | Current | verified |
| What does research say about multi-agent gains and failure modes? | Primary papers with tasks, methods, and limitations | arXiv/conference/publisher | 2023-2026 | verified |
| What improves cost, latency, and token efficiency? | Empirical or formal scheduling/routing/caching evidence | Papers and runtime docs | 2023-2026 | verified |
| What should Pi Workflows implement now versus defer? | Local gap analysis, complexity, tests, and compatibility | Local code plus verified findings | Current | verified |

## Competing hypotheses

1. A large node catalog improves usability more than a small compositional core.
2. Reusable templates can deliver most of that usability without expanding
   runtime semantics.
3. Dynamic map/subworkflow primitives are worth first-class support only when
   stable item identity, bounded concurrency, replay, and fan-in ordering are
   enforced.
4. Multi-agent fan-out improves quality by default.
5. Agent diversity helps only on decomposable tasks and can lose to one strong
   agent when coordination, shared context, or verification is weak.

## Done when

- At least four distinct workflow-engine families and six primary research
  papers are directly verified.
- Every material recommendation has claim-level evidence and counterevidence.
- Two focused follow-up rounds add no new high-priority primitive.
- The evidence ledger passes the deep-research auditor.
- The highest-value low-risk improvements are implemented, documented, and
  covered by focused tests and clean-install verification.
