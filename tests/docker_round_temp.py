"""Run inside a disposable Docker image to verify real subprocess temporary cleanup."""

import subprocess
import sys
import tempfile
from pathlib import Path

from tauceti_worker import round as lifecycle

preserved = Path(tempfile.mkdtemp(prefix="preexisting-")) / "keep"
preserved.write_text("keep")
created = Path("/tmp/round-cleanup-smoke")


def check_round(exit_code):
    script = (
        "from pathlib import Path; import sys,time; "
        f"p=Path({str(created)!r}); p.mkdir(); (p/'output').write_text('scratch'); "
        + ("time.sleep(60)" if exit_code is None else f"sys.exit({exit_code})")
    )
    lifecycle.spawn_round = lambda _args: subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    actual = lifecycle.run_round_subprocess([], timeout=1 if exit_code is None else 30)
    assert actual == (124 if exit_code is None else exit_code), actual
    assert not created.exists(), "round temporary directory survived"
    assert preserved.read_text() == "keep", "pre-existing file was removed"


for exit_code in (0, 7, None):
    check_round(exit_code)

print("PASS: Docker removes completed/failed/timed-out round files and preserves pre-existing files")
