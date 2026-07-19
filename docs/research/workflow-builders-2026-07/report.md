# Deterministic workflow builders and agent orchestration

Research date: 2026-07-19  
Decision: ship transparent reusable actions and classified retry pacing now;
defer native dynamic map, subworkflow, wait, and compensation nodes until their
replay contracts are enforceable.

## Executive finding

Pi Workflows already had the right architectural center: a small execution
vocabulary, code-owned graph transitions, typed routes, immutable per-run
inputs, per-step gates, resumable batch receipts, and evidence-led evaluation.
The largest usability gap was not another runtime. It was the absence of tested,
reusable graph fragments. The largest core reliability gap was that retries had
only an attempt count and timeout, so permanent failures and transient failures
were treated identically and retried immediately.

The market and research converge on six durable ideas:

1. Persist execution state and make effects idempotent; replay is not
   exactly-once execution.
2. Give every mapped item stable identity, bounded concurrency, an explicit
   failure/cardinality policy, and an ordered receipt.
3. Keep graph order separate from the context each model receives.
4. Parallelize only dependency-independent work; join and termination semantics
   must stay explicit.
5. Treat multi-agent fan-out as a measured candidate, not a default upgrade.
6. Optimize from traces by compiling repeated successful patterns into
   deterministic actions and routing work to models using eval evidence.

## What other builders get right

| Family | Strong idea worth adopting | Important warning |
|---|---|---|
| Durable execution: Temporal, AWS Durable Execution | Event history, replay, durable waits, activity boundaries, stable idempotency keys | A retry is at-least-once unless the effect itself deduplicates |
| Data/task orchestrators: Prefect, Dagster | Typed tasks, result caching, transactional publication, retry conditions/backoff, lineage | Too much granularity adds orchestration overhead and authoring burden |
| Event workflow engines: Inngest, n8n, Dify | Per-step memoization, keyed concurrency, subflows, failure branches, per-item continuation policies | Nested retries can multiply; dropping failed map outputs changes output cardinality |
| Agent graphs: LangGraph, AutoGen, CrewAI, Flowise | Checkpointed interrupts, graph/message separation, typed state, explicit loop ceilings, human review | Stateful subgraphs and shared transcripts can leak context or collide without isolation |

These are not copied as vendor-specific nodes. They inform portable contracts.
For example, Prefect exposes delay, jitter, and retry conditions; Inngest warns
that parent and child retry budgets multiply; AWS explicitly rejects the
“retries equal exactly-once” assumption. Pi Workflows now classifies failures,
lets authors declare eligible classes, and records bounded replay-stable pacing.

## What the research changes

Parallel DAG compilation can materially reduce latency and cost on decomposable
tool tasks: LLMCompiler reports up to 3.7x lower latency and 6.7x cost savings on
its evaluated workloads. But this does not justify indiscriminate fan-out.
Mixture-of-Agents reports ensemble gains, while Self-MoA shows that repeated
samples from one stronger model often beat mixed-model diversity. A human-
annotated study of multi-agent systems identifies specification, alignment,
verification, and termination failures across five frameworks and more than
150 tasks.

The product implication is conservative and useful: parallel reviewers should
have non-overlapping roles, filtered inputs, typed synthesis, a termination
budget, and an independent mechanical boundary. The shipped `parallel-review`
action follows exactly that structure. It does not claim that three model calls
are always better than one; it makes the candidate reusable and measurable.

Workflow optimization papers support keeping the graph inspectable. AFlow
searches code-represented workflow candidates using execution feedback;
GPTSwarm optimizes node prompts and graph edges. RouteLLM and FrugalGPT show
that model choice should be measured as a cost-quality routing problem. The
recent Agent Workflow Optimization paper goes one step further: recurring tool
sequences become deterministic composite “meta-tools,” reducing both model
turns and failure surface. That is the research justification for Pi Workflows
actions being authoring-time expansions rather than opaque new runtimes.

## Shipped decision

### Reusable action catalog

`piw actions` lists versioned action contracts. `piw create --action` starts a
workflow from one. `piw add` prefixes and expands a fragment into the canonical
`steps.yaml`, rewrites its internal dependencies, attaches declared upstream
steps, validates the candidate, and writes atomically. No action indirection is
present at runtime.

The initial catalog covers:

- canonical JSONL validation and normalization;
- typed work classification;
- typed action/risk extraction;
- parallel correctness and failure-mode review;
- bounded judge/refine;
- claim plus skeptic evidence synthesis;
- plan/implement/Git-diff verification for repository changes;
- the exact five-stage item pipeline used by `piw batch`.

### Second research pass: workflow fleets and reusable actions

A broader comparison of Temporal, Inngest, Prefect, Dagster, Windmill, n8n,
LangGraph, Restate, and Hatchet sharpened the next boundary. The shared idea is
not a larger canvas vocabulary. It is a durable work identity:
`(workflow revision, corpus digest, item key, node id, attempt)` plus a committed
result or external-effect receipt. Keyed concurrency then serializes work that
targets the same tenant, repository, account, or API resource without blocking
unrelated keys.

Three recovery operations should remain distinct:

1. resume unfinished work against the pinned workflow revision;
2. retry only failed items or nodes with the same pinned contract;
3. fork from a checkpoint under a newer revision and record the lineage.

The current batch controller already covers the first two at item granularity.
Checkpoint forking, shared resource pools, and durable cross-process leases stay
deferred until their persistence and stale-worker contracts are implemented.
The Studio should eventually render one logical batch node with an item table,
not clone 1,000 graph nodes.

The academic pass reinforced four product rules. Classical validation should
compile and reject impossible plans before model execution; final effects must
be mechanically verified; each node should receive only declared inputs rather
than an ever-growing transcript; and semantic repair should be grounded in a
schema error, test result, or other external evidence. Multi-agent conversation
is not itself a reliability feature.

That evidence produced four new authoring-time actions without expanding the
runtime vocabulary:

- `handoff-contract` prevents objective, constraint, and acceptance-test loss
  at agent boundaries;
- `batch-readiness` independently reviews correctness and scale hazards before
  a large corpus launch;
- `failure-triage` separates observed evidence from causal hypotheses and marks
  whether retrying is safe;
- `red-team-repair` ties defects to evidence, performs a bounded repair, and
  independently verifies the result.

Every catalog action now declares its effect class, retry safety, idempotency
expectation, and model-cost shape. These are inspection contracts today. They
should become static validator inputs before a future effectful action catalog
is allowed to grow.

### Classified retries

Every failed attempt now records one of `command_exit`, `model_error`,
`gate_failed`, `schema_failed`, or `judge_below_target`. `retry_on` controls
eligibility. `retry_delay_seconds`, fixed/exponential backoff,
`retry_max_delay_seconds`, and deterministic fractional jitter control pacing.
The default remains backward compatible: all classes, immediate retry.

## Deliberately deferred

| Candidate | Why it is not a v1 node yet | Contract required first |
|---|---|---|
| Native dynamic map | Batch already covers the primary 1,000-item use case without runtime graph mutation | item identity, ordered fan-in, concurrency/spend ceilings, per-item failure/cardinality policy |
| Subworkflow | Child retries, state, and cancellation can multiply or leak into a parent | pinned child digest, mapped I/O, child run id, recursion/retry ceiling, failure propagation |
| Durable human/external wait | A filesystem flag is not a durable distributed approval protocol | request id, artifact digest, approver, timeout, resume token, deduplication, cancellation |
| Compensation | Generic rollback is unsafe for irreversible or partially applied effects | effect-specific idempotency key, receipt, reverse order, retry policy, human escalation |
| Automatic graph optimizer | Optimizer scores can reward-hack weak gates | frozen eval corpus, fixed gates/judges, versioned candidates, held-out promotion check |

## Research saturation and counterevidence

The first follow-up round focused on concrete builder semantics: keyed
concurrency, iteration cardinality, subflow retry composition, interrupts, and
human waits. It added retry classification/pacing and strengthened the deferral
contract for dynamic map/subworkflow; it did not add another runtime priority.

The second follow-up round focused on counterevidence and efficiency: Self-MoA,
multi-agent failure taxonomies, model routing/cascades, and deterministic
meta-tools. It confirmed actions, filtered parallel review, and trace-led model
routing as the highest-value direction; it added no new high-priority primitive.

The claim-level ledger is [`evidence.jsonl`](evidence.jsonl). It is audited with:

```bash
python3 /Users/aliabassi/.codex/skills/deep-research/scripts/audit_evidence.py \
  docs/research/workflow-builders-2026-07/evidence.jsonl --require-primary
```

## Primary sources

- [Temporal history service architecture](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md)
- [AWS durable execution idempotency guidance](https://docs.aws.amazon.com/durable-execution/patterns/best-practices/idempotency/)
- [Prefect tasks and retry policies](https://docs.prefect.io/v3/concepts/tasks)
- [Inngest concurrency](https://www.inngest.com/docs/guides/concurrency) and [child invocation retry composition](https://www.inngest.com/docs/reference/typescript/v3/functions/step-invoke)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [AutoGen GraphFlow](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/graph-flow.html)
- [Flowise Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2)
- [LLMCompiler](https://arxiv.org/abs/2312.04511)
- [AFlow](https://arxiv.org/abs/2410.10762)
- [Language Agents as Optimizable Graphs](https://arxiv.org/abs/2402.16823)
- [Mixture-of-Agents](https://arxiv.org/abs/2406.04692) and [Self-MoA counterevidence](https://arxiv.org/abs/2502.00674)
- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)
- [RouteLLM](https://arxiv.org/abs/2406.18665) and [FrugalGPT](https://arxiv.org/abs/2305.05176)
- [Agent Workflow Optimization](https://arxiv.org/abs/2601.22037)
