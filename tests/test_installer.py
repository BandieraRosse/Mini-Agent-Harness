"""Offline checks for standalone installer configuration."""

import importlib.util
import hashlib
import io
import json
import os
import shutil
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
        shell = self.installer.shutil.which("sh") or "/bin/sh"
        self.assertTrue(launcher.read_text(encoding="utf-8").startswith("#!" + shell + "\n"))

    def test_termux_launcher_shell(self):
        shell = "/data/data/com.termux/files/usr/bin/sh"
        with patch.object(self.installer, "WINDOWS", False), \
                patch.object(self.installer.shutil, "which", return_value=shell):
            text = self.installer.launcher_text(self.root)
        self.assertTrue(text.startswith("#!" + shell + "\n"))
        self.assertIn(' "$@"', text)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "Requires POSIX shell")
    def test_termux_bootstrap_upgrades_before_curl_and_stops_on_failure(self):
        commands = self.root / "commands"
        commands.mkdir()
        log = self.root / "calls"
        helper = (
            "#!" + sys.executable + "\n"
            "import os, pathlib, sys\n"
            "name = pathlib.Path(sys.argv[0]).name\n"
            "if os.environ.get('DEBIAN_FRONTEND') != 'noninteractive': sys.exit(71)\n"
            "with open(os.environ['CALL_LOG'], 'a') as f:\n"
            "    f.write(name + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "if name == 'dpkg':\n"
            "    if '--force-confold' not in sys.argv: sys.exit(72)\n"
            "    sys.exit(int(os.environ['CONFIGURE_STATUS']))\n"
            "if name == 'apt' and sys.argv[1] in ('full-upgrade', 'install'):\n"
            "    if 'Dpkg::Options::=--force-confold' not in sys.argv: sys.exit(73)\n"
            "if name == 'apt' and sys.argv[1] == 'full-upgrade':\n"
            "    sys.exit(int(os.environ['UPGRADE_STATUS']))\n"
            "if name == 'curl':\n"
            "    pathlib.Path(sys.argv[-1]).write_text('exit 0\\n')\n"
        )
        for name in ("pkg", "dpkg", "apt", "curl"):
            command = commands / name
            command.write_text(helper, encoding="utf-8")
            command.chmod(0o755)
        for configure_status, status in ((0, 0), (0, 9), (8, 0)):
            with self.subTest(configure_status=configure_status, upgrade_status=status):
                log.write_text("", encoding="utf-8")
                env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                           PREFIX=str(self.root), CALL_LOG=str(log), UPGRADE_STATUS=str(status),
                           CONFIGURE_STATUS=str(configure_status))
                result = subprocess.run(["sh", str(ROOT / "scripts/install-termux.sh"),
                                         "https://example.test"], env=env, capture_output=True,
                                        text=True, timeout=10)
                calls = log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(result.returncode, configure_status or status, result.stderr)
                self.assertIn('速度可能较慢', result.stdout)
                self.assertNotIn('\x1b[', result.stdout)
                if configure_status or status:
                    self.assertNotIn('安装完毕', result.stdout)
                else:
                    self.assertIn('安装完毕！重启终端后，输入 miniagent 即可使用。', result.stdout)
                self.assertEqual(calls[0], "dpkg --force-confold --configure --pending")
                if configure_status:
                    self.assertEqual(len(calls), 1)
                    continue
                self.assertEqual(calls[1:3], ["apt update -y",
                                            "apt full-upgrade -y -o Dpkg::Options::=--force-confold"])
                if status:
                    self.assertEqual(len(calls), 3)
                else:
                    self.assertEqual(calls[3], "apt install -y -o Dpkg::Options::=--force-confold python curl termux-api")
                    self.assertTrue(calls[4].startswith("curl -fsSL https://example.test/install.sh -o "))

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
