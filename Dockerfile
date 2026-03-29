FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install Node.js 24 and obsidian-headless
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git openssh-client cron tini && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install obsidian-headless CLI globally
RUN npm install -g obsidian-headless

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot && touch nanobot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanobot

# Copy Python source and install (changes often)
COPY nanobot/ nanobot/
RUN uv pip install --system --no-cache .

# Create config directory and obsidian vault directory
RUN mkdir -p /root/.nanobot /root/taos-obsidian-vault

# Set up obsidian-sync cron job (every 30 minutes)
RUN echo '*/30 * * * * root ob sync --path /root/taos-obsidian-vault >> /var/log/obsidian-sync.log 2>&1' > /etc/cron.d/obsidian-sync && \
    chmod 0644 /etc/cron.d/obsidian-sync

# Gateway default port
EXPOSE 18790

COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["status"]
