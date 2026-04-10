FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    less \
    procps \
    ripgrep \
 && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash claude && \
    mkdir -p /home/claude/.local/bin /home/claude/.claude /home/claude/.ssh && \
    chown -R claude:claude /home/claude

COPY entrypoint.sh /home/claude/entrypoint.sh
RUN chown claude:claude /home/claude/entrypoint.sh && chmod 755 /home/claude/entrypoint.sh

USER claude
ENV HOME=/home/claude
ENV PATH=/home/claude/.local/bin:$PATH
ENV HISTFILE=/dev/null

WORKDIR /tmp
# Make home dir writable by any UID for --user compatibility
# (container is sandboxed: cap_drop ALL, no-new-privileges, read-only host mounts)
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    chmod -R a+rwX /home/claude

WORKDIR /home/claude

ENTRYPOINT ["/home/claude/entrypoint.sh"]
