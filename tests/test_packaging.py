"""packaging/files.txt, the spec's %files, root/ and stage.sh agree."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
SPEC = (TOP / "packaging/claude-agent.spec").read_text()


def manifest() -> list[tuple[str, str, str]]:
    lines = (TOP / "packaging/files.txt").read_text().splitlines()
    return [tuple(line.split()) for line in lines if line.strip() and not line.startswith("#")]


# %files lines that stay in the section; any other % line ends it.
KEEP = ("%config", "%license", "%doc", "%dir")


def spec_files() -> dict[str, set[str]]:
    """Each package's file paths from the spec's %files sections, directories left out."""
    found: dict[str, set[str]] = {}
    package = None
    for line in SPEC.splitlines():
        header = re.match(r"%files(?:\s+(\S+))?$", line)
        if header:
            package = "claude-agent" + (f"-{header[1]}" if header[1] else "")
            found[package] = set()
        elif package and line.startswith("%") and not line.startswith(KEEP):
            package = None
        elif package and line.strip() and not line.startswith(("%dir", "%license", "%doc")):
            found[package].add(line.split()[-1])
    return found


class PackagingTest(unittest.TestCase):
    def test_every_file_in_root_is_packaged_once(self) -> None:
        shipped = sorted(path for _, _, path in manifest())
        in_tree = sorted(
            "/" + str(p.relative_to(TOP / "root"))
            for p in (TOP / "root").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
        self.assertEqual(shipped, in_tree)

    def test_spec_files_match_the_manifest(self) -> None:
        expected: dict[str, set[str]] = {}
        for package, _, path in manifest():
            expected.setdefault(package, set()).add(path)
        self.assertEqual(spec_files(), expected)

    def test_every_package_has_a_debian_control_file(self) -> None:
        for package in {package for package, _, _ in manifest()}:
            control = (TOP / f"packaging/debian/{package}.control.in").read_text()
            self.assertIn(f"Package: {package}\n", control)
            self.assertIn("Version: @VERSION@-1\n", control)

    def test_conffiles_are_the_managed_settings_fragments(self) -> None:
        for package in {package for package, _, _ in manifest()}:
            conffiles = (TOP / f"packaging/debian/{package}.conffiles").read_text().split()
            expected = [p for pkg, _, p in manifest() if pkg == package and p.startswith("/etc/")]
            self.assertEqual(conffiles, expected)

    def test_stage_installs_one_package_with_its_modes(self) -> None:
        with tempfile.TemporaryDirectory() as dest:
            subprocess.run([str(TOP / "packaging/stage.sh"), "claude-agent-trentina", dest], check=True)
            staged = sorted("/" + str(p.relative_to(dest)) for p in Path(dest).rglob("*") if p.is_file())
            expected = sorted(p for pkg, _, p in manifest() if pkg == "claude-agent-trentina")
            self.assertEqual(staged, expected)
            for _, mode, path in manifest():
                installed = Path(dest) / path.lstrip("/")
                if installed.exists():
                    self.assertEqual(oct(installed.stat().st_mode & 0o777)[2:], mode[1:])

    def test_core_package_carries_license_and_docs(self) -> None:
        with tempfile.TemporaryDirectory() as dest:
            subprocess.run([str(TOP / "packaging/stage.sh"), "claude-agent", dest], check=True)
            self.assertTrue((Path(dest) / "usr/share/licenses/claude-agent/LICENSE").is_file())
            self.assertTrue((Path(dest) / "usr/share/doc/claude-agent/CHANGELOG.md").is_file())
            examples = sorted(p.name for p in (Path(dest) / "usr/share/doc/claude-agent/examples").iterdir())
            self.assertEqual(examples, sorted(p.name for p in (TOP / "examples").iterdir()))

    def test_cli_version_has_a_changelog_entry(self) -> None:
        version = re.search(r'^VERSION = "([^"]+)"', (TOP / "root/usr/bin/claude-agent").read_text(), re.M)[1]
        self.assertIn(f"## [{version}]", (TOP / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
