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

Codex uses its own inner namespace sandbox. This desktop's Docker AppArmor
profile denies userns_create, so the override selects apparmor=unconfined for
this container, alongside the base Compose seccomp exception. Host policy is
unchanged; Docker filesystem/network namespaces, limited mounts, default dropped
capabilities and resource limits remain. The container is not privileged.

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

The desktop override mounts `deploy/curl` read-only and sets `CURL_HOME` to it.
Its `.curlrc` selects HTTP/1.1 because the bundled Bookworm curl repeatedly ended
parallel artifact-cache downloads with incomplete HTTP/2 streams (exit 18).
TLS certificate checks and Lake's downloaded-artifact hash checks remain enabled.
This configuration is read directly from the fork checkout; changing it requires
no image rebuild. To roll it back, remove the `CURL_HOME` setting and its matching
bind mount from `deploy/compose.desktop.yaml` after the current round finishes,
then use the system service for the next round. Preserve all named volumes.

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

Lake stores downloaded artifacts separately and restores them during a build.
An absent `.lake/build` directory or a 404 for the newest revision does not by
itself establish a cache outage: an older compatible revision may have supplied
the artifacts. `lake build --no-build <existing-module>` checks and restores a
selected existing target without compiling missing source. Let that check finish
and inspect its result before deciding that cache recovery is needed. The worker's
host-agent guidance includes this distinction; rebuild the image after changing
that guidance. To roll back the guidance, revert its source change and rebuild
while the daily service is inactive, retaining all named volumes.

Compose limits the container to two CPUs' worth of time, 4 GiB RAM and at most
1 GiB swap. Normal named volumes retain checkouts, state, toolchains and logs
under Docker's data root. Optional shared Mathlib builds, eager expanded
artifacts and obsolete-toolchain pruning are disabled. The normal 8 GiB reserve
and 14-day/256 MiB disposable agent-log retention remain enabled.

New upstream workers require shared Mathlib views by default. This deployment
explicitly pairs `TAUCETI_SHARED_MATHLIB_BUILDS=off` with
`TAUCETI_PRIVATE_MATHLIB_BUILDS=1` to retain ordinary checkout-local Mathlib cache
downloads. This is a selected storage mode, not a fallback after shared preparation
fails. An attached shared tree, retained shared-view marker (including malformed
markers), or inherited view/lease metadata blocks private extraction and remains
intact for recovery. No shared-cache migration or extra service is needed.

At the existing 03:00 check-in, inspect the service result and actual work:

```sh
systemctl status tauceti-desktop-daily.service
systemctl list-timers tauceti-desktop-daily.timer
journalctl -u tauceti-desktop-daily.service --since today
docker-compose -p tauceti-desktop -f compose.yaml -f deploy/compose.desktop.yaml logs --tail 100 tauceti
```

## Daily upstream updates

The existing 03:00 maintenance agent checks upstream and owns rebasing, validation
and deployment. Keep `origin` pointed at `eohjelle/TauCetiWorker` and `upstream` at
`TauCetiProject/TauCetiWorker`. Fetch current refs into an isolated clone; record
the observed upstream commit, deployed commit/image and exact remote branch head.
Retain a Git bundle or backup ref and the previous image before rebasing. Rebase
`desktop-daily-worker` on the observed `upstream/main`, resolving conflicts against
the current upstream contracts while preserving this desktop's settings. Validate
the resulting code, focused credit/private-cache/roadmap/credential tests, relevant
upstream tests, shell scripts and merged Compose configuration. Build a candidate
image from the exact validated tree under a separate tag.

Before changing the deployment, recheck the system service and canonical container
are stopped, the round lock is unheld, both worker and mathematical checkouts are
clean, and prior publication is settled. Do not interrupt a healthy round; record
its identity and finish independent preparation first. Publish with
`--force-with-lease=refs/heads/desktop-daily-worker:<observed-remote-head>` so another
writer cannot be overwritten. Keep the original remote history in the backup.
Move the live checkout to that exact tested commit, tag the tested image for the
existing Compose project, and recreate its stopped container with Compose `create
--no-deps --force-recreate tauceti`. Do not use `up` for update validation: that
would start another model round. Never use `down -v` or prune recovery images.

Verify the image's packaged source and harmless CLI/import checks, merged Compose
command/mounts/limits, stopped canonical container image, system unit/drop-ins and
next 02:00 worker / 03:00 review occurrences. Preserve any host-installed
`credit-reserve.conf` service drop-in; it is owned by Botolomy and is not replaced
by this fork. No credential refresh, model call, provider spending-setting change
or new recurring job is part of this verification. Record exact refs, image ID,
test results, deployment evidence and any remaining limitations.

For an update rollback, reconcile execution again, restore the retained source
commit and matching image tag, and recreate the stopped canonical container using
the same Compose project and unchanged volumes. Keep the upstream/rebase evidence;
do not rewind the remote branch blindly. Update deployment metadata to identify
the restored commit/image and the failed candidate.

## Stop and uninstall

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
