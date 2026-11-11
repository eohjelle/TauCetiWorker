#!/usr/bin/env python3
"""Desktop handoff tests use only synthetic credentials and temporary directories."""

import base64
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_credentials", ROOT / "scripts" / "prepare_desktop_credentials.py")
assert spec is not None and spec.loader is not None
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


class DesktopCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.codex = root / "desktop-codex" / "auth.json"
        self.codex.parent.mkdir()
        self.gh = root / "desktop-gh"
        self.gh.mkdir()
        (self.gh / "hosts.yml").write_text("github.com:\n  oauth_token: fixture-github-token\n")
        (self.gh / "config.yml").write_text("git_protocol: https\n")
        (self.gh / "unrelated.txt").write_text("must not be copied")
        self.destination = root / "container-credentials"
        self.write_auth()

    def write_auth(self, expiry=None):
        expiry = time.time() + 60 if expiry is None else expiry
        payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
        self.codex.write_text(json.dumps({"tokens": {"access_token": f"header.{payload}.signature", "refresh_token": "fixture-renewable-token", "id_token": "fixture-id-token", "account_id": "fixture-account"}}))

    def prepare(self):
        handoff.prepare(self.codex, self.gh, self.destination)

    def test_access_only_private_files_without_refresh_or_source_changes(self):
        before = self.codex.read_bytes()
        with patch.object(handoff.oauth, "refresh_if_due", side_effect=AssertionError("must not refresh")), patch.object(handoff.oauth, "_post_json", side_effect=AssertionError("must not contact provider")):
            self.prepare()
        mirror = self.destination / "codex-worker" / "auth.json"
        data = json.loads(mirror.read_text())
        self.assertEqual(data["tokens"]["refresh_token"], handoff.oauth.CODEX_REFRESH_PLACEHOLDER)
        self.assertNotIn("fixture-renewable-token", mirror.read_text())
        self.assertEqual(self.codex.read_bytes(), before)
        self.assertEqual({path.name for path in (self.destination / "gh").iterdir()}, {"hosts.yml", "config.yml"})
        for path in (self.destination, *self.destination.iterdir()):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in self.destination.glob("*/*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_new_access_token_replaces_file_without_replacing_bound_directory(self):
        self.prepare()
        directory = self.destination / "codex-worker"
        directory_inode = directory.stat().st_ino
        mirror = directory / "auth.json"
        old_inode = mirror.stat().st_ino
        self.write_auth(time.time() + 120)
        self.prepare()
        self.assertEqual(directory.stat().st_ino, directory_inode)
        self.assertNotEqual(mirror.stat().st_ino, old_inode)
        self.assertEqual(json.loads(mirror.read_text())["tokens"]["access_token"], json.loads(self.codex.read_text())["tokens"]["access_token"])

    def test_expired_or_unreadable_expiry_does_not_publish(self):
        for token in ("header.eyJleHAiOjF9.signature", "not-a-jwt"):
            with self.subTest(token=token):
                self.codex.write_text(json.dumps({"tokens": {"access_token": token}}))
                with self.assertRaises(handoff.PreparationError):
                    self.prepare()
                self.assertFalse(self.destination.exists())

    def test_missing_github_file_prevents_partial_publication(self):
        (self.gh / "hosts.yml").unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_removed_optional_config_is_not_left_stale(self):
        self.prepare()
        (self.gh / "config.yml").unlink()
        self.prepare()
        self.assertFalse((self.destination / "gh" / "config.yml").exists())

    def test_source_overlap_or_destination_symlink_is_rejected(self):
        before = self.codex.read_bytes()
        with self.assertRaises(handoff.PreparationError):
            handoff.prepare(self.codex, self.gh, self.codex.parent)
        self.destination.mkdir()
        (self.destination / "codex-worker").symlink_to(self.codex.parent, target_is_directory=True)
        with self.assertRaises(handoff.PreparationError):
            self.prepare()
        self.assertEqual(self.codex.read_bytes(), before)

    def test_cli_failure_never_prints_credential_content(self):
        self.codex.write_text("fixture-secret-that-is-not-json")
        errors = io.StringIO()
        argv = ["prepare", "--codex-source", str(self.codex), "--gh-source", str(self.gh), "--destination", str(self.destination)]
        with patch.object(sys, "argv", argv), redirect_stderr(errors):
            self.assertEqual(handoff.main(), 1)
        self.assertNotIn("fixture-secret", errors.getvalue())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
