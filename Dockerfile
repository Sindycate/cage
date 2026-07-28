ARG CAGE_BASE=cage-base:latest
FROM ${CAGE_BASE}

LABEL org.opencontainers.image.source=https://github.com/Sindycate/cage
LABEL org.opencontainers.image.description="cage - Docker isolation for AI coding assistants"

RUN useradd -m -s /bin/bash claude && \
    mkdir -p /home/claude/.local/bin /home/claude/.claude /home/claude/.ssh && \
    chown -R claude:claude /home/claude && \
    echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude

COPY entrypoint.sh /home/claude/entrypoint.sh
RUN chmod 755 /home/claude/entrypoint.sh

ENV HOME=/home/claude
ENV PATH=/home/claude/.local/bin:$PATH

# Install Claude Code as the claude user, then make home writable for UID remapping
WORKDIR /tmp
USER claude
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
RUN chmod -R a+rwX /home/claude

WORKDIR /home/claude

ENTRYPOINT ["/home/claude/entrypoint.sh"]
