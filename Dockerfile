ARG CAGE_BASE=cage-base:latest
FROM ${CAGE_BASE}
ARG CAGE_VERSION=dev

LABEL org.opencontainers.image.source=https://github.com/Sindycate/cage
LABEL org.opencontainers.image.description="cage - Docker isolation for AI coding assistants"

RUN useradd -m -s /bin/bash claude && \
    mkdir -p /home/claude/.local/bin /home/claude/.claude /home/claude/.ssh && \
    chown -R claude:claude /home/claude && \
    echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude

ENV HOME=/home/claude
ENV PATH=/home/claude/.local/bin:$PATH

# Install Claude Code as the claude user. Normalize the files in the same layer
# that creates them so Docker does not retain a second copy of the tool tree.
WORKDIR /tmp
USER claude
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    chmod -R a+rwX /home/claude
USER root

COPY entrypoint.sh /home/claude/entrypoint.sh
RUN chmod 755 /home/claude/entrypoint.sh

WORKDIR /home/claude

ENTRYPOINT ["/home/claude/entrypoint.sh"]

LABEL org.opencontainers.image.version="${CAGE_VERSION}" \
      io.cage.managed="true" \
      io.cage.role="claude" \
      io.cage.version="${CAGE_VERSION}"
