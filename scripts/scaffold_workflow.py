#!/usr/bin/env python3
"""Scaffold a repo-local deterministic Codex workflow bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def slug(value: str) -> str:
    out = []
    last_dash = False
    for char in value.lower():
        if char.isalnum():
            out.append(char)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    return "".join(out).strip("-") or "workflow"


def write(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite {path}; use --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def workflow_md(name: str, stages: list[str]) -> str:
    lines = "\n".join(f"{index + 1}. `{stage}`" for index, stage in enumerate(stages))
    return f"""# {name}

## Purpose

Describe the recurring task this deterministic Codex workflow controls.

## Stages

{lines}

## Artifact Contract

Live runs should write artifacts under `.tmp-{name}/`.

Required files:

- `manifest.json`
- one JSON artifact per stage
- `report.md`

## Mutation Policy

No mutation is allowed unless the manifest has:

```json
{{
  "stage": "execute",
  "allowed_to_mutate": true,
  "approval": {{
    "human_gate": "approved",
    "approved_artifact": ".tmp-{name}/dry_run.json"
  }}
}}
```

## Verification

List the deterministic checks that define done.
"""


def manifest_template(name: str, stages: list[str]) -> str:
    data = {
        "workflow": name,
        "case_id": "<stable-id>",
        "stage": stages[0],
        "stages": stages,
        "allowed_to_mutate": False,
        "approval": {
            "human_gate": "not_approved",
            "approved_by": None,
            "approved_at": None,
            "approved_artifact": None,
        },
        "artifacts": {},
        "history": [],
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def schema(name: str, stages: list[str]) -> str:
    data = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["workflow", "case_id", "stage", "status", "evidence"],
        "properties": {
            "workflow": {"const": name},
            "case_id": {"type": "string", "minLength": 1},
            "stage": {"enum": stages},
            "status": {"enum": ["pass", "fail", "blocked", "needs_review"]},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "next_stage": {"type": ["string", "null"], "enum": [*stages, None]},
            "residual_risk": {"type": "array", "items": {"type": "string"}},
        },
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def hook(name: str) -> str:
    return f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def deny(reason):
    print(json.dumps({{
        "hookSpecificOutput": {{
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}
    }}))


payload = json.loads(sys.stdin.read() or "{{}}")
command = ((payload.get("tool_input") or {{}}).get("command") or "")

# Replace this predicate with the workflow's real mutation surface.
is_mutation = "REPLACE_WITH_MUTATION_MARKER" in command
if not is_mutation:
    raise SystemExit(0)

manifest_path = Path(os.environ.get("WORKFLOW_MANIFEST", ".tmp-{name}/manifest.json"))
try:
    manifest = json.loads(manifest_path.read_text())
except Exception as exc:
    deny(f"{name} mutation blocked: manifest missing or invalid: {{exc}}")
    raise SystemExit(0)

approval = manifest.get("approval") or {{}}
allowed = (
    manifest.get("stage") == "execute"
    and manifest.get("allowed_to_mutate") is True
    and approval.get("human_gate") == "approved"
    and approval.get("approved_artifact")
)
if not allowed:
    deny("{name} mutation blocked: manifest is not approved for execute stage")
"""


def hooks_json() -> str:
    data = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/usr/bin/python3 \"$(git rev-parse --show-toplevel)/.codex/workflows/REPLACE_NAME/hooks/guard.py\"",
                            "timeout": 10,
                            "statusMessage": "Checking deterministic workflow gate",
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(data, indent=2) + "\n"


def prompt(name: str) -> str:
    return f"""Use $deterministic-codex-workflows.

Run the `{name}` workflow for CASE_ID.
Start by creating `.tmp-{name}/manifest.json` from `.codex/workflows/{name}/manifest.template.json`.
Follow stages sequentially and do not mutate until an approved dry-run artifact is recorded in the manifest.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--stages", default="intake,evidence,plan,dry_run,human_gate,execute,verify,report")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    name = slug(args.name)
    repo = Path(args.repo).expanduser().resolve()
    stages = [slug(stage) for stage in args.stages.split(",") if stage.strip()]
    if len(stages) < 2:
        raise SystemExit("provide at least two stages")

    root = repo / ".codex" / "workflows" / name
    write(root / "workflow.md", workflow_md(name, stages), args.force)
    write(root / "manifest.template.json", manifest_template(name, stages), args.force)
    write(root / "schemas" / "stage-output.schema.json", schema(name, stages), args.force)
    write(root / "hooks" / "guard.py", hook(name), args.force)
    write(root / "hooks" / "hooks.snippet.json", hooks_json().replace("REPLACE_NAME", name), args.force)
    write(root / "examples" / "prompt.md", prompt(name), args.force)
    (root / "hooks" / "guard.py").chmod(0o755)
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
