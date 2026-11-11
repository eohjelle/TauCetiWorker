# Desktop daily Docker worker

This deployment uses the fork's existing image and Compose volumes, with
[`deploy/compose.desktop.yaml`](../deploy/compose.desktop.yaml) selecting one
Codex round each day. The general data drive is mounted at `/srv/data`; TauCeti
uses `/srv/data/tauceti`, while Docker's configured data root is `/srv/data/docker`.
Use ordinary directories and Docker volumes. No compressed loopback filesystem,
state migration, or storage supervisor is needed.

The source checkout is `/srv/data/tauceti/TauCetiWorker`. The system service runs
as `botolomy` with supplementary group `docker`, so installation does not require
restarting the desktop user manager. Docker access is privileged; the container
does not receive the Docker socket or the desktop home directory.

## Prepare and install

Install Docker Engine, Compose v2 as `/usr/bin/docker-compose`, and Python 3.11
or newer. On Debian 13 the packages are `docker.io`, `docker-cli`,
`docker-compose`, and `docker-buildx`. Prepare and mount the general data drive separately after reviewing
its contents. Configure Docker's data root before building images. `deploy/docker-daemon.desktop.json`
is the source for `/etc/docker/daemon.json`; `deploy/systemd/docker-data-drive.conf`
is installed as `/etc/systemd/system/docker.service.d/data-drive.conf`. The latter
requires the data drive before Docker starts. This deployment uses Docker 26 with
classic `overlay2`, so images and volumes live under that data root. Review existing
daemon configuration before replacing it; containerd image stores require their own
data-root configuration. Create
`/srv/data/tauceti` owned by `botolomy`, mode 0700, and clone this fork there.
The Docker image supplies the worker's tools; follow [the Docker guide](docker.md)
for image requirements and troubleshooting.

The desktop must have working Codex file credentials and GitHub CLI file
credentials. Before each round, `scripts/prepare_desktop_credentials.py` reads
the desktop's `~/.codex/auth.json`, checks that its access token is unexpired,
and uses the existing OAuth mirror writer to publish an access-only copy.
It also copies only `hosts.yml` and optional `config.yml` from `~/.config/gh`.
The private output directories are:

- `/srv/data/tauceti/credentials/codex-worker`, mounted read-only at `/root/.codex`.
- `/srv/data/tauceti/credentials/gh`, mounted read-only at `/root/.config/gh`.

Directories have mode 0700 and generated files mode 0600. Whole-directory mounts
allow atomic file replacement without pinning an old credential inode. The
desktop login remains the only Codex refresh owner: the helper makes no network
requests and never refreshes. Both Compose refreshers have manual profiles and
are excluded from the daily command. Credentials are copied at launch only; an
access token expiring during a long round can require a later retry after the
desktop login refreshes. Keep credentials out of Git and ordinary source backups.

From the checkout, validate and build without starting a worker:

```sh
python3 tests/desktop_credentials.py
docker-compose -p tauceti-desktop -f compose.yaml -f deploy/compose.desktop.yaml config --quiet
docker-compose -p tauceti-desktop -f compose.yaml -f deploy/compose.desktop.yaml build tauceti
```

Remove any prior native user schedule before enabling the system timer:

```sh
systemctl --user disable --now tauceti-desktop-daily.timer
systemctl --user stop tauceti-desktop-daily.service
```

Install the system units and start today's round:

```sh
sudo install -m 644 deploy/systemd/tauceti-desktop-daily.service deploy/systemd/tauceti-desktop-daily.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/tauceti-desktop-daily.service /etc/systemd/system/tauceti-desktop-daily.timer
sudo systemctl daemon-reload
sudo systemctl enable --now tauceti-desktop-daily.timer
sudo systemctl start --no-block tauceti-desktop-daily.service
```

The two worker units, Docker daemon configuration and mount dependency are the
installed system files. Reinstall changed units/configuration and reload systemd after
source changes; rebuild the image when worker source changes. The Compose
override and helper run directly from the checkout. No native environment file
is used. Mount requirements and a mountpoint preflight prevent runtime writes
to an unmounted data directory.

## Schedule and outcome

The timer starts at 02:00 Europe/Oslo, before the existing 03:00 check-in, and
catches up once after downtime. On the spring clock-change day it uses 01:00,
one elapsed hour before 03:00. It starts only the canonical `tauceti` service in
the dedicated `tauceti-desktop` Compose project, without a loop or restart policy.
Use the system service for manual rounds too; do not start a competing worker
in that project.

The command uses worker ID `desktop`, skips progress, selects the ReductiveGroups
roadmap, and prefers roadmap work before review. It requests `--ignore-quota`;
check the worker's quota diagnostics for admission failures. The schedule and a
successful process exit alone do not establish that useful work completed.

Compose limits the container to two CPUs' worth of time, 4 GiB RAM and at most
1 GiB swap. Normal named volumes retain checkouts, state, toolchains and logs
under Docker's data root. Optional shared Mathlib builds, eager expanded
artifacts and obsolete-toolchain pruning are disabled. The normal 8 GiB reserve
and 14-day/256 MiB disposable agent-log retention remain enabled.

At the existing 03:00 check-in, inspect the service result and actual work:

```sh
systemctl status tauceti-desktop-daily.service
systemctl list-timers tauceti-desktop-daily.timer
journalctl -u tauceti-desktop-daily.service --since today
docker-compose -p tauceti-desktop -f compose.yaml -f deploy/compose.desktop.yaml logs --tail 100 tauceti
```

## Stop and rollback

```sh
sudo systemctl disable --now tauceti-desktop-daily.timer
sudo systemctl stop tauceti-desktop-daily.service
```

`ExecStopPost` stops the dedicated Compose service with a 120-second grace period,
including after an interrupted foreground Compose command. To uninstall, remove
only `/etc/systemd/system/tauceti-desktop-daily.service` and
`/etc/systemd/system/tauceti-desktop-daily.timer`, then reload systemd. Keep the
checkout, credentials and named volumes; do not use `down -v`. Disable/remove old
native user-unit links if still present, and keep that timer disabled. Stop the
worker before unmounting `/srv/data`. No filesystem reversal or state migration
is needed.

The workspace project path links to this checkout on the data drive. Source and
deployment changes are committed and pushed to the GitHub fork; do not rely on a
backup walker following that symlink. Credentials and generated volumes stay out
of source archives. A daemon-configuration rollback requires stopping/reconciling
all containers using this data root first; never delete the volume data.
