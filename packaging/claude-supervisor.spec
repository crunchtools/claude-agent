# Packs the tree staged by .github/workflows/packages.yml from root/.
# Build with: rpmbuild -bb --define "pkg_version X.Y.Z" --define "stage DIR"

Name:           claude-supervisor
Version:        %{pkg_version}
Release:        1
Summary:        Run Claude Code as an unattended agent
License:        AGPL-3.0-or-later
URL:            https://github.com/crunchtools/claude-supervisor
BuildArch:      noarch
AutoReqProv:    no
# claude-code comes from Anthropic's signed repository (downloads.claude.ai).
Requires:       claude-code
Requires:       (python3 >= 3.9 or python3.12 or python3.11)
Requires:       bash
Requires:       jq
# Without tmux 3.2 or later the session runs in a pty (its control client needs
# attach-session -f); UBI 10 and Hummingbird carry no tmux at all.
Recommends:     tmux >= 3.2
# claude-agent was this package's name until 2.0.0: an upgrade replaces it, and
# anything that asks for it gets this. Both go in 3.0.0.
Provides:       claude-agent = %{version}-%{release}
Obsoletes:      claude-agent < %{version}-%{release}

%global __os_install_post %{nil}
%global _build_id_links none
%global _binary_payload w9.xzdio

%description
claude-supervisor turns Claude Code into an agent that runs with nobody at the
keyboard. `claude-supervisor run` holds an interactive session in tmux (or a pty) and takes
prompts and allowlisted commands over a local socket, holding them while a
turn runs. It ships the hooks an unattended session needs: circuit breakers
that halt a conversation that has run away, an audit record of every tool
call, and an opt-in hook that answers permission prompts. The hooks act only
in sessions started with CLAUDE_SUPERVISOR=1, so installing the package does not
change anyone's interactive Claude Code.

%package matrix
Summary:        Reach a claude-supervisor session by Matrix text
Requires:       claude-supervisor = %{version}-%{release}
Provides:       claude-agent-matrix = %{version}-%{release}
Obsoletes:      claude-agent-matrix < %{version}-%{release}

%description matrix
A bridge that carries texts from allowed Matrix senders to the session that
claude-supervisor run holds, and texts back each turn's final answer and command
results, with a typing indicator while a turn runs. claude-supervisor run starts
it when CLAUDE_SUPERVISOR_MATRIX_HOMESERVER is set.

%package trentina
Summary:        Crunchtools agent policy for claude-supervisor
Requires:       claude-supervisor = %{version}-%{release}
Provides:       claude-agent-trentina = %{version}-%{release}
Obsoletes:      claude-agent-trentina < %{version}-%{release}

%description trentina
The policy crunchtools agents run under: the Trentina gateway is the only
MCP server, claude.ai connectors and web tools are off, only managed hooks
run, and Claude Code's local file memory stays empty. It applies to every
Claude Code session on the machine.

%install
cp -a %{stage}/. %{buildroot}/

%files
%license /usr/share/licenses/claude-supervisor/LICENSE
%doc /usr/share/doc/claude-supervisor/README.md
%doc /usr/share/doc/claude-supervisor/CHANGELOG.md
%doc /usr/share/doc/claude-supervisor/examples
/usr/bin/claude-supervisor
/usr/bin/claude-agent
%dir /usr/libexec/claude-supervisor
/usr/libexec/claude-supervisor/hooks.py
/usr/libexec/claude-supervisor/allow-hook.sh
/usr/libexec/claude-supervisor/supervisor.py
%dir /etc/claude-code
%dir /etc/claude-code/managed-settings.d
%config(noreplace) /etc/claude-code/managed-settings.d/10-claude-supervisor.json

%files matrix
/usr/libexec/claude-supervisor/matrix-bridge.py

%files trentina
/usr/libexec/claude-supervisor/deny-local-memory.sh
%config(noreplace) /etc/claude-code/managed-settings.d/20-claude-supervisor-trentina.json
