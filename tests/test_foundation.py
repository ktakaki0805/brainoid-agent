"""Acceptance checks for setup failures, installed packages and ignored credentials."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_toolchain import ROOT, check


class ToolchainTests(unittest.TestCase):
    def test_wrong_node_is_rejected_with_recovery_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "node"
            executable.write_text("#!/bin/sh\nprintf 'v0.0.0\\n'\n")
            executable.chmod(0o755)
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/check_toolchain.py")],
                env={**os.environ, "PATH": f"{temporary}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("node: required", result.stderr)
        self.assertIn("found 0.0.0", result.stderr)
        self.assertIn("mise install", result.stderr)

    def test_missing_tools_fail_closed(self) -> None:
        with patch("scripts.check_toolchain.subprocess.run", side_effect=FileNotFoundError):
            errors = check()
        self.assertEqual(len(errors), 4)
        self.assertTrue(all("cannot run --version" in error for error in errors))


class WorkspaceTests(unittest.TestCase):
    def test_packages_import_outside_checkout_without_pythonpath(self) -> None:
        # -I removes cwd/PYTHONPATH: only installed package APIs can satisfy these imports.
        source = "\n".join(
            [
                "import brainoid_api, brainoid_worker, brainoid_device",
                "import brainoid_agent_core, brainoid_memory, brainoid_tools",
                "from fastapi import FastAPI",
                "from pydantic import BaseModel",
                "import uvicorn",
                "assert FastAPI().openapi()['openapi'].startswith('3.')",
                "class Smoke(BaseModel): value: int",
                "assert Smoke(value=1).model_dump() == {'value': 1}",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run([sys.executable, "-I", "-c", source], cwd=temporary, check=True)

    def test_node_packages_resolve_public_exports_and_reject_private_paths(self) -> None:
        source = """
import assert from 'node:assert/strict';
await import('@brainoid-internal/ui');
await import('@brainoid-internal/api-client');
await import('react');
await import('react-dom');
await import('@capacitor/core');
await import('vite');
await assert.rejects(import('@brainoid-internal/ui/src/index.ts'), {
  code: 'ERR_PACKAGE_PATH_NOT_EXPORTED',
});
"""
        subprocess.run(
            ["node", "--input-type=module", "-e", source], cwd=ROOT / "apps/mobile", check=True
        )

    def test_secret_files_are_ignored_but_examples_are_shareable(self) -> None:
        paths = [
            ".env",
            "apps/mobile/.env.local",
            "services/api/credential.key",
            "infra/gcp/service-account-dev.json",
            "secrets/local.json",
            "device.p12",
            "AuthKey_test.p8",
            "release.keystore",
            "id_ed25519",
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin"],
            cwd=ROOT,
            input="\n".join(paths) + "\n.env.example\napps/mobile/.env.example\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(result.stdout.splitlines()), set(paths))


if __name__ == "__main__":
    unittest.main()
