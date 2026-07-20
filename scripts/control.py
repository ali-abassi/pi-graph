"""Standalone workflow discovery and run-history control plane.

This deliberately contains no Loops imports. The CLI can use Loops over its
localhost API when available, but workflow discovery and direct execution must
remain useful on a clean Pi Workflows install.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_RUNNER = PRODUCT_ROOT / "scripts" / "run_steps.py"
PI_WORKFLOWS_HOME = Path(os.environ.get("PI_WORKFLOWS_HOME", Path.home() / ".pi-workflows")).expanduser()
STATE_DIR = Path(os.environ.get("PI_WORKFLOWS_STATE_DIR", PI_WORKFLOWS_HOME / "state")).expanduser()
PYGRAPH_EVENTS_DIR = STATE_DIR / "events"
DEFAULT_PORT = int(os.environ.get("LOOPS_PORT", "47821"))

IGNORED_DIRS = {
    ".git", ".next", ".pytest_cache", ".venv", "__pycache__", "cache",
    "dist", "build", "node_modules", "runs", "state",
}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96] or "workflow"


def _project_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _registry_roots() -> list[Path]:
    path = PI_WORKFLOWS_HOME / "roots.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    values = data.get("roots") if isinstance(data, dict) else data
    return [Path(value).expanduser() for value in values or [] if isinstance(value, str)]


def scan_roots() -> list[Path]:
    override = (os.environ.get("PI_WORKFLOWS_ROOTS") or os.environ.get("LOOPS_WORKFLOW_ROOTS") or "").strip()
    if override:
        roots = [Path(part).expanduser() for part in override.split(os.pathsep) if part]
    else:
        project = _project_root(Path.cwd())
        roots = [
            PRODUCT_ROOT / "examples",
            PRODUCT_ROOT / "templates",
            *_registry_roots(),
        ]
        if project is not None:
            roots[:0] = [project / ".codex" / "workflows", project]
        elif (Path.cwd() / "steps.yaml").is_file():
            # A shell opened at $HOME is not a project root. Only inspect a
            # non-git cwd when it is itself an explicit workflow directory.
            roots.insert(0, Path.cwd())
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def discover_workflows(force: bool = False) -> list[dict[str, Any]]:
    del force  # Standalone CLI processes are short-lived; no cache can survive.
    found_paths: set[Path] = set()
    candidates: list[tuple[Path, Path]] = []
    for root in scan_roots():
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root):
            directories[:] = sorted(name for name in directories if name not in IGNORED_DIRS)
            if "steps.yaml" not in files:
                continue
            path = (Path(current) / "steps.yaml").resolve()
            if path in found_paths:
                continue
            found_paths.add(path)
            candidates.append((root, path))

    found: dict[str, dict[str, Any]] = {}
    for root, path in candidates:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parent = path.parent.name or path.parent.parent.name or "workflow"
        # rel.parent already ends with the workflow directory, so appending
        # `parent` again produced ids like "01-hello-command-01-hello-command".
        base = slugify(str(rel.parent)) if str(rel.parent) != "." else slugify(parent)
        identifier = base
        suffix = 2
        while identifier in found and found[identifier]["path"] != str(path):
            identifier = f"{base}-{suffix}"
            suffix += 1
        runs_dir = path.parent / "runs"
        runs = []
        if runs_dir.is_dir():
            runs = sorted((item for item in runs_dir.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            spec = {}
        found[identifier] = {
            "id": identifier,
            "name": spec.get("workflow") or path.parent.name or identifier,
            "path": str(path),
            "cwd": str(path.parent),
            "runs_dir": str(runs_dir) if runs_dir.is_dir() else None,
            "run_count": len(runs),
            "last_run": dt.datetime.fromtimestamp(runs[0].stat().st_mtime, tz=dt.timezone.utc).isoformat() if runs else None,
            "model": spec.get("model"),
        }
    return sorted(found.values(), key=lambda item: item["last_run"] or "", reverse=True)


def list_workflow_runs(workflow_id: str, limit: int = 50, runs_dir: str | None = None) -> list[dict[str, Any]]:
    workflow = next((item for item in discover_workflows() if item["id"] == workflow_id), None)
    selected_runs_dir = runs_dir or (workflow["runs_dir"] if workflow else None)
    if not selected_runs_dir:
        return []
    runs_path = Path(selected_runs_dir)
    runs: list[dict[str, Any]] = []
    for path in sorted((item for item in runs_path.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        ledger: Any = []
        try:
            ledger = json.loads((path / "ledger.json").read_text(encoding="utf-8")) or []
        except (OSError, ValueError, TypeError):
            pass
        status = "unknown"
        try:
            text = (path / "log.md").read_text(encoding="utf-8", errors="replace")[-4_000:]
            if "run complete" in text:
                status = "complete"
            elif "FAIL" in text or "failed" in text:
                status = "failed"
            elif text.strip():
                status = "in_progress"
        except OSError:
            pass
        runs.append({
            "id": path.name,
            "path": str(path),
            "modified": dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat(),
            "status": status,
            "ledger": ledger,
        })
    return runs
