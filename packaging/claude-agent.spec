# Packs the tree staged by .github/workflows/packages.yml from root/.
# Build with: rpmbuild -bb --define "pkg_version X.Y.Z" --define "stage DIR"

Name:           claude-agent
Version:        %{pkg_version}
Release:        1
Summary:        Run Claude Code as an unattended agent
License:        AGPL-3.0-or-later
URL:            https://github.com/crunchtools/claude-agent
BuildArch:      noarch
AutoReqProv:    no
# claude-code comes from Anthropic's signed repository (downloads.claude.ai).
Requires:       claude-code
Requires:       (python3 >= 3.9 or python3.12 or python3.11)
Requires:       bash
Requires:       jq

%global __os_install_post %{nil}
%global _build_id_links none
%global _binary_payload w9.xzdio

%description
claude-agent turns Claude Code into an agent that runs with nobody at the
keyboard. It ships the hooks an unattended session needs: circuit breakers
that halt a conversation that has run away, an audit record of every tool
call, and an opt-in hook that answers permission prompts. The hooks act only
in sessions started with CLAUDE_AGENT=1, so installing the package does not
change anyone's interactive Claude Code.

%package trentina
Summary:        Crunchtools agent policy for claude-agent
Requires:       claude-agent = %{version}-%{release}

%description trentina
The policy crunchtools agents run under: the Trentina gateway is the only
MCP server, claude.ai connectors and web tools are off, only managed hooks
run, and Claude Code's local file memory stays empty. It applies to every
Claude Code session on the machine.

%install
cp -a %{stage}/. %{buildroot}/

%files
%license /usr/share/licenses/claude-agent/LICENSE
%doc /usr/share/doc/claude-agent/README.md
%doc /usr/share/doc/claude-agent/CHANGELOG.md
/usr/bin/claude-agent
%dir /usr/libexec/claude-agent
/usr/libexec/claude-agent/hooks.py
/usr/libexec/claude-agent/allow-hook.sh
%dir /etc/claude-code
%dir /etc/claude-code/managed-settings.d
%config(noreplace) /etc/claude-code/managed-settings.d/10-claude-agent.json

%files trentina
/usr/libexec/claude-agent/deny-local-memory.sh
%config(noreplace) /etc/claude-code/managed-settings.d/20-claude-agent-trentina.json
