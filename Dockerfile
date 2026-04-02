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

RUN useradd -m -s /bin/bash claude && \
    mkdir -p /home/claude/.local/bin /home/claude/.claude && \
    chown -R claude:claude /home/claude

COPY entrypoint.sh /home/claude/entrypoint.sh
RUN chown claude:claude /home/claude/entrypoint.sh && chmod 755 /home/claude/entrypoint.sh

USER claude
ENV HOME=/home/claude
ENV PATH=/home/claude/.local/bin:$PATH
ENV HISTFILE=/dev/null

WORKDIR /tmp
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /workspace
ENTRYPOINT ["/home/claude/entrypoint.sh"]
