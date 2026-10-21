# Install

claude-agent installs from crunchtools' signed repository, next to
`claude-code` from Anthropic's. Packages are noarch and need Python 3.9+.

## Why it matters

An agent image should be two repo files and one install line, so updates come
from the package manager, not from copying files.

## How it works

dnf:

```bash
sudo curl -fsSLo /etc/yum.repos.d/crunchtools.repo https://crunchtools.github.io/packages/rpm/crunchtools.repo
# plus Anthropic's claude-code.repo: see examples/Containerfile.ubi
sudo dnf install claude-agent
```

Add `claude-agent-matrix` to text it, `claude-agent-trentina` for the
crunchtools policy.

apt: see `examples/Containerfile.debian`.

Key fingerprints: Anthropic `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`,
crunchtools `588C E8BF 2F36 D77E 1B1E 545C C04F 530D 3931 F683`.

## Example

`/usr/share/doc/claude-agent/examples/` has `Containerfile.ubi`,
`Containerfile.debian` and `Containerfile.hummingbird`, which CI builds on every
change, plus `claude-agent.container` to run one under systemd:

```bash
podman build -f Containerfile.ubi -t my-agent .
cp claude-agent.container ~/.config/containers/systemd/
systemctl --user daemon-reload && systemctl --user start claude-agent
```

## Related

[run.md](run.md) · [controls.md](controls.md)
