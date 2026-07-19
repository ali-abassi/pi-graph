# Pi Capability Map for Deterministic Workflows

Reviewed against Pi `0.80.7` on 2026-07-15, re-verified against Pi `0.80.10` on
2026-07-19. The supported product baseline is Pi `0.80.10`; the JSON transport
accepts newer versions but rejects malformed, incomplete, unsettled, or
model-drifted event streams. Re-run compatibility fixtures when Pi changes its
event contract.

The 2026-07-18 re-verification ran a real `--mode json` stage (session header
→ `agent_start` → `turn_start` → message lifecycle → `turn_end` → `agent_end`
→ `agent_settled`, final `stopReason: "stop"`) against the installed `0.80.10`
binary with the exact isolation flags `bounded_pi_json_flags()` emits. The
event sequence and stop-reason semantics this document depends on are
unchanged from the `0.80.7` baseline. This was a live protocol check, not a
version-number bump on trust — re-verify the same way before extending the
range further.

## Choose the host surface deliberately

| Workflow need | Pi surface | Factory rule |
|---|---|---|
| Bounded, isolated stage | JSON mode | Default. One process, one stage, JSONL events, no session. |
| Long-lived control, steering, abort, UI requests | RPC | Build a separate host profile; completion arrives in events, not the command-acceptance response. |
| Typed Node/TypeScript session orchestration | SDK | Use `AgentSessionRuntime`; keep harness checkpoints as the source of truth. |
| Tool policy or structured submission | Extension | Use typed tools and fail-safe `tool_call` gates; extension code has full user privileges. |
| Focused peers that must exchange questions and evidence | Extension + RPC/SDK host | Compile a schema `1.2` peer contract; keep correlation, budgets, receipts, cancellation, and completion in the controller. |

JSON mode exposes session, agent, turn, message, tool, compaction, queue, and
automatic-retry events. The runner must treat malformed JSONL, missing terminal
events, and guard initialization failure as protocol failures rather than
activity noise.

Across the reviewed `0.80.5`-`0.80.7` range, JSON print mode can exit normally
even when the final assistant message reports an error. Inspect stop reasons
and retry/extension evidence.
`agent_settled`, which means no automatic retry, compaction retry, or queued
continuation remains, is required not only for RPC hosts but also for bounded
JSON-mode stages: the factory's own event-stream validator rejects a stage
whose terminal event (the final assistant message, or the `submit_stage_result`
tool call for stages that use it) is not followed by `agent_settled`. A stage
that submits its result through `submit_stage_result` legitimately ends with
stopReason `toolUse` rather than `stop`; accept `toolUse` only for that
structured-result submission path, never as a general substitute for `stop`.

An RPC host must split frames only on LF (optionally stripping CR), validate
every command and response, serialize commands that can race, and assign its own
run/session epoch because events carry no prompt correlation ID. Register the
completion waiter before sending the prompt; the prompt response means accepted,
not completed. Accept a run only when that epoch produces both a successful
terminal assistant message and `agent_settled`, with no disallowed extension or
retry evidence. Bound event buffers and UI requests, and make session replacement
an explicit state transition rather than an incidental command.

An SDK host shares a process and privilege boundary with Pi. Treat extension
load errors as fatal, constrain the resource loader and auth/model/settings
managers, and supervise the SDK in a separate worker process when hard
cancellation or crash isolation is required. Runtime replacement tears down the
old session first, so a failed replacement must enter a terminal recovery state.

## What Pi supplies

- Model/provider routing, thinking levels, model registry metadata, and usage.
- Streamed tool and lifecycle events.
- Ephemeral or persisted session trees, compaction, retry, and queue behavior.
- Extensions with typed tools, event interception, subprocess helpers, and
  terminating structured submissions.
- Enough extension, message, RPC, and lifecycle primitives to implement local
  or networked `list`/`send`/`get`/`await` peer communication.
- Project or global packages containing skills, extensions, prompts, and themes.

## What the harness must supply

- The objective contract, ordered state machine, budgets, and stop conditions.
- Immutable inputs, stage artifacts, checkpoints, idempotency, and resume rules.
- Approval binding and side-effect isolation.
- Mechanical outcome verification and criterion-to-evidence provenance.
- Append-only run evidence, redaction, retention, trackers, and seals.
- Authenticated peer identity, durable message receipts, correlation, quorum,
  hop/message/deadline budgets, cancellation, and response validation.
- OS/container isolation for untrusted work. Pi hooks and project trust are not
  a sandbox.

## Bounded-stage runtime profile

The lean runner disables project trust, extension discovery, skill discovery,
context files for isolated nodes, prompt templates, themes, sessions, and
startup network operations. It retains the selected user Pi directory for
authentication and model configuration, then verifies the requested
provider/model in the final event. The production factory additionally creates
a sanitized agent directory, explicitly loads only its skill and guards, and
records the effective Pi version in the implementation digest.

Do not depend on user or project `.pi/settings.json`, `SYSTEM.md`, discovered
skills, or global package state for correctness. If a resource is required,
declare and hash it as part of the compiled bundle.

Discovery-disable flags do not suppress global `settings.json` or `models.json`.
The Python runner therefore uses a sanitized agent directory with pinned retry
and compaction policy and no global model overrides. A TypeScript SDK host should
prefer in-memory settings and an explicit model/resource registry.

For new specialized stage hosts, use the bundled TypeBox
`submit_stage_result` tool. It validates a stage-specific result schema, marks the
submission, and returns `terminate: true`; the external host still requires
exactly one successful call plus a valid terminal event sequence. Pi may omit or
duplicate a call, so terminating tools do not replace external outcome
verification.

## Capability boundaries

Pi does not itself guarantee permissions, sandboxing, deterministic model
outputs, durable workflow checkpoints, idempotent external actions, calibrated
judges, or truthful completion. Those are harness responsibilities.

Pi also does not make every workflow an agent. Prefer a fixed workflow when the
steps and gates are known. Use a dynamic agent loop only when the model must
choose its own path and a bounded stop condition plus outcome verifier exists.

## Primary sources

- [JSON mode](https://pi.dev/docs/latest/json)
- [RPC mode](https://pi.dev/docs/latest/rpc)
- [SDK](https://pi.dev/docs/latest/sdk)
- [Extensions](https://pi.dev/docs/latest/extensions)
- [Sessions](https://pi.dev/docs/latest/sessions)
- [Session format](https://pi.dev/docs/latest/session-format)
- [Models](https://pi.dev/docs/latest/models)
- [Settings](https://pi.dev/docs/latest/settings)
- [Packages](https://pi.dev/docs/latest/packages)
- [Security](https://pi.dev/docs/latest/security)
- [Pinned Pi 0.80.10 coding-agent source](https://github.com/earendil-works/pi/tree/v0.80.10/packages/coding-agent)
