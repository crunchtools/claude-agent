# A reference agent image: Claude Code plus claude-agent on UBI 10.
#
# This is the "build your own" starting point the docs describe, not an image
# crunchtools publishes. CI builds it from the packages it has just built
# (dropped into rpms/); without any there, it installs from the published
# repositories, as a user's build would.
#
#   podman build -t my-agent .
#   podman run --rm my-agent claude-agent status

FROM registry.access.redhat.com/ubi10/ubi:latest

LABEL maintainer="crunchtools <scott.mccarty@crunchtools.com>"
LABEL description="Reference image: Claude Code as an unattended agent with claude-agent"
LABEL org.opencontainers.image.source="https://github.com/crunchtools/claude-agent"
LABEL org.opencontainers.image.description="Reference image: Claude Code as an unattended agent with claude-agent"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Anthropic's signed repository for claude-code, and crunchtools' for
# claude-agent. Repository definitions and keys are the kind of configuration
# an image may carry (constitution XIV).
COPY rpms/ /tmp/rpms/
RUN printf '%s\n' '[claude-code]' 'name=Claude Code' \
        'baseurl=https://downloads.claude.ai/claude-code/rpm/stable' \
        'enabled=1' 'gpgcheck=1' 'gpgkey=https://downloads.claude.ai/keys/claude-code.asc' \
        > /etc/yum.repos.d/claude-code.repo && \
    if ls /tmp/rpms/*.rpm >/dev/null 2>&1; then \
        dnf install -y --nodocs --setopt=install_weak_deps=0 /tmp/rpms/*.rpm; \
    else \
        curl -fsSL -o /etc/yum.repos.d/crunchtools.repo \
            https://crunchtools.github.io/packages/rpm/crunchtools.repo && \
        dnf install -y --nodocs --setopt=install_weak_deps=0 claude-agent; \
    fi && \
    dnf clean all && rm -rf /tmp/rpms && \
    claude --version && claude-agent --version

RUN useradd --uid 1000 --user-group --create-home --home-dir /home/agent agent

# Every Claude Code session in this container is the agent's.
ENV CLAUDE_AGENT=1 \
    DISABLE_AUTOUPDATER=1

USER 1000
WORKDIR /home/agent
CMD ["claude-agent", "status"]
