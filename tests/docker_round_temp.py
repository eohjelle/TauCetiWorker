"""Run inside a disposable Docker image to verify real subprocess temporary cleanup."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tauceti_worker import round as lifecycle

preserved = Path(tempfile.mkdtemp(prefix="preexisting-")) / "keep"
preserved.write_text("keep")


def check_round(exit_code):
    created = []
    script = (
        "from pathlib import Path; import sys,time,tempfile; "
        "p=Path(tempfile.mkdtemp()); (p/'output').write_text('scratch'); "
        + ("time.sleep(60)" if exit_code is None else f"sys.exit({exit_code})")
    )

    def spawn(_args, *, temporary, **kwargs):
        created.append(temporary)
        return subprocess.Popen(
            [sys.executable, "-c", script], start_new_session=True, env={**os.environ, "TMPDIR": str(temporary)}
        )

    lifecycle.spawn_round = spawn
    actual = lifecycle.run_round_subprocess([], timeout=1 if exit_code is None else 30)
    assert actual == (124 if exit_code is None else exit_code), actual
    assert created and not created[0].exists(), "round temporary directory survived"
    assert preserved.read_text() == "keep", "pre-existing file was removed"


for exit_code in (0, 7, None):
    check_round(exit_code)

print("PASS: Docker removes completed/failed/timed-out round files and preserves pre-existing files")
