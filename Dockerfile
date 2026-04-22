FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    bash \
    bubblewrap \
    ca-certificates \
    curl \
    git \
    gosu \
    jq \
    less \
    procps \
    python3 \
    python3-pip \
    python3-venv \
    ripgrep \
    sudo \
 && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.source=https://github.com/Sindycate/cage
LABEL org.opencontainers.image.description="cage - Docker isolation for AI coding assistants"

RUN useradd -m -s /bin/bash claude && \
    mkdir -p /home/claude/.local/bin /home/claude/.claude /home/claude/.ssh && \
    chown -R claude:claude /home/claude && \
    echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude

COPY entrypoint.sh /home/claude/entrypoint.sh
COPY mcp-relay /usr/local/bin/mcp-relay
RUN chmod 755 /home/claude/entrypoint.sh /usr/local/bin/mcp-relay

ENV HOME=/home/claude
ENV PATH=/home/claude/.local/bin:$PATH
ENV HISTFILE=/dev/null

# Install Claude Code as the claude user, then make home writable for UID remapping
WORKDIR /tmp
USER claude
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
RUN chmod -R a+rwX /home/claude

WORKDIR /home/claude

ENTRYPOINT ["/home/claude/entrypoint.sh"]
