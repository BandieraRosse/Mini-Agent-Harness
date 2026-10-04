"""Offline checks for standalone installer configuration."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
                patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            line = self.installer.add_to_path(self.bin_dir)
            self.installer.add_to_path(self.bin_dir)
        self.assertTrue(profile.read_text(encoding="utf-8").startswith("# Existing profile\nexport EDITOR=vi\n"))
        self.assertTrue(rc.read_text(encoding="utf-8").startswith("# Existing bash settings\n"))
        for path in (profile, rc):
            self.assertEqual(path.read_text(encoding="utf-8").splitlines().count(line), 1)
