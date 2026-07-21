# Integration contract

## Pi

Pi Graph is a Pi package with one declared extension and one skill. The
extension registers `pi_graph`; it never overrides a built-in tool. It uses
Pi's `exec` API, forwards cancellation, throws on non-zero CLI exit, and returns
bounded text plus structured command details.

The native tool can canary or detach a large `batch` corpus. `batch-status`
returns compact progress without holding a model turn open, and `batch-cancel`
stops the detached controller process group.

This follows Pi's documented package contract: resources are declared under the
`pi` manifest, Pi-owned imports are `peerDependencies` with `"*"`, and custom
tool output is truncated with Pi's own helpers. Pi packages execute with the
user's permissions, so the workflow tool allowlist is capability routing rather
than an operating-system sandbox.

Run `./install.sh` to install the complete product and register its local Pi
package. A direct `pi install` can discover the TypeScript adapter, but it does
not provision the Python virtual environment and is therefore not the supported
standalone installation path.

## Custom agent harnesses

A harness with its own terminal rendering may expose a compact `workflows` tool
of its own, but it must invoke the same `piw` CLI. No integration may contain a
second runner or a second workflow schema.

## Codex

The installer links this repository into `~/.agents/skills/pi-graph`.
Codex loads `SKILL.md` progressively and invokes `piw`; the workflow remains
portable because no Codex-only prompt or session state is embedded in YAML.

## Claude Code

The installer links this repository into `~/.claude/skills/pi-graph`.
Claude Code follows supported skill-directory symlinks. Deterministic lifecycle
hooks remain optional; workflow control flow stays in the runner, not in a hook.

## Scheduler adapter (optional, not bundled)

`piw schedule`, `piw automations`, and `piw automation` delegate to an external
scheduler binary on `PATH`. No such scheduler ships with this repository, so
those three commands are inert on a stock install and report that the adapter
is missing; every other command, including `piw batch`, works without one.

An adapter is expected to provide a localhost API, a graph canvas, live run
events, and durable triggers. It resolves the installed runner and returns the
event path for a started run. pi graph falls back to a direct run when the
adapter is absent or resolves a different workflow path.

## Compatibility

- Minimum and tested Pi version: `0.80.10`. Newer versions are accepted because
  the JSON parser fails closed if the event contract becomes incompatible.
- Model calls use Pi JSON mode with sessions, project trust, ambient resources,
  and startup refresh disabled. Every non-empty JSONL line must parse, the
  requested provider/model must match, and `agent_settled` must follow the final
  successful assistant message.
- Python: 3.11 or newer, with PyYAML 6.x, ruamel.yaml 0.18.x, and
  jsonschema 4.x for the public workflow contract boundary.
- Workflow YAML is the cross-agent API. CLI JSON is the automation API.

## Primary Pi sources

- [Pi packages](https://pi.dev/docs/latest/packages)
- [Skills](https://pi.dev/docs/latest/skills)
- [Extensions](https://pi.dev/docs/latest/extensions)
- [JSON event stream](https://pi.dev/docs/latest/json)
- [Settings and resource overrides](https://pi.dev/docs/latest/settings)
- [Security and trust boundaries](https://pi.dev/docs/latest/security)
