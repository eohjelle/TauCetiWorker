"""Exercise the exact trusted shell script across real exec argument limits."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_tools import templates
from tauceti_tools.project import Project

from tauceti_worker import verification_resources as v

FIXTURES = Path(__file__).parent / "fixtures/header_style_batching"


class HeaderBatching(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"
        self.scripts = self.assets / "scripts"
        shutil.copytree(FIXTURES, self.scripts)
        self.project = self.root / "project"
        (self.project / ".lake").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.files = self.root / "files"
        self.modules = self.root / "modules"
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TAUCETI_PROJECT_ROOT": str(self.project),
            "TAUCETI_TRUSTED_SCRIPTS": str(self.scripts),
            "FIXTURE_FILES": str(self.files),
            "FIXTURE_MODULES": str(self.modules),
            "FIXTURE_LOG": str(self.log),
            "LINT_CHANGED_ONLY": "0",
            "LC_ALL": "C",
        }
        (self.scripts / "source-modules.sh").write_text(
            'tauceti_source_modules() { cp "$FIXTURE_FILES" "$1"; cp "$FIXTURE_MODULES" "$2"; }\n'
        )
        lake = self.bin / "lake"
        lake.write_text(
            f"#!{sys.executable}\n"
            "import hashlib,json,os,pathlib,sys\n"
            "args=sys.argv[1:]\n"
            "if args[:3] == ['env','lean','--run']:\n"
            "    assert pathlib.Path(args[3]).name == 'HeaderStyle.lean'\n"
            "    row={'kind':'header','files':args[4:]}\n"
            "    result=int(os.environ.get('FAIL_HEADER','') in args[4:])\n"
            "elif args[:2] == ['exe','lint-style']:\n"
            "    source=pathlib.Path(os.environ['LEAN_SRC_PATH'].split(os.pathsep)[0])/'TauCetiLint/All.lean'\n"
            "    row={'kind':'text','args':args[2:],'imports':source.read_text().splitlines()}\n"
            "    result=int(os.environ.get('FAIL_TEXT','0'))\n"
            "else:\n"
            "    raise AssertionError(args)\n"
            "with open(os.environ['FIXTURE_LOG'],'a') as output: output.write(json.dumps(row)+'\\n')\n"
            "raise SystemExit(result)\n"
        )
        lake.chmod(0o755)

    def inventory(self, count):
        names = [f"TauCeti/{'LongNamespace' * 7}/Source{i:05d}.lean" for i in range(count)]
        self.files.write_bytes(b"".join(os.fsencode(name) + b"\0" for name in names))
        self.modules.write_text("".join(name[:-5].replace("/", ".") + "\n" for name in names))
        return names

    def run_style(self, **changes):
        self.log.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(self.scripts / "lint-style.sh"), "--fixture-option"],
            cwd=self.project,
            env={**self.env, **changes},
            capture_output=True,
            text=True,
            timeout=60,
        )
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, rows

    def adapt(self):
        updated = v.batched_style(self.scripts)
        self.assertIsNotNone(updated)
        (self.scripts / "lint-style.sh").write_bytes(updated)

    def assert_complete(self, rows, names):
        headers = [row for row in rows if row["kind"] == "header"]
        self.assertEqual([name for row in headers for name in row["files"]], names)
        self.assertTrue(all(0 < len(row["files"]) <= 2000 for row in headers))
        text = [row for row in rows if row["kind"] == "text"]
        self.assertEqual(len(text), 1)
        self.assertEqual(rows[-1]["kind"], "text")
        self.assertEqual(text[0]["args"], ["--fixture-option", "TauCetiLint.All", "TauCeti"])
        self.assertEqual(text[0]["imports"], ["import TauCeti", *["import " + n[:-5].replace("/", ".") for n in names]])

    @unittest.skipUnless(sys.platform == "linux", "reproduces the desktop execve limit")
    def test_full_scope_overflows_original_but_every_file_is_checked_once_after_batching(self):
        names = self.inventory(32605)
        self.assertGreater(self.files.stat().st_size, os.sysconf("SC_ARG_MAX"))
        original, rows = self.run_style()
        self.assertEqual(original.returncode, 1)
        self.assertIn("Argument list too long", original.stderr)
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.adapt()
        result, rows = self.run_style()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(len(rows), 2)
        self.assert_complete(rows, names)

    def test_header_failure_runs_all_later_batches_and_the_full_text_check(self):
        names = self.inventory(4201)
        self.adapt()
        result, rows = self.run_style(FAIL_HEADER=names[1999])
        self.assertEqual(result.returncode, 1)
        self.assert_complete(rows, names)

    def test_text_failure_is_not_hidden_and_empty_scope_keeps_its_existing_contract(self):
        names = self.inventory(3)
        self.adapt()
        result, rows = self.run_style(FAIL_TEXT="9")
        self.assertEqual(result.returncode, 1)
        self.assert_complete(rows, names)
        self.inventory(0)
        result, rows = self.run_style()
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipping header audit", result.stdout)
        self.assert_complete(rows, [])

    def test_checked_adapter_exactly_matches_newer_upstream_and_is_idempotent(self):
        before = (self.scripts / "HeaderStyle.lean").read_bytes()
        self.adapt()
        updated = (self.scripts / "lint-style.sh").read_bytes()
        self.assertEqual(hashlib.sha256(updated).hexdigest(), v.STYLE_BATCHED_SHA256)
        self.assertIsNone(v.batched_style(self.scripts))
        self.assertEqual((self.scripts / "HeaderStyle.lean").read_bytes(), before)

    def test_unknown_script_or_checker_fails_closed(self):
        for name in ("lint-style.sh", "HeaderStyle.lean"):
            with self.subTest(name=name):
                path = self.scripts / name
                before = path.read_bytes()
                path.write_bytes(before + b"\n# unrecognized version\n")
                with self.assertRaisesRegex(ValueError, "unrecognized trusted"):
                    v.batched_style(self.scripts)
                path.write_bytes(before)

    def test_run_script_uses_a_private_copy_and_preserves_its_nonzero_exit(self):
        (self.scripts / "local_checks.py").write_text(
            "import hashlib,pathlib\n"
            "root=pathlib.Path(__file__).parent\n"
            f"assert hashlib.sha256((root/'lint-style.sh').read_bytes()).hexdigest() == '{v.STYLE_BATCHED_SHA256}'\n"
            f"assert hashlib.sha256((root/'HeaderStyle.lean').read_bytes()).hexdigest() == '{v.HEADER_STYLE_SHA256}'\n"
            "raise SystemExit(7)\n"
        )
        before = {p.name: p.read_bytes() for p in self.scripts.iterdir()}
        with (
            patch.object(templates, "ASSETS", self.assets),
            patch.object(templates, "bootstrap_qualified", return_value=None),
        ):
            self.assertEqual(v.run_shared_checks(["style"]), 7)
            self.assertEqual(templates.ASSETS, self.assets)
        self.assertEqual({p.name: p.read_bytes() for p in self.scripts.iterdir()}, before)

    def test_options_before_phase_use_the_adapter_and_keep_argument_order(self):
        arguments = (
            ["--base", "fixture", "style"],
            ["--policy", "ordinary", "verify"],
            ["--base=fixture", "--policy=ordinary", "lint"],
        )
        for args in arguments:
            with self.subTest(args=args):
                (self.scripts / "local_checks.py").write_text(
                    "import argparse,hashlib,pathlib,sys\n"
                    "parser=argparse.ArgumentParser()\n"
                    "parser.add_argument('phase', choices=['style','lint','verify'])\n"
                    "parser.add_argument('--base')\nparser.add_argument('--policy')\n"
                    "parser.parse_args()\n"
                    f"assert sys.argv[1:] == {args!r}\n"
                    "style=pathlib.Path(__file__).with_name('lint-style.sh').read_bytes()\n"
                    f"assert hashlib.sha256(style).hexdigest() == '{v.STYLE_BATCHED_SHA256}'\n"
                )
                with (
                    patch.object(templates, "ASSETS", self.assets),
                    patch.object(templates, "bootstrap_qualified", return_value=None),
                ):
                    self.assertEqual(v.run_shared_checks(args), 0)

    def test_project_materialization_renders_scripts_and_preserves_local_policy(self):
        project = Project(
            {
                "repositories": {"content": "example/Library", "roadmap": "example/Roadmap"},
                "lean": {"roots": ["Library"]},
            },
            "fixture",
        )
        policy = self.project / "scripts/policy.json"
        policy.parent.mkdir()
        policy.write_bytes(b'{"TauCeti": "unrendered local policy"}\n')
        rendered = templates.render(v.batched_style(self.scripts).decode(), project, qualified=True)
        digest = hashlib.sha256(rendered.encode()).hexdigest()
        (self.scripts / "local_checks.py").write_text(
            "import hashlib,os,pathlib\n"
            "root=pathlib.Path(__file__).parent\n"
            "assert str(root) == os.environ['TAUCETI_TRUSTED_SCRIPTS']\n"
            f"assert os.environ['TAUCETI_PROJECT_ROOT'] == {str(self.project)!r}\n"
            f"assert hashlib.sha256((root/'lint-style.sh').read_bytes()).hexdigest() == '{digest}'\n"
            f"assert hashlib.sha256((root/'policy.json').read_bytes()).hexdigest() == '{hashlib.sha256(policy.read_bytes()).hexdigest()}'\n"
            "assert 'Library' in (root/'HeaderStyle.lean').read_text()\n"
        )
        with (
            patch.object(templates, "ASSETS", self.assets),
            patch.object(templates, "bootstrap_qualified", return_value=project),
            patch.object(templates, "checkout_root", return_value=self.project),
        ):
            self.assertEqual(v.run_shared_checks(["style"]), 0)
            self.assertEqual(templates.ASSETS, self.assets)
        self.assertEqual(policy.read_bytes(), b'{"TauCeti": "unrendered local policy"}\n')

    def test_unrecognized_adapter_returns_unavailable_without_running_checks(self):
        package = self.root / "tauceti_tools"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "templates.py").write_text(
            f"from pathlib import Path\nASSETS=Path({str(self.assets)!r})\n"
            "def run_script(*args): raise AssertionError('must refuse before running checks')\n"
        )
        (self.scripts / "lint-style.sh").write_text("unknown trusted script\n")
        with (
            patch.dict(os.environ, {"PYTHONPATH": str(self.root)}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(v.verify_shared(["style"]), 75)
        self.assertIn("Shared verification unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
