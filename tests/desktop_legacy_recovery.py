"""Desktop updates preserve old recovery data and refuse destructive unpublished-source cleanup."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import agents, live_use, retention, round_worktrees


class DesktopPreservation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.env = patch.dict(os.environ, {"TAUCETI_PRESERVE_LEGACY_RECOVERY": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "Source.lean").write_text("-- published source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "published source")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def problem(self):
        return agents.checkout_preservation_problem(self.repo)

    def test_published_clean_checkout_proceeds(self):
        self.assertIsNone(self.problem())

    def test_untracked_source_refuses_prepare_and_preserves_bytes(self):
        source = self.repo / "New.lean"
        source.write_text("-- unfinished source\n")
        self.assertIn("New.lean", self.problem())
        with patch.object(agents, "sync_mathlib_pool"), patch.object(round_worktrees, "retire") as retire:
            self.assertFalse(agents.prepare_checkout(SimpleNamespace(checkout=self.repo)))
            retire.assert_not_called()
        self.assertEqual(source.read_text(), "-- unfinished source\n")

    def test_tracked_and_staged_edits_are_preserved(self):
        source = self.repo / "Source.lean"
        source.write_text("-- changed source\n")
        self.assertIn("tracked edits", self.problem())
        self.git("add", "Source.lean")
        self.assertIn("tracked edits", self.problem())
        self.assertFalse(agents.discard_checkout(self.repo, "test"))
        self.assertEqual(source.read_text(), "-- changed source\n")

    def test_clean_unpublished_commit_then_confirmed_push(self):
        (self.repo / "Source.lean").write_text("-- new commit\n")
        self.git("commit", "-qam", "unpublished")
        self.assertIn("lacks confirmed publication", self.problem())
        self.git("update-ref", "refs/tauceti/published/scope/test/new-branch", "HEAD")
        self.assertIsNone(self.problem())

    def test_unpublished_local_branch_is_not_lost(self):
        self.git("checkout", "-qb", "unfinished")
        self.git("commit", "--allow-empty", "-qm", "unpublished")
        self.git("checkout", "-q", "--detach", "refs/remotes/origin/main")
        self.assertIn("lacks confirmed publication", self.problem())

    def test_ignored_source_is_preserved_but_generated_cache_is_allowed(self):
        (self.repo / ".git/info/exclude").write_text(".lake/\n*.secret\n")
        cache = self.repo / ".lake/build/large.olean"
        cache.parent.mkdir(parents=True)
        cache.write_text("generated")
        self.assertIsNone(self.problem())
        ignored = self.repo / "work.secret"
        ignored.write_text("private fixture")
        self.assertIn("ignored source", self.problem())
        self.assertTrue(ignored.exists())

    def test_live_users_and_missing_metadata_refuse_deletion(self):
        with patch.object(live_use, "live_users", return_value=["123 (lean)"]):
            self.assertIn("live users", self.problem())
        partial = self.root / "partial"
        partial.mkdir()
        (partial / "Draft.lean").write_text("source")
        self.assertFalse(agents.discard_checkout(partial, "interrupted clone"))
        self.assertTrue((partial / "Draft.lean").exists())

    def test_inspection_timeout_preserves_source(self):
        with (
            patch.object(live_use, "live_users", return_value=[]),
            patch.object(live_use, "checkout_kind", return_value="standalone"),
            patch.object(agents.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)),
        ):
            self.assertIn("inspection failed", self.problem())
        self.assertTrue((self.repo / "Source.lean").exists())

    def test_worktree_removal_refuses_unfinished_source(self):
        (self.repo / "Source.lean").write_text("-- unfinished\n")
        with patch.object(round_worktrees, "_check_unused"):
            with self.assertRaisesRegex(round_worktrees.InUse, "tracked edits"):
                round_worktrees._remove(self.repo)
        self.assertTrue((self.repo / "Source.lean").exists())

    def test_legacy_roots_and_host_ledger_remain_intact(self):
        state = self.root / "state"
        places = [state, state / "verification-state", state / "sources/forge"]
        fixtures = []
        for place in places:
            for name in retention.LEGACY:
                path = place / name / "Draft.lean"
                path.parent.mkdir(parents=True)
                path.write_text("source")
                fixtures.append(path)
        for place in (state / "progress", self.repo.parent):
            path = place / "retained-checkouts/old/Draft.lean"
            path.parent.mkdir(parents=True)
            path.write_text("source")
            fixtures.append(path)
        ledger = self.root / "verification-storage"
        ledger.mkdir()
        (ledger / "receipt.json").write_text("{}")
        report = retention.fresh_report()
        retention.purge_legacy(state, report, checkout=self.repo)
        with patch.dict(os.environ, {"TAUCETI_VERIFY_STORAGE_DIR": str(ledger)}):
            retention.purge_legacy_host(report)
        with patch.object(retention, "retired_roots", side_effect=AssertionError("must not retire roots")):
            retention.retire_roots(report)
        self.assertEqual(report["removed"], [])
        self.assertTrue(all(path.read_text() == "source" for path in fixtures))
        self.assertTrue((ledger / "receipt.json").exists())

    def test_idle_checkout_keeps_source_while_cache_can_be_reclaimed(self):
        cache = self.repo / ".lake/build/output"
        cache.parent.mkdir(parents=True)
        cache.write_text("generated")
        cfg = SimpleNamespace(checkout=self.repo)
        report = retention.fresh_report()
        retention.evict_tree(cfg, self.repo, self.root, report, discard=True)
        self.assertTrue((self.repo / "Source.lean").exists())
        self.assertFalse(cache.exists())

    def test_detached_collector_inherits_preservation(self):
        env = retention._collector_env(self.root / "activity")
        self.assertEqual(env["TAUCETI_PRESERVE_LEGACY_RECOVERY"], "1")

    def test_unset_switch_keeps_upstream_behavior(self):
        state = self.root / "state"
        recovery = state / "recovery/old/Draft.lean"
        recovery.parent.mkdir(parents=True)
        recovery.write_text("source")
        (self.repo / "Source.lean").write_text("-- unfinished\n")
        with patch.dict(os.environ, {"TAUCETI_PRESERVE_LEGACY_RECOVERY": "0"}):
            self.assertIsNone(self.problem())
            retention.purge_legacy(state, retention.fresh_report())
        self.assertFalse(recovery.exists())


if __name__ == "__main__":
    unittest.main()
