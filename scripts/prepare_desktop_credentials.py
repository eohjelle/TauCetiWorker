#!/usr/bin/env python3
"""Publish private container credentials without refreshing or contacting a provider."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Load the existing stdlib-only mirror implementation without importing the
# package initializer, which also loads the UI and project dependencies.
_spec = importlib.util.spec_from_file_location(
    "tauceti_desktop_oauth", Path(__file__).resolve().parents[1] / "tauceti_worker" / "oauth.py"
)
assert _spec is not None and _spec.loader is not None
oauth = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = oauth
_spec.loader.exec_module(oauth)


class PreparationError(Exception):
    """A credential-free explanation safe to print in the service journal."""


def _write_private(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".prepare.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)  # mkstemp creates the file with mode 0600.
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(codex_source: Path, gh_source: Path, destination: Path) -> None:
    target = destination.resolve()
    for source in (codex_source.parent.resolve(), gh_source.resolve()):
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise PreparationError("Credential source and destination directories must be separate.")
    directories = (destination, destination / "codex-worker", destination / "gh")
    if any(path.is_symlink() for path in directories):
        raise PreparationError("Credential destination directories must not be symlinks.")

    credentials = json.loads(codex_source.read_text())
    tokens = credentials.get("tokens") if isinstance(credentials, dict) else None
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"]
    ):
        raise PreparationError("Desktop Codex access token is missing; check the desktop login.")
    provider = oauth.provider("codex", credentials=codex_source, mirror=directories[1] / "auth.json")
    expiry = oauth._expires_at(provider, credentials)
    if expiry is None or expiry <= time.time():
        raise PreparationError(
            "Desktop Codex access token is expired or has no readable expiry; refresh the desktop login."
        )

    # Read everything before publishing. A failed preflight must not launch a
    # container using credentials left over from a previous round.
    hosts = (gh_source / "hosts.yml").read_bytes()
    if not hosts.strip():
        raise PreparationError("Desktop GitHub hosts.yml is empty; check gh authentication.")
    config_path = gh_source / "config.yml"
    config = config_path.read_bytes() if config_path.exists() else None
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    oauth._write_worker_mirror(provider, credentials)
    _write_private(directories[2] / "hosts.yml", hosts)
    if config is not None:
        _write_private(directories[2] / "config.yml", config)
    else:
        (directories[2] / "config.yml").unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-source",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json",
    )
    parser.add_argument(
        "--gh-source", type=Path, default=Path(os.environ.get("GH_CONFIG_DIR", Path.home() / ".config" / "gh"))
    )
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.codex_source, args.gh_source, args.destination)
    except PreparationError as error:
        print(f"Credential preparation failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — keep every unexpected parser error out of the journal
        # Never put input data or an unexpected parser message into the journal.
        print(
            f"Credential preparation failed ({type(error).__name__}); check desktop login files and permissions.",
            file=sys.stderr,
        )
        return 1
    print("Prepared access-only Codex and GitHub credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
