"""Offline checks for standalone installer configuration."""

import importlib.util
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("standalone_installer", ROOT / "scripts" / "install.py")
        cls.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.installer)


    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin_dir = self.root / "local bin"

    def test_path_setup_preserves_existing_settings_and_is_idempotent(self):
        home = self.root / "home"
        home.mkdir()
        profile = home / ".profile"
        rc = home / ".bashrc"
        profile.write_text("# Existing profile\nexport EDITOR=vi\n", encoding="utf-8")
        rc.write_text("# Existing bash settings\n", encoding="utf-8")
        with patch.object(self.installer.Path, "home", return_value=home), \
                patch.object(self.installer, "WINDOWS", False), \
                patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            line = self.installer.add_to_path(self.bin_dir)
            self.installer.add_to_path(self.bin_dir)
        self.assertTrue(profile.read_text(encoding="utf-8").startswith("# Existing profile\nexport EDITOR=vi\n"))
        self.assertTrue(rc.read_text(encoding="utf-8").startswith("# Existing bash settings\n"))
        for path in (profile, rc):
            self.assertEqual(path.read_text(encoding="utf-8").splitlines().count(line), 1)

    def bundle(self, entry=b"# Test release\n"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in ("agent.py", "miniagent/__init__.py"):
                content = entry if name == "agent.py" else b"# Test release\n"
                member = tarfile.TarInfo("miniagent-1.0.0/" + name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        data = stream.getvalue()
        manifest = {"version": "1.0.0", "python_min": "3.10", "filename": "miniagent-1.0.0.tar.gz",
                    "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return json.dumps(manifest).encode(), data

    def test_windows_install_upgrade_and_failed_download_preserve_launcher(self):
        prefix = self.root / "中文 app % name!"
        manifest, data = self.bundle()
        with patch.object(self.installer, "WINDOWS", True):
            for _ in range(2):
                with patch.object(self.installer, "download", side_effect=[manifest, data]):
                    installed, launcher = self.installer.install("https://example.test", prefix, self.bin_dir)
                self.assertEqual(launcher.name, "miniagent.cmd")
                text = launcher.read_text(encoding="utf-8")
                self.assertIn("setlocal DisableDelayedExpansion", text)
                self.assertIn("中文 app %% name!", text)
                self.assertIn('" %*', text)
                self.assertTrue((prefix / "releases" / ("1.0.0-" + installed["sha256"][:16]) / "agent.py").is_file())
            original = launcher.read_bytes()
            with patch.object(self.installer, "download", side_effect=[manifest, b"bad"]):
                with self.assertRaisesRegex(ValueError, "checksum/size mismatch"):
                    self.installer.install("https://example.test", prefix, self.bin_dir)
            self.assertEqual(launcher.read_bytes(), original)

    def test_windows_refuses_to_overwrite_unmanaged_command(self):
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "miniagent.cmd"
        launcher.write_text("@echo existing\n", encoding="utf-8")
        with patch.object(self.installer, "WINDOWS", True), patch.object(self.installer, "download") as download:
            with self.assertRaisesRegex(ValueError, "not managed"):
                self.installer.install("https://example.test", self.root / "app", self.bin_dir)
            download.assert_not_called()
        self.assertEqual(launcher.read_text(encoding="utf-8"), "@echo existing\n")

    def test_posix_install_still_generates_shell_launcher(self):
        manifest, data = self.bundle()
        with patch.object(self.installer, "WINDOWS", False), \
                patch.object(self.installer, "download", side_effect=[manifest, data]):
            _, launcher = self.installer.install("https://example.test", self.root / "app", self.bin_dir)
        self.assertEqual(launcher.name, "miniagent")
        self.assertTrue(launcher.read_text(encoding="utf-8").startswith("#!/bin/sh\n"))

    def test_windows_user_path_is_preserved_and_case_insensitively_idempotent(self):
        registry = MagicMock()
        registry.REG_SZ, registry.REG_EXPAND_SZ = 1, 2
        value = [r"%USERPROFILE%\existing"]
        registry.QueryValueEx.side_effect = lambda *args: (value[0], registry.REG_EXPAND_SZ)
        registry.SetValueEx.side_effect = lambda key, name, reserved, kind, text: value.__setitem__(0, text)
        with patch.object(self.installer, "WINDOWS", True), \
                patch.dict(sys.modules, {"winreg": registry}), \
                patch.object(self.installer, "notify_environment_change"):
            line = self.installer.add_to_path(self.bin_dir)
            value[0] = value[0].upper()
            self.installer.add_to_path(self.bin_dir)
        registry.SetValueEx.assert_called_once()
        self.assertTrue(value[0].startswith(r"%USERPROFILE%\EXISTING;"))
        self.assertTrue(line.startswith("$env:Path = '"))

    def test_windows_defaults_use_local_app_data(self):
        with patch.object(self.installer, "WINDOWS", True), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(self.installer.default_prefix(), self.root / "MiniAgent")

    @unittest.skipUnless(os.name == "nt", "Requires native Windows cmd.exe")
    def test_native_windows_launcher_preserves_arguments_and_exit_status(self):
        manifest, data = self.bundle(b"import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        with patch.object(self.installer, "download", side_effect=[manifest, data]):
            _, launcher = self.installer.install("https://example.test", self.root / "中文 % app!", self.bin_dir)
        result = subprocess.run([str(launcher), "hello world", "中文!"], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["hello world", "中文!"])
