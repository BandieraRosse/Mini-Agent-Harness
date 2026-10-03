import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from miniagent import __version__
from scripts import build_release as release


def wheel_bytes(name, version, extra=None, *, pure=True):
    metadata = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": b"# Pure Python test dependency.\n",
        f"{metadata}/METADATA": f"Name: {name}\nVersion: {version}\n".encode(),
        f"{metadata}/WHEEL": (f"Wheel-Version: 1.0\nRoot-Is-Purelib: {str(pure).lower()}\n"
                                "Tag: py3-none-any\n").encode(),
        f"{metadata}/licenses/LICENSE": b"Test license\n",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for path, contents in files.items():
            if isinstance(path, str):
                info = zipfile.ZipInfo(path)
                info.filename = path  # ZipInfo otherwise normalizes backslashes on Windows.
            else:
                info = path
            wheel.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        cache = patch.object(release, "WHEEL_CACHE", self.root / "cache")
        cache.start()
        self.addCleanup(cache.stop)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        for name, version in release.DEPENDENCIES.items():
            (self.wheels / f"{name}-{version}-py3-none-any.whl").write_bytes(wheel_bytes(name, version))

    def test_release_allowlist_determinism_and_python_without_site_packages(self):
        source = self.root / "source"
        (source / "miniagent").mkdir(parents=True)
        for relative in release._source_files():
            shutil.copyfile(release.ROOT / relative, source / relative)
        for relative in (".deepseek_api_key", ".git/config", "tests/leak.py", ".venv/leak.py",
                         "miniagent/private.json", "miniagent/__pycache__/leak.pyc"):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"DO_NOT_DISTRIBUTE")
        output = self.root / "releases"
        with patch.object(release, "ROOT", source):
            manifest = release.build_release(output, self.wheels)
            self.assertEqual(manifest, release.build_release(output, self.wheels))
        self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(manifest["python_min"], "3.10")
        data = (output / manifest["filename"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["sha256"])
        self.assertEqual(len(data), manifest["size"])
        prefix = f"miniagent-{__version__}/"
        unpacked = self.root / "unpacked"
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            names = archive.getnames()
            for name in ("agent.py", "miniagent/instructions.md", "prompt_toolkit/__init__.py",
                         "wcwidth/__init__.py", "wcwidth-0.9.1.dist-info/licenses/LICENSE"):
                self.assertIn(prefix + name, names)
            for info in archive.getmembers():
                self.assertTrue(info.isfile())
                self.assertEqual((info.uid, info.gid, info.uname, info.gname, info.mtime), (0, 0, "", "", 0))
                self.assertTrue(info.name.startswith(prefix))
                content = archive.extractfile(info).read()
                self.assertNotIn(b"DO_NOT_DISTRIBUTE", content)
                target = unpacked / info.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            self.assertFalse(any("__pycache__" in name or "/tests/" in name for name in names))
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-S", "agent.py", "--version"],
                                cwd=unpacked / prefix, env=environment, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"MiniAgent {__version__}")

    def test_rejects_unsafe_paths_native_files_and_symlinks(self):
        for path in ("../escape.py", "/absolute.py", "C:/absolute.py", "wcwidth/../../escape.py",
                     "wcwidth\\escape.py", "unrelated/data.py", "wcwidth/native.pyd", "wcwidth/start.pth",
                     "wcwidth/native.bin"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                release._wheel_files(wheel_bytes("wcwidth", "0.9.1", {path: b"bad"}), "wcwidth", "0.9.1")
        info = zipfile.ZipInfo("wcwidth/link.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            release._wheel_files(wheel_bytes("wcwidth", "0.9.1", {info: b"../../secret"}), "wcwidth", "0.9.1")

    def test_rejects_wrong_package_native_metadata_and_large_content(self):
        cases = [wheel_bytes("wcwidth", "0.9.1", pure=False),
                 wheel_bytes("wcwidth", "0.9.1", {"wcwidth-0.9.1.dist-info/METADATA": b"Name: wcwidth\nVersion: 0.0\n"}),
                 wheel_bytes("wcwidth", "0.9.1", {"wcwidth/big.py": b"x" * (release.MAX_FILE + 1)})]
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                release._wheel_files(data, "wcwidth", "0.9.1")

    def test_online_download_verifies_pypi_digest(self):
        data = wheel_bytes("wcwidth", "0.9.1")
        item = {"filename": "wcwidth-0.9.1-py3-none-any.whl", "url": "https://files.pythonhosted.org/test.whl",
                "digests": {"sha256": hashlib.sha256(data).hexdigest()}}
        with patch.object(release, "_fetch", side_effect=[json.dumps({"urls": [item]}).encode(), data]):
            self.assertEqual(release._wheel("wcwidth", "0.9.1", None), data)
        item["digests"]["sha256"] = "0" * 64
        (release.WHEEL_CACHE / item["filename"]).unlink()
        with patch.object(release, "_fetch", side_effect=[json.dumps({"urls": [item]}).encode(), data]):
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                release._wheel("wcwidth", "0.9.1", None)

    def test_cached_build_needs_no_network_and_is_identical(self):
        responses = []
        for name, version in release.DEPENDENCIES.items():
            data = wheel_bytes(name, version)
            item = {"filename": f"{name}-{version}-py3-none-any.whl",
                    "url": "https://files.pythonhosted.org/test.whl",
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()}}
            responses.extend([json.dumps({"urls": [item]}).encode(), data])
        with patch.object(release, "_fetch", side_effect=responses) as fetch:
            first = release.build_release(self.root / "first")
            self.assertEqual(fetch.call_count, 4)
        with patch.object(release, "_fetch", side_effect=AssertionError("Network must not be used")):
            self.assertEqual(first, release.build_release(self.root / "second"))

    def test_incomplete_or_damaged_cache_is_downloaded_again(self):
        name, version = "wcwidth", "0.9.1"
        data = wheel_bytes(name, version)
        filename = f"{name}-{version}-py3-none-any.whl"
        item = {"filename": filename, "url": "https://files.pythonhosted.org/test.whl",
                "digests": {"sha256": hashlib.sha256(data).hexdigest()}}
        release.WHEEL_CACHE.mkdir()
        source = release.WHEEL_CACHE / filename
        checksum = source.with_suffix(".whl.sha256")
        for damage in ("missing-checksum", "bad-checksum", "damaged-wheel"):
            with self.subTest(damage=damage):
                source.write_bytes(data if damage != "damaged-wheel" else b"incomplete download")
                checksum.unlink(missing_ok=True)
                if damage != "missing-checksum":
                    checksum.write_text("invalid" if damage == "bad-checksum" else item["digests"]["sha256"])
                with patch.object(release, "_fetch", side_effect=[json.dumps({"urls": [item]}).encode(), data]) as fetch:
                    self.assertEqual(release._wheel(name, version, None), data)
                    self.assertEqual(fetch.call_count, 2)
                with patch.object(release, "_fetch", side_effect=AssertionError("Unexpected download")):
                    self.assertEqual(release._wheel(name, version, None), data)

    def test_new_dependency_version_cannot_reuse_old_wheel(self):
        name = "wcwidth"
        for version in ("0.9.1", "0.9.2"):
            data = wheel_bytes(name, version)
            item = {"filename": f"{name}-{version}-py3-none-any.whl",
                    "url": "https://files.pythonhosted.org/test.whl",
                    "digests": {"sha256": hashlib.sha256(data).hexdigest()}}
            with patch.object(release, "_fetch", side_effect=[json.dumps({"urls": [item]}).encode(), data]) as fetch:
                self.assertEqual(release._wheel(name, version, None), data)
                self.assertEqual(fetch.call_count, 2)

    def test_nonportable_download_is_not_cached(self):
        data = wheel_bytes("wcwidth", "0.9.1", pure=False)
        item = {"filename": "wcwidth-0.9.1-py3-none-any.whl", "url": "https://files.pythonhosted.org/test.whl",
                "digests": {"sha256": hashlib.sha256(data).hexdigest()}}
        with patch.object(release, "_fetch", side_effect=[json.dumps({"urls": [item]}).encode(), data]):
            with self.assertRaises(ValueError):
                release._wheel("wcwidth", "0.9.1", None)
        self.assertFalse(release.WHEEL_CACHE.exists())

    def test_invalid_build_leaves_previous_manifest_untouched(self):
        output = self.root / "releases"
        manifest = release.build_release(output, self.wheels)
        original = (output / "manifest.json").read_bytes()
        (self.wheels / "wcwidth-0.9.1-py3-none-any.whl").write_bytes(wheel_bytes("wcwidth", "0.9.1", pure=False))
        with self.assertRaises(ValueError):
            release.build_release(output, self.wheels)
        self.assertEqual((output / "manifest.json").read_bytes(), original)
        self.assertEqual(hashlib.sha256((output / manifest["filename"]).read_bytes()).hexdigest(), manifest["sha256"])


if __name__ == "__main__":
    unittest.main()
