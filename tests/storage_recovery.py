"""Standalone storage recovery uses no Lean installation and preserves source and live work."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cache_storage, retention, storage, work_recovery
from tauceti_worker.round import RoundContext


class StorageRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()

    def write(self, relative):
        path = self.checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserved source or disposable output")
        return path

    def test_clean_without_lean_preserves_dependency_sources_git_and_external_links(self):
        outputs = [
            self.write(path)
            for path in (
                ".lake/build/lib/A.olean",
                ".lake/cache/archive",
                ".lake/packages/mathlib/.lake/build/lib/B.olean",
                ".lake/packages/mathlib/.lake/cache/archive",
            )
        ]
        sources = [
            self.write(path)
            for path in (
                "Draft.lean",
                "lean-toolchain",
                ".git/HEAD",
                ".lake/packages/mathlib/Draft.lean",
                ".lake/packages/mathlib/.git/HEAD",
            )
        ]
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (self.checkout / ".lake/packages/external").symlink_to(outside, target_is_directory=True)
        with patch.object(storage.subprocess, "run", side_effect=AssertionError("must not invoke Lean")):
            storage.clean_lake_outputs(self.checkout)
        self.assertFalse(any(path.exists() for path in outputs))
        self.assertTrue(all(path.exists() for path in sources))
        self.assertTrue((outside / "keep").exists())

    def test_live_build_is_not_removed(self):
        output = self.write(".lake/build/lib/A.olean")
        with patch.object(work_recovery, "live_users", return_value=["verifier"]):
            with self.assertRaisesRegex(storage.NoProgress, "cleanup incomplete"):
                storage.clean_lake_outputs(self.checkout)
        self.assertTrue(output.exists())

    def test_housekeeping_runs_without_rounds_and_stops_cleanly(self):
        called = threading.Event()
        cfg = SimpleNamespace(state=self.root / "state/docker")

        def housekeeping(root, **budgets):
            self.assertEqual(root, cfg.state.parent)
            self.assertEqual(budgets, dict(seconds=2, entries=16384, bytes=1024**3))
            called.set()
            return {"removed_bytes": 0}

        with (
            patch.dict(os.environ, {"TAUCETI_MIN_FREE_GIB": "3"}),
            patch.object(work_recovery, "housekeep", side_effect=housekeeping),
        ):
            stopping, thread = storage.start_host_storage_maintenance(cfg)
            try:
                self.assertTrue(called.wait(5), "cleanup must not wait for model availability")
            finally:
                stopping.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_housekeeping_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(work_recovery, "housekeep") as housekeep:
            self.assertIsNone(storage.start_host_storage_maintenance(None))
            housekeep.assert_not_called()

    def test_first_round_without_a_checkout_needs_no_cleanup(self):
        cfg = SimpleNamespace(checkout=self.root / "not-cloned-yet")
        with patch.dict(os.environ, {"TAUCETI_MIN_FREE_GIB": "3"}):
            self.assertFalse(storage.maintain_host_lake_storage(cfg, phase="first round"))

    def test_compressed_volume_cannot_hide_physical_disk_pressure(self):
        backing = self.root / "physical"
        env = {"TAUCETI_MIN_FREE_GIB": "3", "TAUCETI_STORAGE_GUARD_PATH": str(backing)}
        gib = 1024**3
        with patch.object(
            storage.shutil,
            "disk_usage",
            side_effect=lambda path: SimpleNamespace(free=(2 if path == backing else 40) * gib),
        ):
            self.assertEqual(storage.host_storage_free(self.checkout, env), 2 * gib)
            from tauceti_worker.agents import BuildCacheUnavailable

            with self.assertRaisesRegex(BuildCacheUnavailable, "2.0 GiB"):
                storage.verify_host_storage_headroom(self.checkout, env, phase="compressed volume")

    def test_round_reclaims_space_before_admission_can_block_it(self):
        cfg = SimpleNamespace(state=self.root / "state/docker", checkout=self.checkout)
        events = []
        with (
            patch.dict(os.environ, {"TAUCETI_MIN_FREE_GIB": "3"}),
            patch.object(retention, "recycle", return_value={"errors": [], "removed": []}),
            patch.object(
                storage, "prune_obsolete_host_lean_toolchains", side_effect=lambda _: events.append("toolchains")
            ),
            patch.object(storage, "maintain_host_lake_storage", side_effect=lambda *a, **kw: events.append("builds")),
            patch.object(cache_storage, "admit", side_effect=RuntimeError("admission reached")),
            patch("tauceti_worker.round.signal.signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "admission reached"):
                with RoundContext(cfg):
                    self.fail("failed admission cannot start a round")
        self.assertEqual(events, ["toolchains", "builds"])


if __name__ == "__main__":
    unittest.main()
