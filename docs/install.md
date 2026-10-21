# Install and build an agent image

claude-agent installs from crunchtools' signed repositories, next to
`claude-code` from Anthropic's. Both repositories offer dnf and apt; the
packages are noarch and run on any Python 3.9 or newer.

## Why it matters

An agent image should be two repository files and one install line, so the
controls in it are the same ones everyone else runs, updated by the package
manager rather than copied into each Containerfile.

## How it works

Tested on every release: UBI 9 and 10, Fedora, Hummingbird, Debian 12 and 13,
Ubuntu 24.04 and 26.04.

**dnf** (RHEL, UBI, Fedora, Hummingbird builder images):

```bash
sudo tee /etc/yum.repos.d/claude-code.repo <<'EOF'
[claude-code]
name=Claude Code
baseurl=https://downloads.claude.ai/claude-code/rpm/stable
enabled=1
gpgcheck=1
gpgkey=https://downloads.claude.ai/keys/claude-code.asc
EOF
sudo curl -fsSL -o /etc/yum.repos.d/crunchtools.repo \
  https://crunchtools.github.io/packages/rpm/crunchtools.repo
sudo dnf install claude-agent
```

**apt** (Debian, Ubuntu):

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL -o /etc/apt/keyrings/claude-code.asc https://downloads.claude.ai/keys/claude-code.asc
echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
  | sudo tee /etc/apt/sources.list.d/claude-code.list
sudo curl -fsSL -o /etc/apt/keyrings/crunchtools.asc https://crunchtools.github.io/packages/crunchtools.asc
sudo curl -fsSL -o /etc/apt/sources.list.d/crunchtools.sources \
  https://crunchtools.github.io/packages/deb/crunchtools.sources
sudo apt update && sudo apt install claude-agent
```

Verify Anthropic's key fingerprint (`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF
1A7E CACE`) and crunchtools' (`588C E8BF 2F36 D77E 1B1E 545C C04F 530D 3931
F683`) before trusting them.

## Example

The repo's [`Containerfile`](../Containerfile) is a complete reference image
on UBI 10. The parts that matter:

```dockerfile
FROM registry.access.redhat.com/ubi10/ubi:latest
RUN printf '%s\n' '[claude-code]' 'name=Claude Code' \
        'baseurl=https://downloads.claude.ai/claude-code/rpm/stable' \
        'enabled=1' 'gpgcheck=1' 'gpgkey=https://downloads.claude.ai/keys/claude-code.asc' \
        > /etc/yum.repos.d/claude-code.repo && \
    curl -fsSL -o /etc/yum.repos.d/crunchtools.repo \
        https://crunchtools.github.io/packages/rpm/crunchtools.repo && \
    dnf install -y claude-agent && dnf clean all
RUN useradd --uid 1000 --create-home agent
ENV CLAUDE_AGENT=1
USER 1000
```

Add `claude-agent-trentina` for the crunchtools policy. Mount the agent's
identity (`CLAUDE.md`, settings) and its home at run time rather than baking
them in.

## Related

- [hooks.md](hooks.md): what `CLAUDE_AGENT=1` turns on
- [policy.md](policy.md): the machine-wide policy package
