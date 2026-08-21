from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema.validators import validator_for


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import version_info  # noqa: E402


class VersionInfoTests(unittest.TestCase):
    def copy_product(self, destination: Path) -> None:
        for relative in version_info.product_inventory(ROOT):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)

    def test_metadata_versions_match_authoritative_version(self) -> None:
        authoritative = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        versions = version_info.metadata_versions(ROOT)
        self.assertEqual(set(versions.values()), {authoritative})

    def test_product_digest_is_content_bound_and_rejects_inventory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = Path(raw) / "product"
            self.copy_product(product)
            original = version_info.product_digest(product)
            (product / "README.md").write_text("tampered\n", encoding="utf-8")
            self.assertNotEqual(version_info.product_digest(product), original)
            (product / "README.md").unlink()
            (product / "README.md").symlink_to(ROOT / "README.md")
            with self.assertRaisesRegex(version_info.VersionInfoError, "rejects symlink"):
                version_info.product_digest(product)

    def test_install_manifest_binds_exact_bytes_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = Path(raw) / "product"
            self.copy_product(product)
            manifest = version_info.write_install_manifest(
                product, ROOT, product / "install-manifest.json")
            identity = version_info.build_version_info(product, default_compare=False)
            self.assertTrue(identity["ok"])
            self.assertTrue(identity["install"]["manifest_valid"])
            self.assertEqual(identity["executing"]["tree_sha256"], manifest["installed_tree_sha256"])
            (product / "README.md").write_text("tampered\n", encoding="utf-8")
            changed = version_info.build_version_info(product, default_compare=False)
            self.assertFalse(changed["ok"])
            self.assertIn("installed_tampered", {item["code"] for item in changed["drift"]})

    def test_version_json_is_one_document_and_reports_explicit_invalid_root(self) -> None:
        healthy = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "piw.py"), "version", "--json"],
            capture_output=True, text=True, check=False,
            env={"PATH": str(Path(sys.executable).parent), "PI_GRAPH_HOME": "/nonexistent"},
        )
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        payload = json.loads(healthy.stdout)
        self.assertEqual(payload["schema"], "pi-graph.version.v1")
        self.assertTrue(payload["ok"])
        self.assertEqual(healthy.stdout.count("\n"), 1)

        invalid = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "piw.py"), "version",
             "--compare-root", "/nonexistent", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_root")

    def test_optimization_schemas_are_valid_and_closed_at_the_root(self) -> None:
        schemas = sorted((ROOT / "schemas").glob("optimization-*.schema.json"))
        self.assertEqual(len(schemas), 6)
        for path in schemas:
            schema = json.loads(path.read_text(encoding="utf-8"))
            validator = validator_for(schema)
            validator.check_schema(schema)
            errors = list(validator(schema).iter_errors({"unexpected": True}))
            self.assertTrue(errors, path.name)
            self.assertFalse(schema.get("additionalProperties", True), path.name)


if __name__ == "__main__":
    unittest.main()
