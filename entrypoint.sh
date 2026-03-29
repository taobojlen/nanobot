#!/bin/bash
set -e

# Set permissive umask so files created by root inside the container
# are writable by the host user (who may not be root).
umask 0000

# Start cron daemon in background
cron

# Initial Obsidian vault sync if vault is empty or missing
if [ ! -d /root/taos-obsidian-vault ] || [ -z "$(ls -A /root/taos-obsidian-vault 2>/dev/null)" ]; then
    echo "Obsidian vault empty or missing, running initial sync..."
    ob sync --path /root/taos-obsidian-vault || echo "WARNING: Initial obsidian sync failed (auth may not be configured yet)"
fi

exec tini -- nanobot "$@"
