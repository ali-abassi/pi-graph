#!/usr/bin/env python3
"""Durable local-filesystem run bundles for Pi Graph.

This module is the sole writer of a bundle's manifest, state, trace, and owner
lock metadata.  The runner continues to own compatibility projections such as
log.md, ledger.json, and <step>.md.
"""

from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

MANIFEST_SCHEMA = "pi-graph.run-bundle.v1"
STATE_SCHEMA = "pi-graph.run-state.v1"
TRACE_SCHEMA = "pi-graph.trace-event.v1"
MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
TRACE_NAME = "trace.jsonl"
SNAPSHOT_NAME = "workflow.yaml"
LOCK_NAME = "run.lock"
OWNER_NAME = "run-owner.json"
BOOTSTRAP_NAME = ".bundle-bootstrap.json"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
RUN_STATUSES = {"initialized", "running", "interrupted", "failed", "completed"}
STEP_STATUSES = {"pending", "running", "passed", "failed", "skipped", "cached", "interrupted"}


class BundleError(RuntimeError):
    """A durable boundary is absent, busy, corrupt, or inconsistent."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def atomic_write_json(path: Path, value: object) -> None:
    """Durably replace one JSON file without exposing partial bytes."""
    descriptor, raw_temp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            json.dump(value, output, indent=1)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        _fsync_parent(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp.unlink(missing_ok=True)


def _create_exclusive(path: Path, data: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise BundleError(f"refusing to rewrite immutable bundle file: {path.name}") from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write creating {path}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent(path.parent)


def _validate_schema(value: dict[str, Any], filename: str, label: str) -> None:
    try:
        schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleError(f"cannot load published {label} schema: {error}") from error
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        where = ".".join(str(part) for part in first.path) or "<root>"
        raise BundleError(f"{label} schema invalid at {where}: {first.message}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleError(f"durable bundle has unreadable {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise BundleError(f"durable bundle {path.name} must be a JSON object")
    return value


def _require_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise BundleError(f"durable bundle has invalid SHA-256 at {where}")
    return value


class RunBundle:
    """One fenced v1 bundle. Hold the object for the whole mutating process."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.manifest_path = self.run_dir / MANIFEST_NAME
        self.state_path = self.run_dir / STATE_NAME
        self.trace_path = self.run_dir / TRACE_NAME
        self.snapshot_path = self.run_dir / SNAPSHOT_NAME
        self.lock_path = self.run_dir / LOCK_NAME
        self.owner_path = self.run_dir / OWNER_NAME
        self._lock_fd: int | None = None
        self._mutex = threading.RLock()
        self._projection_failed = False
        self.manifest: dict[str, Any] | None = None
        self.state: dict[str, Any] | None = None

    @property
    def exists(self) -> bool:
        return self.manifest_path.exists()

    def acquire(self) -> "RunBundle":
        """Acquire a stable-inode advisory fence before any run content changes."""
        if self._lock_fd is not None:
            return self
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            owner = ""
            try:
                metadata = _read_json(self.owner_path)
                owner = f" (owner pid {metadata.get('pid')}, host {metadata.get('host')})"
            except BundleError:
                pass
            raise BundleError(f"run is already owned by another writer{owner}: {self.run_dir}") from error
        self._lock_fd = descriptor
        atomic_write_json(self.owner_path, {
            "pid": os.getpid(), "host": socket.gethostname(), "acquired_at": utc_now(),
        })
        return self

    def close(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> "RunBundle":
        return self.acquire()

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()

    def _require_lock(self) -> None:
        if self._lock_fd is None:
            raise BundleError("bundle mutation requires the run owner lock")

    def initialize(
        self,
        *,
        workflow_bytes: bytes,
        source_path: Path,
        input_bytes: bytes | None,
        cwd: Path,
        workflow_dir: Path,
        step_ids: list[str],
        events_path: Path | None,
    ) -> None:
        self._require_lock()
        bootstrap_path = self.run_dir / BOOTSTRAP_NAME
        if (self.manifest_path.exists() or self.state_path.exists() or
                self.trace_path.exists() or bootstrap_path.exists()):
            raise BundleError("refusing to initialize over an existing durable bundle")
        _create_exclusive(self.snapshot_path, workflow_bytes)
        if input_bytes is not None:
            _create_exclusive(self.run_dir / "input.txt", input_bytes)
        _create_exclusive(self.trace_path, b"")
        now = utc_now()
        input_record = None if input_bytes is None else {
            "path": "input.txt", "sha256": sha256_bytes(input_bytes), "bytes": len(input_bytes),
        }
        self.state = {
            "schema": STATE_SCHEMA,
            "run_id": self.run_dir.name,
            "status": "initialized",
            "trace_seq": 0,
            "resume_count": 0,
            "steps": {sid: {"status": "pending", "updated_at": now} for sid in step_ids},
            "updated_at": now,
        }
        self.manifest = {
            "schema": MANIFEST_SCHEMA,
            "run_id": self.run_dir.name,
            "workflow": {
                "snapshot": SNAPSHOT_NAME,
                "source_path": str(Path(source_path).resolve()),
                "sha256": sha256_bytes(workflow_bytes),
                "bytes": len(workflow_bytes),
            },
            "input": input_record,
            "execution": {"cwd": str(Path(cwd).resolve()), "workflow_dir": str(Path(workflow_dir).resolve())},
            "status": "initialized",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "projections": {
                "ledger": "ledger.json", "log": "log.md", "step_artifacts": "<step>.md",
                "produced": "produced/", "events": str(events_path.resolve()) if events_path else None,
            },
        }
        # One recoverable bootstrap projection closes the two-file initialization
        # crash window. It is removed only after both canonical projections land.
        atomic_write_json(bootstrap_path, {"manifest": self.manifest, "state": self.state})
        atomic_write_json(self.state_path, self.state)
        atomic_write_json(self.manifest_path, self.manifest)
        bootstrap_path.unlink(missing_ok=True)
        _fsync_parent(self.run_dir)

    def load(self, *, repair: bool = True) -> None:
        self._require_lock()
        bootstrap_path = self.run_dir / BOOTSTRAP_NAME
        if bootstrap_path.exists():
            bootstrap = _read_json(bootstrap_path)
            manifest = bootstrap.get("manifest")
            state = bootstrap.get("state")
            if not isinstance(manifest, dict) or not isinstance(state, dict):
                raise BundleError("durable bundle bootstrap is malformed")
            _validate_schema(manifest, "run-bundle.schema.json", "manifest")
            _validate_schema(state, "run-state.schema.json", "state")
            atomic_write_json(self.state_path, state)
            atomic_write_json(self.manifest_path, manifest)
            bootstrap_path.unlink(missing_ok=True)
            _fsync_parent(self.run_dir)
        if not self.manifest_path.exists():
            raise BundleError(
                "this is a legacy run without a durable v1 boundary; use --from for legacy "
                "surgical reruns or start a new run (legacy runs cannot use --resume)"
            )
        manifest = _read_json(self.manifest_path)
        schema = manifest.get("schema")
        if schema != MANIFEST_SCHEMA:
            raise BundleError(f"unsupported durable bundle schema {schema!r}; refusing legacy fallback")
        state = _read_json(self.state_path)
        if state.get("schema") != STATE_SCHEMA:
            raise BundleError(f"unsupported durable state schema {state.get('schema')!r}")
        self.manifest, self.state = manifest, state
        self._projection_failed = False
        self._validate_documents()
        if repair:
            self.repair_trace()
        # A crash after state replacement but before manifest replacement leaves
        # state authoritative. Reconcile only after trace/state consistency holds.
        if self.manifest["status"] != self.state["status"]:
            self.manifest["status"] = self.state["status"]
            self.manifest["updated_at"] = self.state["updated_at"]
            if self.state["status"] == "completed" and not self.manifest.get("completed_at"):
                self.manifest["completed_at"] = self.state["updated_at"]
            atomic_write_json(self.manifest_path, self.manifest)

    def _validate_documents(self) -> None:
        assert self.manifest is not None and self.state is not None
        manifest, state = self.manifest, self.state
        _validate_schema(manifest, "run-bundle.schema.json", "manifest")
        _validate_schema(state, "run-state.schema.json", "state")
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id or state.get("run_id") != run_id:
            raise BundleError("manifest/state run ids differ")
        if run_id != self.run_dir.name:
            raise BundleError("bundle run id does not match its directory")
        if manifest.get("status") not in RUN_STATUSES or state.get("status") not in RUN_STATUSES:
            raise BundleError("bundle has an invalid run status")
        if not isinstance(state.get("trace_seq"), int) or state["trace_seq"] < 0:
            raise BundleError("bundle has an invalid committed trace sequence")
        if not isinstance(state.get("resume_count"), int) or state["resume_count"] < 0:
            raise BundleError("bundle has an invalid resume count")
        steps = state.get("steps")
        if not isinstance(steps, dict):
            raise BundleError("bundle state steps must be an object")
        for sid, value in steps.items():
            if not isinstance(sid, str) or not isinstance(value, dict) or value.get("status") not in STEP_STATUSES:
                raise BundleError(f"bundle has invalid state for step {sid!r}")
        workflow = manifest.get("workflow")
        if not isinstance(workflow, dict) or workflow.get("snapshot") != SNAPSHOT_NAME:
            raise BundleError("bundle has an invalid workflow record")
        _require_sha(workflow.get("sha256"), "manifest.workflow.sha256")
        input_record = manifest.get("input")
        if input_record is not None:
            if not isinstance(input_record, dict) or input_record.get("path") != "input.txt":
                raise BundleError("bundle has an invalid input record")
            _require_sha(input_record.get("sha256"), "manifest.input.sha256")

    def repair_trace(self) -> None:
        """Repair only the bounded append/commit crash windows, otherwise fail closed."""
        self._require_lock()
        assert self.manifest is not None and self.state is not None
        try:
            raw = self.trace_path.read_bytes()
        except OSError as error:
            raise BundleError(f"durable trace is unreadable: {error}") from error
        committed = self.state["trace_seq"]
        # State is the commit boundary. Validate exactly that prefix; bytes after
        # it were appended before a crash and are discarded without interpretation.
        parts = raw.splitlines(keepends=True)
        if len(parts) < committed:
            raise BundleError(
                f"durable trace ends at {len(parts)} before committed state sequence {committed}"
            )
        target = 0
        for expected in range(1, committed + 1):
            part = parts[expected - 1]
            if not part.endswith(b"\n"):
                raise BundleError(
                    f"durable trace event {expected} is torn before the committed boundary"
                )
            target += len(part)
            try:
                event = json.loads(part[:-1])
            except (UnicodeDecodeError, ValueError) as error:
                raise BundleError(f"durable trace corruption at committed event {expected}: {error}") from error
            if not isinstance(event, dict):
                raise BundleError(f"durable trace event {expected} is not an object")
            _validate_schema(event, "trace-event.schema.json", f"trace event {expected}")
            if event.get("run_id") != self.manifest["run_id"]:
                raise BundleError(f"durable trace event {expected} has a different run id")
            if event.get("seq") != expected:
                raise BundleError(f"durable trace sequence is not contiguous at event {expected}")
        # Drop a torn final fragment and any complete append not committed by state.
        if len(raw) != target:
            descriptor = os.open(self.trace_path, os.O_WRONLY)
            try:
                os.ftruncate(descriptor, target)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_parent(self.run_dir)

    def verify_integrity(self) -> None:
        """Verify frozen workflow/input fingerprints and trace/state consistency."""
        assert self.manifest is not None and self.state is not None
        self._validate_documents()
        self.repair_trace()
        workflow = self.manifest["workflow"]
        try:
            frozen = self.snapshot_path.read_bytes()
        except OSError as error:
            raise BundleError(f"frozen workflow snapshot is unreadable: {error}") from error
        if len(frozen) != workflow.get("bytes") or sha256_bytes(frozen) != workflow["sha256"]:
            raise BundleError("frozen workflow snapshot fingerprint mismatch")
        input_record = self.manifest.get("input")
        input_path = self.run_dir / "input.txt"
        if input_record is None:
            if input_path.exists():
                raise BundleError("run has an unrecorded input.txt")
        else:
            try:
                data = input_path.read_bytes()
            except OSError as error:
                raise BundleError(f"immutable run input is unreadable: {error}") from error
            if len(data) != input_record.get("bytes") or sha256_bytes(data) != input_record["sha256"]:
                raise BundleError("immutable run input fingerprint mismatch (cannot be forced)")

    def check_resume_source(self, source_bytes: bytes, *, force_drift: bool) -> None:
        assert self.manifest is not None
        expected = self.manifest["workflow"]["sha256"]
        actual = sha256_bytes(source_bytes)
        if actual == expected:
            return
        if not force_drift:
            raise BundleError(
                f"workflow source drift: run froze {expected}, current source is {actual}; "
                "review the change and pass --force-drift to execute the current source while preserving the original snapshot"
            )
        drift = {"at": utc_now(), "expected_sha256": expected, "actual_sha256": actual}
        self.record("workflow_drift_forced", payload=drift, manifest_drift=drift)

    def record(
        self,
        event_type: str,
        *,
        step_id: str | None = None,
        payload: dict[str, Any] | None = None,
        step_status: str | None = None,
        step_reason: str | None = None,
        deterministic: bool | None = None,
        step_updates: dict[str, dict[str, Any]] | None = None,
        run_status: str | None = None,
        increment_resume: bool = False,
        manifest_drift: dict[str, Any] | None = None,
    ) -> int:
        """Append+fsync event, atomically commit state sequence, then manifest."""
        self._require_lock()
        with self._mutex:
            if self._projection_failed:
                raise BundleError("bundle projection previously failed; close and reload before recording")
            assert self.manifest is not None and self.state is not None
            if step_status is not None and step_status not in STEP_STATUSES:
                raise BundleError(f"invalid step status {step_status!r}")
            if run_status is not None and run_status not in RUN_STATUSES:
                raise BundleError(f"invalid run status {run_status!r}")
            now = utc_now()
            seq = self.state["trace_seq"] + 1
            event: dict[str, Any] = {
                "schema": TRACE_SCHEMA, "seq": seq, "timestamp": now,
                "run_id": self.manifest["run_id"], "type": event_type,
                "payload": payload or {},
            }
            if step_id is not None:
                event["step_id"] = step_id
            line = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
            with self.trace_path.open("ab", buffering=0) as trace:
                trace.write(line)
                os.fsync(trace.fileno())

            new_state = copy.deepcopy(self.state)
            new_manifest = copy.deepcopy(self.manifest)
            new_state["trace_seq"] = seq
            new_state["updated_at"] = now
            if increment_resume:
                new_state["resume_count"] += 1
            updates = copy.deepcopy(step_updates or {})
            if step_id is not None and step_status is not None:
                updates[step_id] = {"status": step_status}
                if step_reason:
                    updates[step_id]["reason"] = step_reason
                if deterministic is not None:
                    updates[step_id]["deterministic"] = deterministic
            for sid, update in updates.items():
                if sid not in new_state["steps"]:
                    raise BundleError(f"trace update names unknown step {sid!r}")
                status = update.get("status")
                if status not in STEP_STATUSES:
                    raise BundleError(f"invalid step status {status!r}")
                item = {"status": status, "updated_at": now}
                if update.get("reason"):
                    item["reason"] = str(update["reason"])
                if "deterministic" in update:
                    item["deterministic"] = bool(update["deterministic"])
                new_state["steps"][sid] = item
            if run_status is not None:
                new_state["status"] = run_status
                new_manifest["status"] = run_status
                if run_status == "running" and new_manifest.get("started_at") is None:
                    new_manifest["started_at"] = now
                if run_status == "completed":
                    new_manifest["completed_at"] = now
            new_manifest["updated_at"] = now
            if manifest_drift is not None:
                new_manifest.setdefault("drift", []).append(manifest_drift)
            try:
                atomic_write_json(self.state_path, new_state)
            except Exception as error:
                # The trace append is intentionally ahead of the commit boundary.
                # Fail closed so no caller can reuse that uncommitted sequence;
                # reload truncates it back to the still-authoritative old state.
                self._projection_failed = True
                raise BundleError(
                    "state projection failed after trace append; close and reload"
                ) from error
            # State is the committed sequence authority as soon as replacement
            # succeeds. Never reuse its sequence if the manifest projection fails.
            self.state = new_state
            try:
                atomic_write_json(self.manifest_path, new_manifest)
            except Exception as error:
                self.manifest = new_manifest
                self._projection_failed = True
                raise BundleError(
                    "manifest projection failed after state commit; close and reload"
                ) from error
            self.manifest = new_manifest
            return seq

    def classify_released_running_as_interrupted(self) -> set[str]:
        """After acquiring the released fence, turn crash-left running nodes into interrupted."""
        assert self.state is not None
        running = {sid for sid, item in self.state["steps"].items() if item["status"] == "running"}
        if running or self.state.get("status") == "running":
            self.record(
                "run_interrupted_detected",
                payload={"steps": sorted(running), "reason": "owner lock released while running"},
                step_updates={sid: {"status": "interrupted", "reason": "previous owner exited"} for sid in running},
                run_status="interrupted",
            )
        return running

    def mark_interrupted(self, reason: str) -> None:
        if self.state is None or self.state.get("status") not in {"initialized", "running"}:
            return
        running = [sid for sid, item in self.state["steps"].items() if item["status"] == "running"]
        self.record(
            "run_interrupted",
            payload={"reason": reason, "steps": running},
            step_updates={sid: {"status": "interrupted", "reason": reason} for sid in running},
            run_status="interrupted",
        )
