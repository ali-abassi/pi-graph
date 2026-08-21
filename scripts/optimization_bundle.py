#!/usr/bin/env python3
"""Durable, local state for deterministic workflow optimization experiments.

This module never executes a workflow or reads a private holdout.  It owns the
frozen experiment contract, immutable snapshots, one-writer event ledger,
recovery projection, candidate-boundary checks, and byte-perfect rollback.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import yaml
from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from version_info import build_version_info

MAX_FILE_BYTES = 16 * 1024 * 1024
ZERO_HASH = "0" * 64
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
TERMINAL_STATES = {
    "plateau", "target_achieved", "budget_exhausted", "blocked_by_infra",
    "harden_eval", "authority_required", "stopped_by_user", "promoted",
    "promotion_reverted", "retained_incumbent", "failed",
}


class OptimizationError(RuntimeError):
    """Fail-closed experiment contract, integrity, or transition error."""


class OptimizationBusy(OptimizationError):
    """Another writer owns this experiment."""


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_bytes(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    path = path.expanduser().absolute()
    try:
        before = path.lstat()
    except OSError as error:
        raise OptimizationError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OptimizationError(f"expected a regular non-symlink file: {path}")
    if before.st_size > limit:
        raise OptimizationError(f"file exceeds {limit} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise OptimizationError(f"cannot open {path}: {error}") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OptimizationError(f"file identity changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OptimizationError(f"file exceeds {limit} bytes: {path}")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise OptimizationError(f"file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    return sha256_bytes(_safe_bytes(path))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(_safe_bytes(path).decode("utf-8"),
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OptimizationError(f"invalid JSON in {path}: {error}") from error


def _schema(root: Path, name: str) -> dict[str, Any]:
    value = _load_json(root / "schemas" / name)
    if not isinstance(value, dict):
        raise OptimizationError(f"schema is not an object: {name}")
    return value


def _require_finite(value: Any, path: str = "/") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise OptimizationError(f"non-finite number at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _require_finite(child, f"{path.rstrip('/')}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _require_finite(child, f"{path.rstrip('/')}/{index}")


def validate_schema(root: Path, name: str, value: Any) -> None:
    _require_finite(value)
    schema = _schema(root, name)
    cls = validator_for(schema)
    cls.check_schema(schema)
    errors = sorted(cls(schema, format_checker=FormatChecker()).iter_errors(value),
                    key=lambda item: list(item.absolute_path))
    if errors:
        first = errors[0]
        where = "/" + "/".join(map(str, first.absolute_path)) if first.absolute_path else "/"
        raise OptimizationError(f"{name} validation failed at {where}: {first.message}")


def semantic_workflow(raw: bytes) -> tuple[Any, str]:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise OptimizationError(f"invalid workflow YAML: {error}") from error
    if not isinstance(value, dict):
        raise OptimizationError("workflow must be a YAML mapping")
    return value, sha256_bytes(canonical(value))


def _resolve_source(base: Path, relative: str) -> Path:
    unresolved = (base / relative).expanduser().absolute()
    with contextlib.suppress(FileNotFoundError):
        if unresolved.is_symlink():
            raise OptimizationError(f"source is a symlink: {relative}")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as error:
        raise OptimizationError(f"source escapes experiment root: {relative}") from error
    return candidate


def _count_jsonl(raw: bytes, label: str) -> int:
    count = 0
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise OptimizationError(f"{label} has blank JSONL line {number}")
        try:
            json.loads(line, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
        except (json.JSONDecodeError, ValueError) as error:
            raise OptimizationError(f"{label} has invalid JSONL line {number}: {error}") from error
        count += 1
    if count < 1:
        raise OptimizationError(f"{label} is empty")
    return count


class OwnerLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "OwnerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.fd)
            self.fd = None
            raise OptimizationBusy(f"experiment is busy: {self.path.parent}") from error
        os.ftruncate(self.fd, 0)
        os.write(self.fd, canonical({"pid": os.getpid(), "acquired_at": time.time()}))
        os.fsync(self.fd)
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class EventLedger:
    def __init__(self, bundle: Path, experiment_id: str, root: Path):
        self.path = bundle / "events.jsonl"
        self.commit_path = bundle / "events.commit.json"
        self.experiment_id = experiment_id
        self.root = root

    def replay(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            if self.commit_path.exists():
                raise OptimizationError("ledger commit exists without ledger")
            return []
        raw = _safe_bytes(self.path, 64 * 1024 * 1024)
        if not self.commit_path.exists():
            if raw:
                raise OptimizationError("ledger is missing commit marker")
            return []
        commit = _load_json(self.commit_path)
        if (not isinstance(commit, dict) or set(commit) != {"seq", "event_sha256", "ledger_bytes"}
                or not isinstance(commit["seq"], int) or commit["seq"] < 0
                or not isinstance(commit["ledger_bytes"], int) or commit["ledger_bytes"] < 0
                or commit["ledger_bytes"] > len(raw)):
            raise OptimizationError("ledger commit marker is malformed")
        prefix = raw[:commit["ledger_bytes"]]
        if prefix and not prefix.endswith(b"\n"):
            raise OptimizationError("committed ledger prefix is torn")
        lines = prefix.splitlines()
        if len(lines) != commit["seq"]:
            raise OptimizationError("ledger commit sequence does not match committed prefix")
        records: list[dict[str, Any]] = []
        previous = ZERO_HASH
        for index, line in enumerate(lines, 1):
            try:
                record = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (json.JSONDecodeError, ValueError) as error:
                raise OptimizationError(f"corrupt ledger event {index}: {error}") from error
            if not isinstance(record, dict) or record.get("seq") != index:
                raise OptimizationError(f"ledger sequence mismatch at event {index}")
            if record.get("experiment_id") != self.experiment_id or record.get("prev_sha256") != previous:
                raise OptimizationError(f"ledger chain mismatch at event {index}")
            claimed = record.get("event_sha256")
            unhashed = dict(record)
            unhashed.pop("event_sha256", None)
            actual = sha256_bytes(canonical(unhashed))
            if claimed != actual:
                raise OptimizationError(f"ledger event hash mismatch at event {index}")
            validate_schema(self.root, "optimization-event.schema.json", record)
            previous = claimed
            records.append(record)
        if commit["event_sha256"] != previous:
            raise OptimizationError("ledger commit hash does not match committed prefix")
        return records

    def append(self, event_type: str, payload: dict[str, Any], *, actor: str = "piw", attempt: int = 1) -> dict[str, Any]:
        records = self.replay()
        previous = records[-1]["event_sha256"] if records else ZERO_HASH
        if self.path.exists() and self.commit_path.exists():
            committed_bytes = _load_json(self.commit_path)["ledger_bytes"]
            if self.path.stat().st_size > committed_bytes:
                with self.path.open("r+b") as stream:
                    stream.truncate(committed_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
        record = {
            "schema": "pi-graph.optimization-event.v1", "seq": len(records) + 1,
            "timestamp": _now(), "experiment_id": self.experiment_id,
            "type": event_type, "actor": actor, "attempt": attempt,
            "payload": payload, "prev_sha256": previous,
        }
        record["event_sha256"] = sha256_bytes(canonical(record))
        validate_schema(self.root, "optimization-event.schema.json", record)
        line = canonical(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            written = os.write(fd, line)
            if written != len(line):
                raise OptimizationError("short ledger append")
            os.fsync(fd)
        finally:
            os.close(fd)
        atomic_write(self.commit_path, canonical({
            "seq": record["seq"], "event_sha256": record["event_sha256"],
            "ledger_bytes": self.path.stat().st_size,
        }))
        return record


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _executable_identity(argv: list[str], environment: dict[str, str]) -> dict[str, str]:
    token = sys.executable if argv[0] == "python3" else argv[0]
    resolved = (Path(token).expanduser().resolve() if Path(token).is_absolute()
                else Path(shutil.which(token, path=environment.get("PATH") or os.environ.get("PATH")) or ""))
    if not str(resolved) or not resolved.is_file():
        raise OptimizationError(f"executable cannot be resolved: {argv[0]}")
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def _program_identity(program: dict[str, Any], environment: dict[str, str]) -> dict[str, Any]:
    executable = _executable_identity(program["argv"], environment)
    return {
        "argv_sha256": sha256_bytes(canonical(program["argv"])),
        "sources_sha256": sha256_bytes(canonical(program["sources"])),
        "executable_path": executable["path"], "executable_sha256": executable["sha256"],
        "timeout_seconds": program["timeout_seconds"],
    }


def effective_environment(contract: dict[str, Any]) -> dict[str, str]:
    configured = {str(key): str(value) for key, value in contract["execution"]["environment"].items()}
    configured.setdefault("PATH", os.environ.get("PATH", ""))
    configured["PI_GRAPH_OPTIMIZATION"] = "1"
    configured["PI_GRAPH_NETWORK_POLICY"] = contract["boundaries"]["network"]
    configured["PYTHONDONTWRITEBYTECODE"] = "1"
    return configured


def _runtime_fingerprint(root: Path, contract: dict[str, Any]) -> dict[str, Any]:
    version = build_version_info(root, default_compare=False)
    execution = contract["execution"]
    dependencies = []
    for item in execution["dependency_files"]:
        dependencies.append({"path": item["path"], "sha256": item["sha256"]})
    environment_hashes = {key: sha256_bytes(value.encode())
                          for key, value in effective_environment(contract).items()}
    memory = "unknown"
    with contextlib.suppress(ValueError, OSError, AttributeError):
        memory = str(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    result = {
        "lifecycle_schema": "pi-graph.optimization.v1",
        "schema_versions": {
            "contract": "pi-graph.optimization-contract.v1", "manifest": "pi-graph.optimization-manifest.v1",
            "state": "pi-graph.optimization-state.v1", "event": "pi-graph.optimization-event.v1",
            "metrics": "pi-graph.optimization-metrics.v1", "receipt": "pi-graph.optimization-receipt.v1",
        },
        "product_version": version["product_version"],
        "product_tree_sha256": version["executing"]["tree_sha256"],
        "source_revision": version["executing"]["revision"], "source_dirty": version["executing"]["dirty"],
        "piw": {"path": str(root / "scripts/piw.py"), "sha256": sha256_file(root / "scripts/piw.py")},
        "run_batch": {"path": str(root / "scripts/run_batch.py"), "sha256": sha256_file(root / "scripts/run_batch.py")},
        "run_steps": {"path": str(root / "scripts/run_steps.py"), "sha256": sha256_file(root / "scripts/run_steps.py")},
        "python": platform.python_version(), "node": version["runtime"]["node"], "pi": version["runtime"]["pi"],
        "provider": execution["provider"], "model": execution["model"], "thinking": execution["thinking"],
        "tools": execution["tools"], "dependencies": dependencies, "environment_hashes": environment_hashes,
        "network": contract["boundaries"]["network"],
        "batch": {**execution["batch"], "cache": execution["cache"]},
        "platform": version["runtime"]["platform"], "cpu": platform.processor() or "unknown", "memory": memory,
    }
    result["fingerprint_sha256"] = sha256_bytes(canonical(result))
    return result


def _source_inventory(contract: dict[str, Any], base: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    groups = [
        ("evaluator", contract["evaluation"]["evaluator"]["sources"]),
        ("parser", contract["evaluation"]["parser"]["sources"]),
        ("protected", contract["boundaries"]["protected_files"]),
        ("dependency", contract["execution"]["dependency_files"]),
    ]
    seen: set[tuple[str, str]] = set()
    for role, items in groups:
        for item in items:
            key = role, item["path"]
            if key in seen:
                continue
            seen.add(key)
            resolved = _resolve_source(base, item["path"])
            actual = sha256_file(resolved)
            if actual != item["sha256"]:
                raise OptimizationError(f"frozen source fingerprint mismatch: {item['path']}")
            result.append({"role": role, "path": str(resolved), "sha256": actual})
    return result


def _copy_verified(source: Path, destination: Path, expected: str | None = None) -> str:
    raw = _safe_bytes(source)
    actual = sha256_bytes(raw)
    if expected is not None and actual != expected:
        raise OptimizationError(f"fingerprint mismatch: {source}")
    atomic_write(destination, raw, stat.S_IMODE(source.stat().st_mode) or 0o600)
    if sha256_file(destination) != actual:
        raise OptimizationError(f"snapshot verification failed: {destination}")
    return actual


class OptimizationBundle:
    def __init__(self, path: Path, product_root: Path | None = None):
        self.path = path.resolve()
        self.product_root = (product_root or Path(__file__).resolve().parents[1]).resolve()
        self.manifest = _load_json(self.path / "manifest.json") if (self.path / "manifest.json").exists() else None
        self.experiment_id = self.manifest["experiment_id"] if isinstance(self.manifest, dict) else self.path.name
        self.ledger = EventLedger(self.path, self.experiment_id, self.product_root)

    @classmethod
    def init(cls, workflow: Path, contract_path: Path, out: Path, *, holdout_path: Path | None = None,
             experiment_id: str | None = None, product_root: Path | None = None) -> "OptimizationBundle":
        root = (product_root or Path(__file__).resolve().parents[1]).resolve()
        workflow = workflow.expanduser().absolute()
        contract_path = contract_path.expanduser().absolute()
        out = out.expanduser().absolute()
        if out.exists():
            raise OptimizationError(f"experiment already exists: {out}")
        contract = _load_json(contract_path)
        validate_schema(root, "optimization-contract.schema.json", contract)
        if not isinstance(contract, dict):
            raise OptimizationError("contract must be an object")
        gate_ids = [gate["id"] for gate in contract["evaluation"]["hard_gates"]]
        if len(gate_ids) != len(set(gate_ids)):
            raise OptimizationError("hard gate ids must be unique")
        mutable_ids = [item["mechanism"] for item in contract["boundaries"]["mutable"]]
        mutable_pointers = [item["pointer"] for item in contract["boundaries"]["mutable"]]
        if len(mutable_ids) != len(set(mutable_ids)) or len(mutable_pointers) != len(set(mutable_pointers)):
            raise OptimizationError("mutable mechanism ids and pointers must be unique")
        base = contract_path.parent.resolve()
        workflow_raw = _safe_bytes(workflow)
        workflow_value, semantic_hash = semantic_workflow(workflow_raw)
        del workflow_value
        development_path = _resolve_source(base, contract["development"]["path"])
        development_raw = _safe_bytes(development_path)
        development_hash = sha256_bytes(development_raw)
        if development_hash != contract["development"]["sha256"]:
            raise OptimizationError("development corpus fingerprint mismatch")
        if _count_jsonl(development_raw, "development corpus") != contract["development"]["count"]:
            raise OptimizationError("development corpus count mismatch")
        sources = _source_inventory(contract, base)
        exp_id = experiment_id or out.name
        if not ID_RE.fullmatch(exp_id):
            raise OptimizationError("invalid experiment id")
        # Validate the holdout locator only as metadata. Never open, copy, hash,
        # parse, glob, or include holdout bytes during initialization.
        # The holdout path is deliberately ignored here. Initialization may
        # know only the digest/count; no locator or bytes enter candidate-readable state.
        del holdout_path
        runtime = _runtime_fingerprint(root, contract)
        effective_env = effective_environment(contract)
        created_at = _now()
        contract_raw = canonical(contract)
        manifest = {
            "schema": "pi-graph.optimization-manifest.v1", "experiment_id": exp_id,
            "contract": {"path": "contract.json", "sha256": sha256_bytes(contract_raw)},
            "baseline": {"source_path": str(workflow), "snapshot": "artifacts/baseline.yaml",
                         "sha256": sha256_bytes(workflow_raw), "semantic_sha256": semantic_hash,
                         "mode": stat.S_IMODE(workflow.stat().st_mode)},
            "sources": sources,
            "development": {"sha256": development_hash, "format": "jsonl", "count": contract["development"]["count"]},
            "holdout": {"sha256": contract["holdout"]["sha256"], "format": "jsonl", "count": contract["holdout"]["count"]},
            "evaluation": {
                "evaluation_version": contract["evaluation_version"],
                "evaluator": _program_identity(contract["evaluation"]["evaluator"], effective_env),
                "parser": _program_identity(contract["evaluation"]["parser"], effective_env),
                "hard_gates": [{"id": gate["id"], "argv_sha256": sha256_bytes(canonical(gate["argv"])),
                                **{f"executable_{key}": value for key, value in _executable_identity(gate["argv"], effective_env).items()},
                                "timeout_seconds": gate["timeout_seconds"]} for gate in contract["evaluation"]["hard_gates"]],
                "metric": {"name": contract["objective"]["primary_metric"], "direction": contract["objective"]["direction"],
                           "minimum_gain": contract["objective"]["minimum_gain"]},
                "repeats": contract["evaluation"]["repeats"], "seeds": contract["evaluation"]["seeds"],
                "uncertainty": contract["evaluation"]["uncertainty"], "tie_breakers": contract["evaluation"]["tie_breakers"],
            },
            "boundaries": {"mutable": contract["boundaries"]["mutable"],
                           "authorized_effects": contract["boundaries"]["authorized_effects"],
                           "network": contract["boundaries"]["network"], "rollback": contract["boundaries"]["rollback"]},
            "budgets": contract["budgets"], "runtime": runtime, "created_at": created_at,
        }
        validate_schema(root, "optimization-manifest.schema.json", manifest)
        try:
            out.mkdir(parents=True)
            for directory in ("artifacts", "candidates", "evaluations", "receipts", "private"):
                (out / directory).mkdir(mode=0o700)
            atomic_write(out / "contract.json", contract_raw)
            atomic_write(out / "manifest.json", canonical(manifest))
            atomic_write(out / "development.jsonl", development_raw)
            frozen_sources = out / "artifacts" / "frozen-sources"
            frozen_sources.mkdir(mode=0o700)
            for source in sources:
                snapshot = frozen_sources / source["sha256"]
                if not snapshot.exists():
                    _copy_verified(Path(source["path"]), snapshot, source["sha256"])
            _copy_verified(workflow, out / "artifacts" / "baseline.yaml")
            _copy_verified(workflow, out / "artifacts" / "active.yaml")
            bundle = cls(out, root)
            with bundle.lock():
                bundle.ledger.append("contract_validated", {
                    "contract_sha256": manifest["contract"]["sha256"],
                    "manifest_sha256": sha256_file(out / "manifest.json"),
                    "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
                })
                bundle.ledger.append("artifact_initialized", {
                    "artifact_sha256": manifest["baseline"]["sha256"],
                    "semantic_sha256": semantic_hash,
                    "snapshot": {"path": "artifacts/baseline.yaml", "sha256": manifest["baseline"]["sha256"]},
                })
                bundle.write_state()
            return bundle
        except Exception:
            import shutil
            shutil.rmtree(out, ignore_errors=True)
            raise

    def lock(self) -> OwnerLock:
        return OwnerLock(self.path / "owner.lock")

    def contract(self) -> dict[str, Any]:
        value = _load_json(self.path / "contract.json")
        validate_schema(self.product_root, "optimization-contract.schema.json", value)
        return value

    def verify_integrity(self, *, include_active: bool = True) -> None:
        if not isinstance(self.manifest, dict):
            raise OptimizationError("manifest missing")
        validate_schema(self.product_root, "optimization-manifest.schema.json", self.manifest)
        checks = [
            (self.path / self.manifest["contract"]["path"], self.manifest["contract"]["sha256"], "contract"),
            (self.path / "development.jsonl", self.manifest["development"]["sha256"], "development corpus"),
            (self.path / self.manifest["baseline"]["snapshot"], self.manifest["baseline"]["sha256"], "baseline snapshot"),
        ]
        state = self.state()
        for key in ("baseline", "incumbent"):
            artifact = state.get(key)
            if artifact and artifact["id"] != "baseline":
                checks.append((self.path / artifact["snapshot"], artifact["artifact_sha256"],
                               f"{key} snapshot"))
        if include_active:
            incumbent = state.get("incumbent") or state.get("baseline")
            expected = incumbent["artifact_sha256"] if incumbent else self.manifest["baseline"]["sha256"]
            checks.append((self.path / "artifacts" / "active.yaml", expected, "active artifact"))
        for path, expected, label in checks:
            if sha256_file(path) != expected:
                raise OptimizationError(f"immutable {label} fingerprint drift")
        contract = self.contract()
        for source in self.manifest["sources"]:
            snapshot = self.path / "artifacts" / "frozen-sources" / source["sha256"]
            if sha256_file(snapshot) != source["sha256"]:
                raise OptimizationError(f"frozen source snapshot drift: {source['path']}")
            if sha256_file(Path(source["path"])) != source["sha256"]:
                raise OptimizationError(f"immutable source fingerprint drift: {source['path']}")
        environment = effective_environment(contract)
        expected_programs = {
            "evaluator": _program_identity(contract["evaluation"]["evaluator"], environment),
            "parser": _program_identity(contract["evaluation"]["parser"], environment),
        }
        for role, identity in expected_programs.items():
            if identity != self.manifest["evaluation"][role]:
                raise OptimizationError(f"{role} executable or argv fingerprint drift")
        expected_gates = [{"id": gate["id"], "argv_sha256": sha256_bytes(canonical(gate["argv"])),
                           **{f"executable_{key}": value for key, value in _executable_identity(gate["argv"], environment).items()},
                           "timeout_seconds": gate["timeout_seconds"]}
                          for gate in contract["evaluation"]["hard_gates"]]
        if expected_gates != self.manifest["evaluation"]["hard_gates"]:
            raise OptimizationError("hard-gate executable or argv fingerprint drift")
        runtime = _runtime_fingerprint(self.product_root, contract)
        if runtime["fingerprint_sha256"] != self.manifest["runtime"]["fingerprint_sha256"]:
            raise OptimizationError("runtime fingerprint drift")
        self.ledger.replay()

    def state(self) -> dict[str, Any]:
        state = derive_state(self.experiment_id, self.ledger.replay())
        # Older v1 commit events omitted semantic_sha256. Recompute it from the
        # committed snapshot so authoring clients never receive a raw-byte hash
        # mislabeled as semantic identity.
        for key in ("baseline", "incumbent"):
            artifact = state.get(key)
            if not artifact:
                continue
            snapshot = self.path / artifact["snapshot"]
            if snapshot.is_file() and sha256_file(snapshot) == artifact["artifact_sha256"]:
                _, artifact["semantic_sha256"] = semantic_workflow(_safe_bytes(snapshot))
        return state

    def write_state(self) -> dict[str, Any]:
        state = self.state()
        validate_schema(self.product_root, "optimization-state.schema.json", state)
        atomic_write(self.path / "state.json", canonical(state))
        return state

    def status(self) -> dict[str, Any]:
        state = self.state()
        return {"schema": "pi-graph.optimization-status.v1", "experiment_id": self.experiment_id,
                "path": str(self.path), "state": state,
                "manifest_sha256": sha256_file(self.path / "manifest.json"),
                "ledger_sha256": sha256_file(self.path / "events.jsonl")}

    def restore_frozen_sources(self) -> list[str]:
        restored: list[str] = []
        for source in self.manifest["sources"]:
            target = Path(source["path"])
            snapshot = self.path / "artifacts" / "frozen-sources" / source["sha256"]
            raw = _safe_bytes(snapshot)
            mode = 0o600
            with contextlib.suppress(OSError):
                current = target.lstat()
                if stat.S_ISREG(current.st_mode):
                    mode = stat.S_IMODE(current.st_mode)
            atomic_write(target, raw, mode)
            if sha256_file(target) != source["sha256"]:
                raise OptimizationError(f"protected source restoration failed: {target}")
            restored.append(str(target))
        return restored

    def snapshot_active(self, candidate_id: str) -> Path:
        if not ID_RE.fullmatch(candidate_id):
            raise OptimizationError("invalid candidate id")
        destination = self.path / "candidates" / candidate_id / "parent.yaml"
        _copy_verified(self.path / "artifacts" / "active.yaml", destination)
        return destination

    def apply_candidate(self, candidate: Path, candidate_id: str, *, candidate_raw: bytes | None = None,
                        expected_parent: str | None = None,
                        validated: dict[str, Any] | None = None) -> tuple[str, str]:
        parent = self.path / "candidates" / candidate_id / "parent.yaml"
        if not parent.exists():
            parent = self.snapshot_active(candidate_id)
        parent_hash = sha256_file(parent)
        if expected_parent is not None and parent_hash != expected_parent:
            raise OptimizationError("candidate parent snapshot changed before apply")
        raw = candidate_raw if candidate_raw is not None else _safe_bytes(candidate)
        details = validated or validate_candidate(self, candidate, parent_hash)
        if sha256_bytes(raw) != details["artifact_sha256"]:
            raise OptimizationError("candidate changed after validation")
        destination = self.path / "candidates" / candidate_id / "candidate.yaml"
        atomic_write(destination, raw, self.manifest["baseline"]["mode"])
        atomic_write(self.path / "artifacts" / "active.yaml", raw, self.manifest["baseline"]["mode"])
        if sha256_file(self.path / "artifacts" / "active.yaml") != details["artifact_sha256"]:
            raise OptimizationError("candidate apply verification failed")
        return details["artifact_sha256"], details["semantic_sha256"]

    def restore(self, candidate_id: str, expected: str | None = None) -> str:
        snapshot = self.path / "candidates" / candidate_id / "parent.yaml"
        snapshot_hash = sha256_file(snapshot)
        if expected is not None and snapshot_hash != expected:
            raise OptimizationError("candidate parent snapshot changed before rollback")
        expected = snapshot_hash
        atomic_write(self.path / "artifacts" / "active.yaml", _safe_bytes(snapshot),
                     self.manifest["baseline"]["mode"])
        actual = sha256_file(self.path / "artifacts" / "active.yaml")
        if actual != expected:
            raise OptimizationError("byte-perfect rollback verification failed")
        return actual


def derive_state(experiment_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema": "pi-graph.optimization-state.v1", "experiment_id": experiment_id,
        "status": "created", "baseline": None, "incumbent": None,
        "candidate_cursor": 0, "consecutive_non_keeps": 0, "holdout_uses": 0,
        "budget": {"candidates_dispatched": 0, "candidates_completed": 0, "keeps": 0, "discards": 0,
                   "usage": {"tokens": 0, "cost_usd": 0, "wall_seconds": 0, "failures": 0}},
        "resume_count": 0, "pending_operation": None, "terminal_reason": None,
        "last_event_seq": 0, "updated_at": records[-1]["timestamp"] if records else _now(),
    }
    for record in records:
        event, payload = record["type"], record["payload"]
        state["last_event_seq"] = record["seq"]
        state["updated_at"] = record["timestamp"]
        if event == "contract_validated": state["status"] = "contract_validating"
        elif event == "artifact_initialized": state["status"] = "created"
        elif event == "baseline_evaluated": state["status"] = "baseline_running"
        elif event == "baseline_verified":
            artifact = {"id": "baseline", "parent_id": None,
                        "artifact_sha256": payload["artifact_sha256"],
                        "semantic_sha256": payload.get("semantic_sha256", payload["artifact_sha256"]),
                        "snapshot": "artifacts/baseline.yaml", "score": payload["metrics"]["primary"],
                        "gates_passed": True, "committed_event_seq": record["seq"]}
            state["baseline"] = state["incumbent"] = artifact
            state["status"] = "baseline_verified"
        elif event == "candidate_declared":
            state["status"] = "searching"; state["candidate_cursor"] += 1
            state["budget"]["candidates_dispatched"] += 1
            state["pending_operation"] = {"id": f"candidate:{payload['candidate_id']}",
                "type": "candidate", "stage": "declared",
                "candidate_id": payload["candidate_id"], "started_at": record["timestamp"]}
        elif event == "snapshot_created" and state["pending_operation"]:
            state["pending_operation"]["stage"] = "snapshotted"
        elif event == "candidate_mutated" and state["pending_operation"]:
            state["pending_operation"]["stage"] = "mutated"
        elif event == "gate_evaluated" and state["pending_operation"]:
            state["pending_operation"]["stage"] = "gating"
        elif event == "arm_evaluated" and state["pending_operation"]:
            state["pending_operation"]["stage"] = "evaluated"
        elif event == "candidate_decided":
            state["budget"]["candidates_completed"] += 1
            decision = payload["decision"]
            if decision == "keep":
                state["budget"]["keeps"] += 1; state["consecutive_non_keeps"] = 0
            elif decision in {"discard", "gate_failed", "inconclusive"}:
                state["budget"]["discards"] += 1; state["consecutive_non_keeps"] += 1
            else:
                state["budget"]["usage"]["failures"] += 1
            metrics = payload.get("metrics")
            if metrics:
                usage = metrics["usage"]
                for key in ("tokens", "cost_usd", "wall_seconds"):
                    state["budget"]["usage"][key] += usage[key]
            if state["pending_operation"]:
                state["pending_operation"]["stage"] = "decided"
        elif event == "incumbent_committed":
            state["incumbent"] = {"id": payload["candidate_id"], "parent_id": payload["parent_id"],
                "artifact_sha256": payload["artifact_sha256"], "semantic_sha256": payload.get("semantic_sha256", payload["artifact_sha256"]),
                "snapshot": f"candidates/{payload['candidate_id']}/candidate.yaml", "score": payload["score"],
                "gates_passed": True, "committed_event_seq": record["seq"]}
            state["pending_operation"] = None
        elif event == "rollback_verified":
            state["pending_operation"] = None
        elif event == "holdout_access_granted": state["holdout_uses"] += 1; state["status"] = "promotion_running"
        elif event == "resumed":
            state["resume_count"] = payload["resume_count"]
            state["status"] = "searching" if state["baseline"] is not None else "created"
        elif event == "promotion": state["status"] = "promoted"; state["terminal_reason"] = "promoted"
        elif event == "promotion_reverted":
            state["status"] = "promotion_reverted"; state["terminal_reason"] = "promotion_reverted"
            if state["baseline"] is not None:
                state["incumbent"] = state["baseline"]
        elif event in {"budget_exhausted", "authority_required", "harden_eval", "blocked_by_infra", "failed"}:
            mapping = {"budget_exhausted": "budget_exhausted", "authority_required": "authority_required",
                       "harden_eval": "harden_eval", "blocked_by_infra": "blocked_by_infra", "failed": "failed"}
            state["status"] = mapping[event]; state["terminal_reason"] = mapping[event]
        elif event == "terminal": state["status"] = payload["status"]; state["terminal_reason"] = payload["status"]
    return state


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise OptimizationError(f"invalid JSON Pointer: {pointer}")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _changed_paths(left: Any, right: Any, prefix: str = "") -> set[str]:
    if type(left) is not type(right):
        return {prefix or "/"}
    if isinstance(left, dict):
        paths: set[str] = set()
        for key in set(left) | set(right):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            child = f"{prefix}/{escaped}"
            if key not in left or key not in right:
                paths.add(child)
            else:
                paths.update(_changed_paths(left[key], right[key], child))
        return paths
    if isinstance(left, list):
        paths = set()
        for index in range(max(len(left), len(right))):
            child = f"{prefix}/{index}"
            if index >= len(left) or index >= len(right): paths.add(child)
            else: paths.update(_changed_paths(left[index], right[index], child))
        return paths
    return set() if left == right else {prefix or "/"}


def validate_candidate(bundle: OptimizationBundle, candidate: Path, expected_parent_sha256: str,
                       *, mechanism: str | None = None,
                       candidate_raw: bytes | None = None) -> dict[str, Any]:
    active = bundle.path / "artifacts" / "active.yaml"
    parent_raw = _safe_bytes(active)
    if sha256_bytes(parent_raw) != expected_parent_sha256:
        raise OptimizationError("candidate parent hash does not match incumbent")
    candidate_raw = candidate_raw if candidate_raw is not None else _safe_bytes(candidate)
    parent_value, _ = semantic_workflow(parent_raw)
    candidate_value, semantic_hash = semantic_workflow(candidate_raw)
    changed = sorted(_changed_paths(parent_value, candidate_value))
    if not changed:
        raise OptimizationError("candidate has no semantic change")
    allowed = bundle.manifest["boundaries"]["mutable"]
    matches = []
    for item in allowed:
        pointer = item["pointer"]
        if all(path == pointer or path.startswith(pointer + "/") for path in changed):
            matches.append(item)
    if mechanism is not None:
        matches = [item for item in matches if item["mechanism"] == mechanism]
    if len(matches) != 1:
        raise OptimizationError(f"candidate must change exactly one declared mutable path; changed={changed}")
    diff = {"mechanism": matches[0]["mechanism"], "pointer": matches[0]["pointer"], "changed": changed,
            "parent_sha256": expected_parent_sha256, "artifact_sha256": sha256_bytes(candidate_raw),
            "semantic_sha256": semantic_hash}
    diff["diff_sha256"] = sha256_bytes(canonical(diff))
    return diff


def stop_reason(state: dict[str, Any], budgets: dict[str, Any], *, elapsed_wall_seconds: float) -> str | None:
    values = state["budget"]
    usage = values["usage"]
    checks = (
        (values["candidates_dispatched"] >= budgets["max_candidates"], "candidates"),
        (elapsed_wall_seconds >= budgets["max_wall_seconds"], "wall_seconds"),
        (usage["tokens"] >= budgets["max_tokens"], "tokens"),
        (usage["cost_usd"] >= budgets["max_cost_usd"], "cost_usd"),
        (usage["failures"] > budgets["max_failures"], "failures"),
        (state["consecutive_non_keeps"] >= budgets["max_consecutive_non_keeps"], "plateau"),
    )
    for reached, reason in checks:
        if reached:
            return reason
    return None


def resolve_experiment(selector: str, roots: list[Path]) -> Path:
    candidate = Path(selector).expanduser()
    if candidate.exists():
        resolved = candidate.resolve()
        if not (resolved / "manifest.json").is_file():
            raise OptimizationError(f"not an experiment: {resolved}")
        return resolved
    matches = []
    for root in roots:
        if root.is_dir():
            for path in root.iterdir():
                if path.is_dir() and (path / "manifest.json").is_file() and (path.name == selector or selector in path.name):
                    matches.append(path.resolve())
    exact = [path for path in matches if path.name == selector]
    if len(exact) == 1: return exact[0]
    unique = sorted(set(matches))
    if len(unique) == 1: return unique[0]
    if not unique: raise OptimizationError(f"experiment not found: {selector}")
    raise OptimizationError(f"ambiguous experiment selector {selector}: {', '.join(map(str, unique))}")
