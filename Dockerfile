# A disposable Linux host for the Tau Ceti worker. The agent runs in host mode inside this
# container; it does not use the more restrictive Bubble sandbox.
FROM node:22-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime tools for tauceti and its agents, plus a native toolchain for Lean builds. Debian
# package revisions deliberately track Bookworm's security repository instead of being frozen.
# hadolint ignore=DL3008
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        python3 \
        python3-requests \
        ripgrep \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Lean toolchains are shared container infrastructure, not per-worker HOME state. Keep Elan itself
# image-owned under /opt/elan, while Compose persists only /opt/elan/toolchains so the checkout's
# lean-toolchain file remains authoritative across image and container replacement. Expose Elan's
# proxies and uv/uvx from /usr/local/bin so they remain discoverable after TauCeti replaces HOME and
# an agent starts a login shell.
ENV ELAN_HOME=/opt/elan \
    UV_CACHE_DIR=/root/.cache/uv \
    DISABLE_AUTOUPDATER=1 \
    IS_SANDBOX=1 \
    PYTHONUNBUFFERED=1
RUN set -eux; \
    curl -fsSL https://elan.lean-lang.org/elan-init.sh \
      | sh -s -- -y --default-toolchain none --no-modify-path; \
    mkdir -p "$ELAN_HOME/toolchains"; \
    for tool in "$ELAN_HOME"/bin/*; do \
      install -m 0755 "$tool" "/usr/local/bin/$(basename "$tool")"; \
    done; \
    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_UNMANAGED_INSTALL=/usr/local/bin sh; \
    mkdir -p /tmp/tauceti-worker-home; \
    env HOME=/tmp/tauceti-worker-home /bin/bash -lc \
      'test "$ELAN_HOME" = /opt/elan; \
       test "$(command -v elan)" = /usr/local/bin/elan; \
       test "$(command -v lake)" = /usr/local/bin/lake; \
       test "$(command -v lean)" = /usr/local/bin/lean; \
       test "$(command -v uv)" = /usr/local/bin/uv; \
       test "$(command -v uvx)" = /usr/local/bin/uvx'; \
    rm -rf /tmp/tauceti-worker-home

# Subscription authentication is performed at runtime and persisted by compose.yaml. Pin the
# clients so rebuilding a deployment uses known client versions; the scheduled image build detects
# when a pinned client or its service contract stops working. Keep this after Elan so a client-version
# bump does not invalidate the installer layer.
ARG CLAUDE_CODE_VERSION=2.1.220
ARG CODEX_VERSION=0.145.0
RUN npm install -g \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}"

# Keep dependency layers reusable while making every image contain the exact checked-out worker
# revision under test (including pull-request changes).
WORKDIR /opt/tauceti
COPY tauceti pyproject.toml ./
COPY prompts ./prompts
COPY scripts ./scripts
COPY tauceti_worker ./tauceti_worker
COPY tauceti-path.sh /etc/profile.d/tauceti-path.sh

RUN install -m 0755 scripts/oauth_refresh_loop.py /usr/local/bin/tauceti-oauth-refresh \
    && install -m 0755 scripts/docker-entrypoint /usr/local/bin/tauceti-entrypoint \
    && chmod 0644 /etc/profile.d/tauceti-path.sh \
    && chmod 0755 tauceti scripts/claim.sh scripts/gh-safe-pr-create scripts/git-safe-push \
        scripts/tauceti-axioms scripts/tauceti-lint-env \
    && ./tauceti --help >/dev/null \
    && mkdir -p /tmp/tauceti-worker-home \
    && env HOME=/tmp/tauceti-worker-home /bin/bash -lc \
      'test "$(command -v lake)" = /usr/local/bin/lake; \
       test "$(command -v git-safe-push)" = /opt/tauceti/scripts/git-safe-push' \
    && rm -rf /tmp/tauceti-worker-home \
    && git config --system user.name "TauCeti Worker" \
    && git config --system user.email "tauceti-worker@users.noreply.github.com" \
    && git config --system credential.https://github.com.helper "" \
    && git config --system --add credential.https://github.com.helper "!gh auth git-credential"

ENTRYPOINT ["/usr/local/bin/tauceti-entrypoint"]
CMD ["./tauceti", "work", "--loop"]
