#!/usr/bin/env python3
"""Deterministic Pi CLI fixture for token-free end-to-end journeys.

Rules come from FAKE_PI_RULES_JSON. The first rule whose `contains` strings all
occur in the final prompt wins. Supported kinds deliberately mirror every Pi
stream condition the runner treats specially.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def log_argv(argv: list[str]) -> None:
    destination = os.environ.get("FAKE_PI_ARGV_LOG")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(argv, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def assistant(rule: dict[str, Any], provider: str, model: str, *, text: str = "",
              stop_reason: str = "stop", content: list[dict[str, Any]] | None = None,
              error: str | None = None) -> None:
    message: dict[str, Any] = {
        "role": "assistant",
        "provider": rule.get("provider", provider),
        "model": rule.get("model", model),
        "stopReason": stop_reason,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "usage": {
            "input": int(rule.get("input_tokens", 5)),
            "output": int(rule.get("output_tokens", 2)),
            "totalTokens": int(rule.get("tokens", 7)),
            "cost": {"total": float(rule.get("cost", 0.001))},
        },
    }
    if error:
        message["errorMessage"] = error
    emit({"type": "message_end", "message": message})


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("pi 0.84.2")
        return 0
    if argv == ["--list-models"]:
        print("provider model context max-out thinking images")
        print("fixture luna 1M 64K yes no")
        print("fixture judge 1M 64K yes no")
        return 0
    if "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc":
        emit({"id": "pi-graph-doctor", "success": True,
              "data": {"commands": [{"name": "skill:pi-graph"}]}})
        return 0

    log_argv(argv)
    prompt = argv[-1] if argv else ""
    requested = argv[argv.index("--model") + 1] if "--model" in argv else "fixture/luna"
    provider, _, model = requested.partition("/")
    if not model:
        provider, model = "fixture", provider
    try:
        rules = json.loads(os.environ.get("FAKE_PI_RULES_JSON", "[]"))
    except ValueError as error:
        print(f"invalid FAKE_PI_RULES_JSON: {error}", file=sys.stderr)
        return 2
    rule = next(
        (candidate for candidate in rules
         if all(str(marker) in prompt for marker in candidate.get("contains", []))
         and all(str(marker) not in prompt for marker in candidate.get("excludes", []))),
        {"kind": "success", "text": "fixture output"},
    )
    kind = rule.get("kind", "success")
    if effect := rule.get("write"):
        path = Path(effect["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(effect.get("text", "")), encoding="utf-8")

    if kind == "exit":
        print(rule.get("stderr", "fixture process failure"), file=sys.stderr)
        return int(rule.get("code", 1))
    if kind == "malformed":
        print("{not-json")
        return 0
    if kind == "extension_error":
        emit({"type": "extension_error", "error": rule.get("error", "fixture extension error")})
        return 0
    if kind == "auto_retry_error":
        emit({"type": "auto_retry_end", "success": False,
              "error": rule.get("error", "fixture retry exhausted")})
        return 0
    if kind == "model_error":
        assistant(rule, provider, model, stop_reason="error", content=[],
                  error=rule.get("error", "fixture provider error"))
    elif kind == "blank":
        assistant(rule, provider, model, text="   ")
    elif kind == "tool":
        assistant(rule, provider, model, stop_reason="toolUse", content=[{
            "type": "toolCall", "id": "call-1", "name": rule.get("tool", "read"),
            "arguments": rule.get("arguments", {"path": "fixture.txt"}),
        }])
        emit({"type": "tool_execution_start", "toolCallId": "call-1",
              "toolName": rule.get("tool", "read"), "args": rule.get("arguments", {})})
        emit({"type": "tool_execution_end", "toolCallId": "call-1",
              "toolName": rule.get("tool", "read"), "result": {}, "isError": False})
        assistant(rule, provider, model, text=str(rule.get("text", "fixture tool output")))
    else:
        assistant(rule, provider, model, text=str(rule.get("text", "fixture output")))
    if kind != "unsettled":
        emit({"type": "agent_settled"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
