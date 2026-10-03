"""Exercise the standalone installer against a real local download server."""

import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("standalone_installer", ROOT / "scripts" / "install.py")
        cls.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.installer)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.prefix = self.root / "installed release"
        self.bin_dir = self.root / "local bin"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.downloads)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def publish(self, version="1.2.3", extra=(), empty=False, **manifest_changes):
        """Create a tiny functioning Python package; callers can add hostile members."""
        filename = f"miniagent-{version}.tar.gz"
        archive = self.downloads / filename
        files = {
            "agent.py": "from miniagent.cli import main\nraise SystemExit(main())\n",
            "miniagent/__init__.py": "",
            "miniagent/cli.py": f"def main():\n    print('MiniAgent {version}')\n    return 0\n",
        }
        with tarfile.open(archive, "w:gz") as output:
            for name, content in (() if empty else files.items()):
                raw = content.encode("utf-8")
                member = tarfile.TarInfo(f"miniagent-{version}/{name}")
                member.size = len(raw)
                output.addfile(member, io.BytesIO(raw))
            for member, content in extra:
                output.addfile(member, io.BytesIO(content) if content is not None else None)
        raw = archive.read_bytes()
        manifest = {"version": version, "python_min": "3.10", "filename": filename,
                    "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        manifest.update(manifest_changes)
        (self.downloads / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def install(self):
        return self.installer.install(self.url, self.prefix, self.bin_dir)

    def test_http_install_runs_with_system_python_without_site_packages(self):
        expected = self.publish()
        manifest, launcher = self.install()
        self.assertEqual(manifest, expected)
        self.assertEqual(launcher, self.bin_dir / "miniagent")
        command = shlex.split(launcher.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(command[:2], ["exec", sys.executable])
        self.assertEqual(command[-1], "$@")
        installed_agent = Path(command[2])
        installed_agent.relative_to(self.prefix)
        result = subprocess.run([sys.executable, "-S", str(installed_agent), "--version"],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), "MiniAgent 1.2.3")
        self.assertFalse(list(self.prefix.glob(".install-*")))
        self.assertFalse(list(self.bin_dir.glob(".miniagent-*")))
        if os.name != "nt":
            self.assertTrue(os.access(launcher, os.X_OK))
            direct = subprocess.run([str(launcher), "--version"], capture_output=True, text=True,
                                    timeout=10, check=True)
            self.assertEqual(direct.stdout.strip(), "MiniAgent 1.2.3")

    def test_reinstall_is_idempotent(self):
        self.publish()
        first_manifest, first_launcher = self.install()
        original = first_launcher.read_bytes()
        first_files = sorted(path.relative_to(self.prefix) for path in self.prefix.rglob("*"))
        second_manifest, second_launcher = self.install()
        self.assertEqual((second_manifest, second_launcher), (first_manifest, first_launcher))
        self.assertEqual(second_launcher.read_bytes(), original)
        self.assertEqual(sorted(path.relative_to(self.prefix) for path in self.prefix.rglob("*")), first_files)

    def test_shell_bootstrap_downloads_installer_and_launches_command(self):
        shell = shutil.which("sh") if os.name != "nt" else None
        if os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            shell = str(candidate) if candidate.is_file() else None
        if shell is None:
            self.skipTest("POSIX shell unavailable")
        self.publish()
        for name in ("install.py", "install.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.downloads / name)
        result = subprocess.run(
            [shell, str(self.downloads / "install.sh"), self.url, "--prefix", str(self.prefix),
             "--bin-dir", str(self.bin_dir)], env=dict(os.environ, PYTHON=sys.executable),
            cwd=self.root, capture_output=True, text=True, timeout=20, check=True)
        self.assertIn("Installed MiniAgent 1.2.3", result.stdout)
        launched = subprocess.run([shell, str(self.bin_dir / "miniagent"), "--version"],
                                  capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(launched.stdout.strip(), "MiniAgent 1.2.3")

    def test_unwritable_shell_config_reports_success_and_manual_path(self):
        self.publish()
        output = io.StringIO()
        with patch.object(self.installer, "add_to_path", side_effect=PermissionError), \
                patch("sys.stdout", output):
            code = self.installer.main([self.url, "--prefix", str(self.prefix),
                                        "--bin-dir", str(self.bin_dir), "--add-to-path"])
        self.assertEqual(code, 0)
        self.assertTrue((self.bin_dir / "miniagent").is_file())
        self.assertIn("Installed successfully", output.getvalue())
        self.assertIn("export PATH=", output.getvalue())

    def test_successful_update_switches_launcher_and_preserves_old_release(self):
        self.publish()
        _, launcher = self.install()
        old_agent = Path(shlex.split(launcher.read_text(encoding="utf-8").splitlines()[-1])[2])
        old_source = old_agent.read_bytes()
        self.publish("1.2.4")
        updated, _ = self.install()
        self.assertEqual(updated["version"], "1.2.4")
        new_agent = Path(shlex.split(launcher.read_text(encoding="utf-8").splitlines()[-1])[2])
        self.assertNotEqual(old_agent, new_agent)
        self.assertEqual(old_agent.read_bytes(), old_source)
        output = subprocess.run([sys.executable, "-S", str(new_agent)], capture_output=True, text=True,
                                timeout=10, check=True)
        self.assertEqual(output.stdout.strip(), "MiniAgent 1.2.4")

    def test_checksum_failure_keeps_previous_installation(self):
        self.publish()
        _, launcher = self.install()
        previous = launcher.read_bytes()
        self.publish("1.2.4", sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.install()
        self.assertEqual(launcher.read_bytes(), previous)
        self.assertEqual(len(list((self.prefix / "releases").iterdir())), 1)

    def test_unsafe_tar_members_are_rejected_without_replacing_launcher(self):
        self.publish()
        _, launcher = self.install()
        previous = launcher.read_bytes()
        cases = [
            ("miniagent-1.2.4/../../escaped.py", tarfile.REGTYPE, ""),
            ("/tmp/miniagent-escape.py", tarfile.REGTYPE, ""),
            (r"miniagent-1.2.4/..\escaped.py", tarfile.REGTYPE, ""),
            ("miniagent-1.2.4/C:/escaped.py", tarfile.REGTYPE, ""),
            ("miniagent-1.2.4/link", tarfile.SYMTYPE, "../../escaped.py"),
            ("miniagent-1.2.4/link", tarfile.LNKTYPE, "miniagent-1.2.4/agent.py"),
            ("miniagent-1.2.4/pipe", tarfile.FIFOTYPE, ""),
            ("miniagent-1.2.4/agent.py", tarfile.REGTYPE, ""),
        ]
        for name, member_type, link in cases:
            with self.subTest(name=name, member_type=member_type):
                member = tarfile.TarInfo(name)
                member.type = member_type
                member.linkname = link
                self.publish("1.2.4", extra=[(member, b"" if member.isreg() else None)])
                with self.assertRaises(ValueError):
                    self.install()
                self.assertEqual(launcher.read_bytes(), previous)
                self.assertEqual(len(list((self.prefix / "releases").iterdir())), 1)
                self.assertFalse(list(self.prefix.glob(".install-*")))
        self.assertFalse((self.root / "escaped.py").exists())

    def test_incomplete_archive_keeps_previous_installation(self):
        self.publish()
        _, launcher = self.install()
        previous = launcher.read_bytes()
        self.publish("1.2.4", empty=True)
        with self.assertRaisesRegex(ValueError, "entry point"):
            self.install()
        self.assertEqual(launcher.read_bytes(), previous)

    def test_unmanaged_command_is_never_overwritten(self):
        self.publish()
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "miniagent"
        launcher.write_text("#!/bin/sh\necho existing-command\n", encoding="utf-8")
        original = launcher.read_bytes()
        with self.assertRaisesRegex(ValueError, "not managed"):
            self.install()
        self.assertEqual(launcher.read_bytes(), original)
        self.assertFalse(self.prefix.exists())

    def test_invalid_manifests_do_not_create_installation(self):
        cases = [{"filename": "../release.tar.gz"}, {"size": True}, {"size": 0},
                 {"sha256": "invalid"}, {"python_min": "3.99"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.publish(**changes)
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.prefix.exists())
                self.assertFalse(self.bin_dir.exists())

    def test_path_setup_preserves_existing_settings_and_is_idempotent(self):
        home = self.root / "home"
        home.mkdir()
        profile = home / ".profile"
        rc = home / ".bashrc"
        profile.write_text("# Existing profile\nexport EDITOR=vi\n", encoding="utf-8")
        rc.write_text("# Existing bash settings\n", encoding="utf-8")
        with patch.object(self.installer.Path, "home", return_value=home), \
                patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            line = self.installer.add_to_path(self.bin_dir)
            self.installer.add_to_path(self.bin_dir)
        self.assertTrue(profile.read_text(encoding="utf-8").startswith("# Existing profile\nexport EDITOR=vi\n"))
        self.assertTrue(rc.read_text(encoding="utf-8").startswith("# Existing bash settings\n"))
        for path in (profile, rc):
            self.assertEqual(path.read_text(encoding="utf-8").splitlines().count(line), 1)
