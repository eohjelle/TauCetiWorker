# Docker deployment

The Compose deployment packages TauCetiWorker, Elan, GitHub CLI, Codex, and Claude
Code for an unattended Linux host. The Lean toolchain selected by TauCeti is downloaded
on first use and retained in a named volume.

The image is built from the current local checkout: the Dockerfile copies `tauceti`,
`tauceti_worker/`, `prompts/`, and `scripts/` from the build context and never clones
a remote TauCetiWorker repository. Building from a fork checkout therefore packages
that fork's exact checked-out revision, including uncommitted source changes.

## Requirements

- Docker with Compose v2, either as `docker compose` or the standalone `docker-compose`
- At least 8 GB of RAM for Lean builds
- Roughly 25 GB of free disk for the image, toolchain, and Mathlib cache
- GitHub access plus Codex and Claude subscription credentials

## Setup

Run these commands from the repository root. Follow the prompts from each login
command; the temporary setup containers are removed, while credentials remain in
named volumes. If `docker compose` is unavailable, replace it with `docker-compose`.

```bash
docker compose build
docker compose run --rm auth gh auth login --git-protocol https
docker compose run --rm auth codex login --device-auth
docker compose run --rm auth claude auth login
```

Start the worker and follow its logs:

```bash
docker compose up -d
docker compose logs -f tauceti claude-refresh codex-refresh
```

Starting the deployment also starts the credential refreshers. They copy access-only
credentials into the volumes mounted by the worker, so they must run before checking
the worker environment. Once they have started, an optional check is:

```bash
docker compose run --rm tauceti ./tauceti doctor
```

Codex and Claude credentials should report `[ok]`. Missing `bubble`, `incus`, and `pi`
are expected for the standard host-mode deployment; they are only needed for Bubble
sandboxing or the DeepSeek and MiniMax agents.

## Worker options

Pass options to the long-running worker when starting it:

```bash
TAUCETI_WORKER_ARGS="--only roadmap --roadmap-only Topology" docker compose up -d
```

To keep those options for future `docker compose up` commands, put the setting in
`.env` at the repository root instead:

```dotenv
TAUCETI_WORKER_ARGS=--only roadmap --roadmap-only Topology
```

Then apply it normally:

```bash
docker compose up -d
```

The options are appended to `tauceti work --loop`. Edit or remove the line and run
`docker compose up -d` again to change or clear them; credentials, Lean toolchains,
and worker data are retained.

### Pacing

For a persistent custom quota curve, set `TAUCETI_PACE` in `.env`:

```dotenv
TAUCETI_PACE=0:10,100:90
```

Compose passes this directly to the worker. You can also put `--pace 0:10,100:90`
in `TAUCETI_WORKER_ARGS`; the command-line value wins if both are present. A blank or
unset `TAUCETI_PACE` keeps the default identity curve (`used% < elapsed%`).

The bundled Compose service runs one worker loop directly because Docker already
provides supervision. It therefore does not read `workers.toml` or the legacy
`workers.conf`. Declarative workers do support `pace = "0:10,100:90"` in
`workers.toml`; `workers.conf` supports the equivalent `--pace` only as input to the
one-shot `tauceti workers import` migration. See [Persistent workers](workers.md).

## Operations

Stop the deployment while retaining all data:

```bash
docker compose down
```

Update the checkout and replace the image while retaining volumes:

```bash
git pull
docker compose up -d --build
```

After a successful replacement, reclaim stopped containers, unused images, and build
cache on a dedicated worker host:

```bash
./scripts/docker-storage-prune
```

The command never prunes named volumes or running containers. Do not use it on a
shared Docker host unless every unused image and stopped container is disposable.

Because the image copies the local checkout, inspect or switch to the intended fork
branch before rebuilding. `git pull` is only an example update policy; Docker does not
fetch or select a branch itself.

To erase credentials, checkouts, caches, logs, and worker state:

```bash
docker compose down -v
```

This is destructive and requires fresh logins and dependency downloads on the
next start.

## Persistent storage

| Volume | Contents |
|---|---|
| `claude`, `codex` | Provider credentials, writable only by setup and the corresponding refresher |
| `claude-worker` | Refresh-token-free Claude mirror plus the worker's shared quota-bootstrap ledger |
| `codex-worker` | Access-token mirror, writable by the refresher and mounted read-only by the worker |
| `gh` | GitHub CLI credentials |
| `uv-cache` | Downloaded Python tools and packages |
| `elan-toolchains` | Lean toolchains selected by each checkout's `lean-toolchain` file |
| `checkouts` | Worker repositories and incremental Lean build artifacts |
| `state` | Scheduler state and isolated worker home |
| `logs` | Per-round logs |

## Storage policy

Compose uses Docker's rotating, compressed `local` logging driver for every service,
with three 10 MiB files per container. Completed worker logs are retained for 14 days
and then deleted at a round boundary; if they reach 1 GiB sooner, the oldest completed
logs are removed first. The active session log is never removed. Override these
defaults in `.env` with `TAUCETI_LOG_RETENTION_DAYS` and `TAUCETI_LOG_MAX_GIB`.

The writable Lake artifact cache has a 10 GiB soft limit, and authoring requires at
least 8 GiB of filesystem headroom. These defaults can be changed with
`TAUCETI_LAKE_CACHE_MAX_GIB` and `TAUCETI_LAKE_CACHE_MIN_FREE_GIB`. The compressed
Mathlib download cache is cleared when TauCeti changes Lean toolchain. Incremental
`.lake/build` trees are deliberately retained because deleting them turns the next
round into a large rebuild.

On a dedicated systemd host, install the bundled journal limits once:

```bash
sudo install -D -m 0644 deploy/tauceti-journald.conf /etc/systemd/journald.conf.d/tauceti-storage.conf
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=250M
```

This caps the system journal at 250 MiB and asks journald to preserve 2 GiB of free
space. Docker's own logs are managed by its log driver, not by deleting files below
`/var/lib/docker`.

If expanded Lean builds become the only way to recover urgently needed space, stop
the worker first and remove the `checkouts` named volume manually. That discards the
checkout and every incremental build but leaves credentials, scheduler state, logs,
and downloaded Elan toolchains intact; the next authoring round reclones and restores
public caches. This is an emergency operation, not routine maintenance. Inspect the
resolved volume name with `docker compose config --volumes` and `docker volume ls`
before deleting it, and never remove it while a round or agent is active.

Claude and Codex use rotating, single-consumer refresh tokens. One refresher owns
each provider credential and publishes a refresh-token-free mirror; the worker never
mounts the source provider credentials. The Claude mirror is writable by the worker
only because its quota-bootstrap ledger lives beside it; the refresher republishes the
access-only credential every polling cycle.

Elan's executable and proxies remain image-owned, while only downloaded Lean toolchains
are persisted. This avoids baking a TauCeti version into the image, lets each checkout's
`lean-toolchain` file remain authoritative, and prevents container replacement from
re-downloading a multi-gigabyte toolchain. On this dedicated deployment, obsolete official
toolchains are removed once no worker checkout requests them.

The refreshers check once a minute, renew within 90 minutes of expiry, avoid rotating
more than once per 10 minutes, and back off to 15 minutes after errors. Advanced
deployments can override these service environment variables:

- `TAUCETI_REFRESH_POLL_SECONDS`
- `TAUCETI_REFRESH_SKEW_SECONDS`
- `TAUCETI_REFRESH_MIN_INTERVAL_SECONDS`
- `TAUCETI_REFRESH_MAX_BACKOFF_SECONDS`

## Security

This deployment runs agents in host mode inside the worker container, not in a
Bubble round. Agents can access their provider access tokens and the GitHub credential
and have unrestricted network access. Use it only on a trusted, dedicated Docker host.

The deployment was adapted from
[eohjelle/TauCetiWorker-docker](https://github.com/eohjelle/TauCetiWorker-docker).
