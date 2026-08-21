#!/usr/bin/env python3
"""Deterministic pi-graph product and source/install identity.

The product digest covers only the public, bounded inventory approved by the
version contract.  Paths and exact file bytes are hashed; the install manifest
is deliberately outside that inventory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - pi-graph requires Python 3.10
    tomllib = None  # type: ignore[assignment]

VERSION_SCHEMA = "pi-graph.version.v1"
INSTALL_SCHEMA = "pi-graph.install-manifest.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
MAX_PRODUCT_FILE_BYTES = 16 * 1024 * 1024
_FIXED_FILES = (
    "VERSION", "extensions/pi-graph.ts", "extensions/pi-graph.test.mjs",
    "package.json", "package-lock.json", "pyproject.toml", "requirements.txt",
    "README.md", "SKILL.md", "AGENTS.md",
)


class VersionInfoError(RuntimeError):
    """Identity cannot be read safely."""


def _sha256_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise VersionInfoError(f"cannot inspect product file {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise VersionInfoError(f"product file is not regular: {path}")
    if before.st_size > MAX_PRODUCT_FILE_BYTES:
        raise VersionInfoError(f"product file exceeds {MAX_PRODUCT_FILE_BYTES} bytes: {path}")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise VersionInfoError(f"cannot open product file {path}: {error}") from error
    digest = hashlib.sha256()
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise VersionInfoError(f"product file identity changed: {path}")
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PRODUCT_FILE_BYTES:
                raise VersionInfoError(f"product file exceeds {MAX_PRODUCT_FILE_BYTES} bytes: {path}")
            digest.update(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise VersionInfoError(f"product file changed while hashing: {path}")
    finally:
        os.close(fd)
    return digest.hexdigest()


def _regular_product_file(root: Path, relative: str, required: bool = True) -> Path | None:
    path = root / relative
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if required:
            raise VersionInfoError(f"product file is missing: {relative}")
        return None
    if stat.S_ISLNK(mode):
        raise VersionInfoError(f"product inventory rejects symlink: {relative}")
    if not stat.S_ISREG(mode):
        raise VersionInfoError(f"product path is not a regular file: {relative}")
    if path.lstat().st_size > MAX_PRODUCT_FILE_BYTES:
        raise VersionInfoError(f"product file exceeds {MAX_PRODUCT_FILE_BYTES} bytes: {relative}")
    return path


def _directory_files(root: Path, relative: str, recursive: bool, suffix: str | None) -> list[str]:
    base = root / relative
    try:
        base_mode = base.lstat().st_mode
    except FileNotFoundError as error:
        raise VersionInfoError(f"product directory is missing: {relative}") from error
    if stat.S_ISLNK(base_mode) or not stat.S_ISDIR(base_mode):
        raise VersionInfoError(f"product directory is not a real directory: {relative}")

    found: list[str] = []
    if recursive:
        for current, directories, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            for name in list(directories):
                child = current_path / name
                if child.is_symlink():
                    raise VersionInfoError(
                        f"product inventory rejects symlink: {child.relative_to(root).as_posix()}"
                    )
            for name in files:
                child = current_path / name
                rel = child.relative_to(root).as_posix()
                if suffix is not None and not name.endswith(suffix):
                    continue
                _regular_product_file(root, rel)
                found.append(rel)
    else:
        for child in base.iterdir():
            if suffix is not None and not child.name.endswith(suffix):
                continue
            rel = child.relative_to(root).as_posix()
            _regular_product_file(root, rel)
            found.append(rel)
    return found


def product_inventory(root: Path | str) -> list[str]:
    """Return the sorted bounded product inventory, rejecting included symlinks."""
    root = Path(root).expanduser().resolve()
    paths = list(_FIXED_FILES)
    for relative in _FIXED_FILES:
        _regular_product_file(root, relative)
    paths.extend(_directory_files(root, "bin", recursive=True, suffix=None))
    paths.extend(_directory_files(root, "scripts", recursive=False, suffix=".py"))
    paths.extend(_directory_files(root, "schemas", recursive=False, suffix=".json"))
    paths.extend(_directory_files(root, "actions", recursive=False, suffix=".yaml"))
    paths.extend(_directory_files(root, "docs", recursive=True, suffix=".md"))
    return sorted(set(paths))


def product_digest(root: Path | str) -> str:
    """Hash normalized relative paths and each file's exact byte digest."""
    root = Path(root).expanduser().resolve()
    entries = [
        {"path": relative, "sha256": _sha256_file(root / relative)}
        for relative in product_inventory(root)
    ]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(("pi-graph-product-v1\n" + canonical).encode("utf-8")).hexdigest()


def product_version(root: Path | str) -> str:
    path = _regular_product_file(Path(root).expanduser().resolve(), "VERSION")
    assert path is not None
    version = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise VersionInfoError("VERSION is not a valid product version")
    return version


def metadata_versions(root: Path | str) -> dict[str, str | None]:
    root = Path(root).expanduser().resolve()
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")) if tomllib else {}
    except (OSError, ValueError, TypeError) as error:
        raise VersionInfoError(f"package version metadata is unreadable: {error}") from error
    packages = lock.get("packages") if isinstance(lock, dict) else None
    root_lock = packages.get("") if isinstance(packages, dict) else None
    return {
        "VERSION": product_version(root),
        "package.json": package.get("version") if isinstance(package, dict) else None,
        "package-lock.json": lock.get("version") if isinstance(lock, dict) else None,
        "package-lock.json#packages": root_lock.get("version") if isinstance(root_lock, dict) else None,
        "pyproject.toml": ((pyproject.get("project") or {}).get("version")
                           if isinstance(pyproject, dict) else None),
    }


def _git_identity(root: Path) -> tuple[str | None, bool | None]:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            return None, None
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if revision.returncode != 0 or status_result.returncode != 0:
            return None, None
        return revision.stdout.strip(), bool(status_result.stdout)
    except (OSError, subprocess.SubprocessError):
        return None, None


def _command_version(name: str) -> str | None:
    executable = shutil.which(name)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=5, check=False,
        )
        line = (result.stdout or result.stderr).strip().splitlines()
        return line[0] if line else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, str(error)
    required = {
        "schema", "product_version", "source_revision", "source_dirty",
        "source_tree_sha256", "installed_tree_sha256", "installed_at",
        "install_id", "source_root",
    }
    if not isinstance(value, dict) or set(value) != required:
        return None, "manifest fields do not match pi-graph.install-manifest.v1"
    if value.get("schema") != INSTALL_SCHEMA:
        return None, "manifest schema is unsupported"
    if not all(SHA256_RE.fullmatch(str(value.get(key, "")))
               for key in ("source_tree_sha256", "installed_tree_sha256")):
        return None, "manifest digest is invalid"
    if value.get("source_revision") is not None and not GIT_REVISION_RE.fullmatch(str(value["source_revision"])):
        return None, "manifest source revision is invalid"
    if not isinstance(value.get("source_dirty"), bool):
        return None, "manifest source_dirty is invalid"
    for key in ("product_version", "installed_at", "install_id", "source_root"):
        if not isinstance(value.get(key), str) or not value[key]:
            return None, f"manifest {key} is invalid"
    return value, None


def _drift(code: str, message: str, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if expected is not None:
        item["expected"] = expected
    if actual is not None:
        item["actual"] = actual
    return item


def root_identity(root: Path | str, *, expected_install: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise VersionInfoError(f"comparison root is not a directory: {root}")
    version = product_version(root)
    tree = product_digest(root)
    revision, dirty = _git_identity(root)
    manifest_path = root / "install-manifest.json"
    manifest: dict[str, Any] | None = None
    manifest_error: str | None = None
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            manifest_error = "install manifest must be a regular non-symlink file"
        else:
            manifest, manifest_error = _read_manifest(manifest_path)

    kind = "installed" if manifest is not None or manifest_error is not None or expected_install else "source"
    drift: list[dict[str, Any]] = []
    versions = metadata_versions(root)
    mismatched = {key: value for key, value in versions.items() if value != version}
    if mismatched:
        drift.append(_drift(
            "metadata_version_mismatch", "package metadata does not match VERSION",
            version, mismatched,
        ))
    if kind == "installed" and manifest is None:
        code = "manifest_invalid" if manifest_error else "manifest_missing"
        message = manifest_error or "installed product has no install-manifest.json"
        drift.append(_drift(code, message))
    if manifest is not None:
        revision = manifest["source_revision"]
        dirty = manifest["source_dirty"]
        if manifest["product_version"] != version:
            drift.append(_drift(
                "metadata_version_mismatch", "install manifest version does not match VERSION",
                manifest["product_version"], version,
            ))
        if manifest["installed_tree_sha256"] != tree:
            drift.append(_drift(
                "installed_tampered", "installed product bytes differ from the install manifest",
                manifest["installed_tree_sha256"], tree,
            ))

    identity = {
        "root": str(root),
        "product_version": version,
        "revision": revision,
        "dirty": dirty,
        "tree_sha256": tree,
        "kind": kind,
        "manifest_path": str(manifest_path) if kind == "installed" else None,
        "manifest_valid": manifest is not None if kind == "installed" else None,
        "manifest": manifest,
    }
    return identity, drift


def build_version_info(
    product_root: Path | str,
    compare_root: Path | str | None = None,
    *,
    default_compare: bool = True,
) -> dict[str, Any]:
    root = Path(product_root).expanduser().resolve()
    own_expected_install = (root / "install-manifest.json").exists()
    own, drift = root_identity(root, expected_install=own_expected_install)
    runner = root / "scripts" / "run_steps.py"
    batch = root / "scripts" / "run_batch.py"
    piw = root / "bin" / "piw"
    for path in (runner, batch, piw):
        if path.is_symlink() or not path.is_file():
            raise VersionInfoError(f"required product entry is not a regular file: {path.relative_to(root)}")

    comparison_expected_install = False
    explicit = compare_root is not None
    if compare_root is None and default_compare and own["kind"] == "source":
        candidate = Path(os.environ.get("PI_GRAPH_HOME", str(Path.home() / ".pi-graph"))).expanduser()
        if candidate.exists() and candidate.resolve() != root:
            compare_root = candidate
            comparison_expected_install = True
    elif explicit:
        comparison_expected_install = not (Path(compare_root).expanduser() / ".git").exists()

    comparison = None
    if compare_root is not None:
        comparison_path = Path(compare_root).expanduser().resolve()
        try:
            compared, compared_drift = root_identity(
                comparison_path, expected_install=comparison_expected_install)
        except VersionInfoError as error:
            if explicit:
                raise
            # A stale pre-manifest install must not make an otherwise healthy
            # source checkout's own identity unreadable. Report actionable
            # comparison drift instead; explicit invalid roots still fail.
            comparison = {
                "root": str(comparison_path), "product_version": None,
                "revision": None, "tree_sha256": None,
            }
            drift.append(_drift("manifest_missing", f"comparison product is unreadable: {error}"))
        else:
            comparison = {
                "root": compared["root"],
                "product_version": compared["product_version"],
                "revision": compared["revision"],
                "tree_sha256": compared["tree_sha256"],
            }
            drift.extend(compared_drift)
            if own["product_version"] != compared["product_version"]:
                drift.append(_drift("version", "product versions differ",
                                    own["product_version"], compared["product_version"]))
            if own["revision"] is not None and compared["revision"] is not None and own["revision"] != compared["revision"]:
                drift.append(_drift("revision", "source revisions differ",
                                    own["revision"], compared["revision"]))
            if own["tree_sha256"] != compared["tree_sha256"]:
                drift.append(_drift("tree", "product tree digests differ",
                                    own["tree_sha256"], compared["tree_sha256"]))

    # Keep one stable entry per exact finding while retaining independent codes.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in drift:
        key = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    own_integrity = not any(item["code"] in {
        "manifest_missing", "manifest_invalid", "installed_tampered", "metadata_version_mismatch"
    } for item in root_identity(root, expected_install=own_expected_install)[1])
    manifest = own["manifest"]
    return {
        "schema": VERSION_SCHEMA,
        "ok": own_integrity,
        "product_version": own["product_version"],
        "executing": {
            "root": own["root"],
            "resolved_piw": str(piw.resolve()),
            "revision": own["revision"],
            "dirty": own["dirty"],
            "tree_sha256": own["tree_sha256"],
            "runner_path": str(runner),
            "runner_sha256": _sha256_file(runner),
            "batch_path": str(batch),
            "batch_sha256": _sha256_file(batch),
        },
        "runtime": {
            "python": platform.python_version(),
            "node": _command_version("node"),
            "pi": _command_version("pi"),
            "platform": platform.platform(),
        },
        "install": {
            "kind": own["kind"],
            "manifest_path": own["manifest_path"],
            "manifest_valid": own["manifest_valid"],
            "install_id": manifest.get("install_id") if manifest else None,
            "installed_tree_sha256": manifest.get("installed_tree_sha256") if manifest else None,
            "self_integrity": own_integrity,
        },
        "comparison": comparison,
        "drift": unique,
    }


def write_install_manifest(root: Path | str, source_root: Path | str, output: Path | str) -> dict[str, Any]:
    """Atomically write a staged install manifest after verifying source parity."""
    root = Path(root).expanduser().resolve()
    source_root = Path(source_root).expanduser().resolve()
    output = Path(output).expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve(strict=False)
    if output.parent != root or output.name != "install-manifest.json":
        raise VersionInfoError("install manifest output must be ROOT/install-manifest.json")
    source_tree = product_digest(source_root)
    installed_tree = product_digest(root)
    if source_tree != installed_tree:
        raise VersionInfoError("staged product digest differs from source product digest")
    source_revision, source_dirty = _git_identity(source_root)
    manifest = {
        "schema": INSTALL_SCHEMA,
        "product_version": product_version(root),
        "source_revision": source_revision,
        "source_dirty": bool(source_dirty),
        "source_tree_sha256": source_tree,
        "installed_tree_sha256": installed_tree,
        "installed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "install_id": str(uuid.uuid4()),
        "source_root": str(source_root),
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install-manifest.", dir=str(output.parent))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute pi-graph product identity")
    sub = parser.add_subparsers(dest="command", required=True)
    digest_parser = sub.add_parser("digest")
    digest_parser.add_argument("--root", required=True)
    manifest_parser = sub.add_parser("write-manifest")
    manifest_parser.add_argument("--root", required=True)
    manifest_parser.add_argument("--source-root", required=True)
    manifest_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "digest":
            payload = {"tree_sha256": product_digest(args.root), "version": product_version(args.root)}
        else:
            payload = write_install_manifest(args.root, args.source_root, args.output)
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    except VersionInfoError as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
