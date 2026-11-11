# Native desktop daily worker

The files in [`deploy/systemd/`](../deploy/systemd/) run one native Codex round
from this fork at 02:00 Europe/Oslo, before the existing 03:00 check-in. They use
ordinary directories under `/srv/data/tauceti` on the general data drive mounted
at `/srv/data`. The rest of that drive remains available for other storage.
No Docker, worker loop, storage supervisor, loopback filesystem, or state bind
mount is required.

The command preserves the worker's scope: skip progress, use the ReductiveGroups
roadmap, and prefer roadmap work before review. `--ignore-quota` bypasses soft
quota pacing; unreadable usage and provider hard limits can still prevent work.
The timer starts an attempt, so the 03:00 check must verify the actual outcome.

## Prepare the host

Install `git`, `gh`, `jq`, `uv`, `codex`, and `elan`/`lake`. The service expects
`uv` at `~/.local/bin/uv` and finds the other tools through its explicit `PATH`.
Prepare the general data disk separately after reviewing its contents. Mount it
persistently at `/srv/data`. Create a private `/srv/data/tauceti` directory owned
by the worker's login user with mode 0700, then clone `eohjelle/TauCetiWorker` to
`/srv/data/tauceti/TauCetiWorker`. The TauCeti directory is an ordinary subdirectory,
not a separate mount.

The source checkout anchors its own `state/`, `checkouts/`, and `logs/` directories.
The environment template puts Lean toolchains, caches, and temporary files on
the same disk. Use ordinary directories; keep credentials out of Git and backups
that are not intended to hold credentials.

Run as the login user after the disk is mounted:

```sh
mountpoint --quiet /srv/data || exit
install -d -m 700 /srv/data/tauceti /srv/data/tauceti/elan /srv/data/tauceti/cache /srv/data/tauceti/tmp
cd /srv/data/tauceti/TauCetiWorker || exit
install -d -m 700 ~/.config/tauceti
install -m 600 deploy/systemd/native-desktop.env.example ~/.config/tauceti/native-desktop.env
```

Review an existing environment file before replacing it. This file uses systemd
environment syntax, with literal absolute paths. The template disables optional
shared Mathlib builds, eager expanded artifacts, and obsolete-toolchain pruning.
It retains the worker's ordinary 8 GiB free-space check and 14-day/256 MiB
disposable agent-log retention. Source, recovery state, and credentials are not
disposable logs. It does not import the former host's storage settings.

Authenticate `gh` as the intended GitHub account, and verify the desktop's Codex
login. `--worker-id desktop` uses the fork's existing credential isolation:
the worker mirrors the desktop's access token, with the real refresh token
removed, and follows subsequent desktop refreshes. Keep refresh ownership with
the existing desktop login; do not add a second refresher holding its token.
The source paths default to `~/.codex` and `~/.config/gh`; override `CODEX_HOME`
or `GH_CONFIG_DIR` in the private environment file if needed.

For the supplied template, check the dependencies and authentication using the
same environment before starting a real round:

```sh
set -a
# shellcheck source=deploy/systemd/native-desktop.env.example
. ~/.config/tauceti/native-desktop.env
set +a
~/.local/bin/uv run --script ./tauceti doctor
```

## Install and run

The following links the units to their source in the fork. Reload the user
manager after updating these files. Ensure this user's systemd manager starts
at boot; unattended user services normally use `loginctl enable-linger USER`.

```sh
systemd-analyze --user verify deploy/systemd/tauceti-desktop-daily.service deploy/systemd/tauceti-desktop-daily.timer
systemctl --user link /srv/data/tauceti/TauCetiWorker/deploy/systemd/tauceti-desktop-daily.service /srv/data/tauceti/TauCetiWorker/deploy/systemd/tauceti-desktop-daily.timer
systemctl --user daemon-reload
systemctl --user enable --now tauceti-desktop-daily.timer
systemctl --user start --no-block tauceti-desktop-daily.service
```

The last command starts today's round immediately. Starting the same service
while it is already running does not launch another copy. The timer catches up
once after downtime (`Persistent=true`). On the spring clock-change day, when
02:00 is absent, it starts at 01:00: one elapsed hour before 03:00.

The service checks that `/srv/data` is a mount point before launching, so a
missing data disk does not turn into runtime data on the system disk. Its CPU
quota allows two CPU cores' worth of time; its memory ceiling is 4 GiB. These
desktop resource limits can be adjusted with `systemctl --user edit
tauceti-desktop-daily.service`. A memory-limit termination is a failed round,
not completed work.

Inspect the local outcome at the existing 03:00 check-in:

```sh
systemctl --user list-timers tauceti-desktop-daily.timer
systemctl --user status tauceti-desktop-daily.service
journalctl --user -u tauceti-desktop-daily.service --since today
```

Also inspect the worker's `logs/desktop/` and prepared activity records for
useful work. A service exit status alone does not establish that useful work
completed. This deployment adds no separate check-in job.

## Stop or remove the schedule

```sh
systemctl --user disable --now tauceti-desktop-daily.timer
systemctl --user stop tauceti-desktop-daily.service
```

Stopping sends SIGTERM to all service processes, allowing the worker's
existing cleanup to run, with a two-minute stop timeout. To uninstall, remove
only the two unit links from `~/.config/systemd/user/`, then run `systemctl
--user daemon-reload`. Preserve the environment file, checkout, runtime state,
and credentials. Stop the worker before unmounting the data disk. Restoring
ordinary native operation never requires filesystem or state migration.
