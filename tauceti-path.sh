# shellcheck shell=sh
# Debian's /etc/profile replaces the inherited PATH for login shells; restore the agent-safe helpers.
export PATH="/opt/tauceti/scripts:$PATH"
