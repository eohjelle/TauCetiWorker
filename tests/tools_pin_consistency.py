"""Both worker entry points must select the same immutable shared Tools revision."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
script = (ROOT / "tauceti").read_text()
metadata = re.search(r"(?ms)^# /// script\n(.*?)^# ///$", script)
assert metadata, "source entry point has no PEP 723 metadata"
script_project = tomllib.loads("\n".join(line.removeprefix("# ") for line in metadata[1].splitlines()))
installed_project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def tools_pin(project):
    dependencies = [value for value in project["dependencies"] if value.startswith("tauceti-tools ")]
    assert len(dependencies) == 1, "each entry point must declare exactly one shared Tools dependency"
    match = re.fullmatch(
        r"tauceti-tools @ git\+https://github.com/TauCetiProject/TauCetiTools@([0-9a-f]{40})", dependencies[0]
    )
    assert match, "shared Tools must have an immutable Git revision"
    return match[1]


assert tools_pin(script_project) == tools_pin(installed_project), "source and installed worker Tools pins differ"
print(f"Tools pin consistent: {tools_pin(script_project)}")
