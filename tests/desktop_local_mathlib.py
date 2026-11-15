#!/usr/bin/env python3
"""Desktop private Mathlib is explicit, offline-testable, and never warms a shared tree."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, round_storage, shared_mathlib


class DesktopLocalMathlibTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.logs = self.root / "logs"
        self.files = {
            "lakefile.toml": 'name = "TauCeti"\n',
            "lean-toolchain": "leanprover/lean4:fixture\n",
            "lake-manifest.json": json.dumps(
                {"name": "TauCeti", "packages": [{"name": "mathlib", "url": "https://example.invalid/mathlib"}]}
            ),
        }
        for name, contents in self.files.items():
            (self.cwd / name).write_text(contents)
        self.env = {
            "HOME": str(self.root / "home"),
            "TAUCETI_WORKER_ID": "desktop-fixture",
            "TAUCETI_PRIVATE_MATHLIB_BUILDS": "1",
            "TAUCETI_SHARED_MATHLIB_BUILDS": "off",
            "TAUCETI_SHARED_ARTIFACTS": "0",
            "TAUCETI_SHARED_MATHLIB_ROOT": str(self.root / "shared-pool"),
            "TAUCETI_BUILD_CACHE_ROOT": str(self.root / "build-pool"),
            "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
            "TAUCETI_VERIFY_STATE": str(self.root / "verification"),
            "TAUCETI_ROUND_PRIVATE_BYTES": "off",
            "LAKE_CACHE_DIR": str(self.root / "home" / "lake-cache"),
            "MATHLIB_CACHE_DIR": str(self.root / "home" / "mathlib-cache"),
        }
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(agents, "log"))
        self.enterContext(patch.object(agents, "report_failure"))
        self.enterContext(patch.object(agents, "verify_host_storage_headroom"))

    def assert_dependency_files_unchanged(self):
        for name, contents in self.files.items():
            self.assertEqual((self.cwd / name).read_text(), contents)

    @contextlib.contextmanager
    def forbid_shared_preparation(self):
        with (
            patch.object(shared_mathlib, "view", side_effect=AssertionError("private mode entered shared view")),
            patch.object(
                shared_mathlib, "rebase_target", side_effect=AssertionError("private mode rewrote dependency files")
            ),
            patch.object(shared_mathlib, "capability", side_effect=AssertionError("private mode probed overlays")),
        ):
            yield

    def cache_success(self, command, cwd, env, output, timeout):
        self.assertEqual(cwd, self.cwd)
        self.assertGreater(timeout, 0)
        self.assertNotIn("TAUCETI_QUEUE_MATHLIB", env)
        self.assert_dependency_files_unchanged()
        return 0

    def test_private_mode_requires_both_explicit_switches(self):
        self.assertTrue(agents.private_mathlib_enabled(self.env))
        for private in (None, "", "0", "true"):
            with self.subTest(private=private):
                env = dict(self.env)
                if private is None:
                    env.pop("TAUCETI_PRIVATE_MATHLIB_BUILDS")
                else:
                    env["TAUCETI_PRIVATE_MATHLIB_BUILDS"] = private
                self.assertFalse(agents.private_mathlib_enabled(env))
        for shared in (None, "", "auto", "required"):
            with self.subTest(shared=shared):
                env = dict(self.env)
                if shared is None:
                    env.pop("TAUCETI_SHARED_MATHLIB_BUILDS")
                else:
                    env["TAUCETI_SHARED_MATHLIB_BUILDS"] = shared
                self.assertFalse(agents.private_mathlib_enabled(env))

    def test_private_preparation_fetches_both_caches_without_shared_state(self):
        # Lazy restoration means successful fetches need not create .lake/build.
        with (
            self.forbid_shared_preparation(),
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(agents, "_cache_fetch", side_effect=self.cache_success) as fetch,
            patch.object(agents.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")),
        ):
            with agents.host_build_cache_context(self.cwd, self.env, self.logs) as prefix:
                self.assertFalse(prefix)
                self.assertFalse((self.cwd / ".lake/build").exists())
                self.assertNotIn("TAUCETI_SHARED_MATHLIB_VIEW", self.env)
                self.assertNotIn("TAUCETI_QUEUE_MATHLIB", self.env)
                config = Path(self.env["LAKE_CONFIG"])
                self.assertIn('name = "tauceti-public"', config.read_text())
            commands = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(commands[0], ["lake", "exe", "cache", "get"])
        self.assertEqual(commands[1][:3], ["lake", "cache", "get"])
        self.assertIn("--max-revs=100", commands[1])
        self.assertEqual(len(commands), 2)
        self.assertFalse(config.exists())
        self.assert_dependency_files_unchanged()

    def test_private_mode_without_tauceti_service_keeps_historical_no_warm_path(self):
        with (
            self.forbid_shared_preparation(),
            patch.object(agents, "TAUCETI_CACHE_SERVICE", ""),
            patch.object(agents, "_cache_fetch", side_effect=AssertionError("unexpected cache fetch")),
            patch.object(agents, "prepare_host_build_caches", side_effect=AssertionError("unexpected cache warm")),
        ):
            with agents.host_build_cache_context(self.cwd, self.env, self.logs) as prefix:
                self.assertFalse(prefix)
                self.assertNotIn("LAKE_CONFIG", self.env)
                self.assertNotIn("TAUCETI_QUEUE_MATHLIB", self.env)
        self.assert_dependency_files_unchanged()

    def test_shared_tree_or_inherited_view_blocks_private_extraction_without_changes(self):
        source = self.root / "shared-pool" / shared_mathlib.LAYOUT / "sources" / "revision" / "mathlib"
        source.mkdir(parents=True)
        sentinel = source / "preserve.lean"
        sentinel.write_text("immutable shared source\n")
        packages = self.cwd / ".lake/packages"
        packages.mkdir(parents=True)
        link = packages / "mathlib"
        marker = self.cwd / ".lake/tauceti-mathlib/view.json"
        marker.parent.mkdir(parents=True)
        for service in ("tauceti-public", ""):
            for state in ("attached", "marker", "empty-marker", "corrupt-marker", "linked-marker", "environment"):
                with self.subTest(service=service, state=state):
                    env = dict(self.env)
                    if state == "attached":
                        link.symlink_to(source, target_is_directory=True)
                    elif state == "marker":
                        marker.write_text(json.dumps({"key": "leased-key", "rev": "revision"}))
                    elif state == "empty-marker":
                        marker.write_text("{}")
                    elif state == "corrupt-marker":
                        marker.write_text("{broken shared receipt")
                    elif state == "linked-marker":
                        marker.symlink_to(self.root / "missing-shared-receipt.json")
                    else:
                        env["TAUCETI_SHARED_MATHLIB_VIEW"] = '["bwrap"]'
                    with (
                        self.forbid_shared_preparation(),
                        patch.object(agents, "TAUCETI_CACHE_SERVICE", service),
                        patch.object(agents, "_cache_fetch", side_effect=AssertionError("warmed a shared tree")),
                        self.assertRaises(agents.BuildCacheUnavailable),
                        agents.host_build_cache_context(self.cwd, env, self.logs),
                    ):
                        self.fail("unsafe private context was admitted")
                    self.assertEqual(sentinel.read_text(), "immutable shared source\n")
                    self.assert_dependency_files_unchanged()
                    if state == "attached":
                        self.assertTrue(link.is_symlink())
                        self.assertEqual(link.resolve(), source)
                        link.unlink()
                    elif "marker" in state:
                        if state == "marker":
                            self.assertEqual(json.loads(marker.read_text()), {"key": "leased-key", "rev": "revision"})
                        elif state == "empty-marker":
                            self.assertEqual(marker.read_text(), "{}")
                        elif state == "corrupt-marker":
                            self.assertEqual(marker.read_text(), "{broken shared receipt")
                        else:
                            self.assertTrue(marker.is_symlink())
                        marker.unlink()

    def test_direct_cache_warming_rejects_inherited_shared_state(self):
        for key, value in (
            ("TAUCETI_SHARED_MATHLIB_VIEW", '["bwrap"]'),
            ("TAUCETI_SHARED_MATHLIB_KEY", "leased-key"),
            ("TAUCETI_SHARED_MATHLIB_LEASE_FD", "42"),
        ):
            with self.subTest(key=key):
                env = {**self.env, key: value}
                with (
                    patch.object(agents, "_cache_fetch", side_effect=AssertionError("extracted into inherited view")),
                    self.assertRaises(agents.BuildCacheUnavailable),
                ):
                    agents.warm_host_build_caches(self.cwd, env, self.logs)
                self.assertEqual(env[key], value)
                self.assert_dependency_files_unchanged()

    def test_mathlib_transport_failure_never_reaches_agent_or_source_build(self):
        commands = []

        def failure(command, _cwd, _env, output, _timeout):
            commands.append(command)
            output.write(b"error: HTTP response status code 503\n")
            return 1

        with (
            self.forbid_shared_preparation(),
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(agents, "_cache_fetch", side_effect=failure),
            self.assertRaises(agents.BuildCacheUnavailable),
            agents.host_build_cache_context(self.cwd, self.env, self.logs),
        ):
            self.fail("agent context admitted after failed Mathlib cache transfer")
        self.assertEqual(commands, [["lake", "exe", "cache", "get"]] * 2)
        self.assert_dependency_files_unchanged()

    def test_off_without_private_opt_in_still_blocks_mathlib_rounds(self):
        for service in ("tauceti-public", ""):
            with self.subTest(service=service):
                env = dict(self.env)
                env.pop("TAUCETI_PRIVATE_MATHLIB_BUILDS")
                with (
                    patch.object(agents, "TAUCETI_CACHE_SERVICE", service),
                    patch.object(shared_mathlib, "capability", side_effect=AssertionError("off probed overlays")),
                    patch.object(agents, "_cache_fetch", side_effect=AssertionError("off fetched private cache")),
                    self.assertRaisesRegex(agents.BuildCacheUnavailable, "switched off"),
                    agents.host_build_cache_context(self.cwd, env, self.logs),
                ):
                    self.fail("ordinary sharing-off policy stopped blocking")

    def test_shared_mode_retains_view_and_never_extracts_private_mathlib(self):
        env = {**self.env, "TAUCETI_SHARED_MATHLIB_BUILDS": "required"}
        restored = []

        @contextlib.contextmanager
        def shared_view(*_args):
            yield ["env"]

        @contextlib.contextmanager
        def rebase_target(*_args):
            yield lambda: restored.append(True)

        with (
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(shared_mathlib, "view", side_effect=shared_view) as view,
            patch.object(shared_mathlib, "rebase_target", side_effect=rebase_target) as rebase,
            patch.object(shared_mathlib, "preparation_status"),
            patch.object(agents, "_cache_fetch", side_effect=self.cache_success) as fetch,
            patch.object(agents.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")),
        ):
            with agents.host_build_cache_context(self.cwd, env, self.logs) as prefix:
                self.assertEqual(prefix, ["env"])
                self.assertEqual(json.loads(env["TAUCETI_SHARED_MATHLIB_VIEW"]), ["env"])
            view.assert_called_once()
            rebase.assert_called_once()
        self.assertEqual(restored, [True])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0][:4], ["env", "lake", "cache", "get"])
        self.assertNotIn("TAUCETI_SHARED_MATHLIB_VIEW", env)

    def prompt_for(self, *, private, cache_note=True):
        env = dict(self.env)
        if not private:
            env.pop("TAUCETI_PRIVATE_MATHLIB_BUILDS")
            env["TAUCETI_SHARED_MATHLIB_BUILDS"] = "required"
        captured = []

        def launch(prompt, _profile):
            captured.append(prompt)
            return ["offline-agent-placeholder"], dict(env), None

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(agents, "TAUCETI_CACHE_SERVICE", "tauceti-public"),
            patch.object(agents, "host_agent_launch", side_effect=launch),
            patch.object(agents, "host_build_cache_context", return_value=contextlib.nullcontext([])),
            patch.object(agents, "run_agent_proc", return_value=0),
            patch.object(agents, "relink_build_outputs"),
            patch.object(round_storage, "start", return_value=None),
        ):
            self.assertEqual(
                agents.run_agent_host(self.cwd, "fixture task", "codex", self.logs, cache_note=cache_note), 0
            )
        return captured[0]

    def test_private_prompt_has_cache_commands_and_lazy_restoration_guidance(self):
        prompt = self.prompt_for(private=True)
        self.assertIn("lake exe cache get", prompt)
        self.assertIn("lake cache get --service tauceti-public", prompt)
        self.assertIn("lake build --no-build <existing-module>", prompt)
        self.assertIn("an absent .lake/build directory alone is not a cache failure", prompt)
        self.assertIn("404 before an older compatible revision succeeds", prompt)
        self.assertIn("let it finish", prompt)
        self.assertNotIn("Never run `lake exe cache get`", prompt)
        self.assertNotIn("shared, read-only build", prompt)

    def test_shared_prompt_keeps_shared_build_protection(self):
        prompt = self.prompt_for(private=False)
        self.assertIn("shared, read-only build", prompt)
        self.assertIn("Never run `lake exe cache get` or `lake update`", prompt)
        self.assertIn("lake build --no-build <existing-module>", prompt)
        self.assertEqual(self.prompt_for(private=True, cache_note=False), "fixture task")


if __name__ == "__main__":
    unittest.main()
