"""Exercise the distribution allowlist over a real local HTTP socket."""

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.distribute import create_server


class DistributionServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.releases = self.root / "releases"
        self.scripts = self.root / "scripts"
        self.releases.mkdir()
        self.scripts.mkdir()
        self.payload = b"test release archive\x00\xff"
        self.filename = "miniagent-0.3.0.tar.gz"
        (self.releases / self.filename).write_bytes(self.payload)
        self.manifest = {
            "version": "0.3.0",
            "python_min": "3.10",
            "filename": self.filename,
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "size": len(self.payload),
        }
        self.write_manifest()
        (self.scripts / "install.sh").write_bytes(b"#!/bin/sh\n")
        (self.scripts / "install.py").write_bytes(b"print('install')\n")
        self.server = create_server(self.releases, port=0, scripts_directory=self.scripts)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, thread)

    def stop_server(self, thread):
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=2)

    def write_manifest(self):
        (self.releases / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_get_and_head_published_files(self):
        expected = {
            "/install.sh": b"#!/bin/sh\n",
            "/install.py": b"print('install')\n",
            "/" + self.filename: self.payload,
        }
        for path, payload in expected.items():
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(200, status)
                self.assertEqual(payload, body)
                self.assertEqual(str(len(payload)), headers["Content-Length"])
                self.assertEqual("nosniff", headers["X-Content-Type-Options"])
                status, headers, body = self.request(path, "HEAD")
                self.assertEqual(200, status)
                self.assertEqual(b"", body)
                self.assertEqual(str(len(payload)), headers["Content-Length"])
        status, headers, body = self.request("/manifest.json")
        self.assertEqual(200, status)
        self.assertEqual(self.manifest, json.loads(body))
        status, headers, body = self.request("/manifest.json", "HEAD")
        self.assertEqual(200, status)
        self.assertEqual(b"", body)

    def test_root_is_static_help_not_a_directory_listing(self):
        (self.releases / "private.txt").write_text("not published", encoding="utf-8")
        status, headers, body = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b"sh -s -- http://HOST:8765", body)
        self.assertNotIn(b"private.txt", body)

    def test_secret_other_archives_and_traversal_are_not_served(self):
        secret = b"private-api-key-sentinel"
        (self.releases / ".deepseek_api_key").write_bytes(secret)
        (self.root / ".deepseek_api_key").write_bytes(secret)
        (self.releases / "miniagent-old.tar.gz").write_bytes(secret)
        paths = (
            "/.deepseek_api_key", "/../.deepseek_api_key", "/%2e%2e/.deepseek_api_key",
            "/%2e%2e%2f.deepseek_api_key", "/..%5c.deepseek_api_key", "/miniagent-old.tar.gz",
            "/scripts/", "/scripts/distribute.py", "/install.sh/", "/manifest.json/../.deepseek_api_key",
        )
        for path in paths:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(404, status)
                self.assertNotIn(secret, body)

    def test_invalid_manifest_never_publishes_an_arbitrary_filename(self):
        secret = b"private-api-key-sentinel"
        (self.releases / ".deepseek_api_key").write_bytes(secret)
        self.manifest.update(filename=".deepseek_api_key", size=len(secret))
        self.write_manifest()
        for path in ("/manifest.json", "/.deepseek_api_key", "/" + self.filename):
            status, headers, body = self.request(path)
            self.assertEqual(404, status)
            self.assertNotIn(secret, body)

    def test_unexpected_manifest_fields_are_not_published(self):
        self.manifest["private"] = "private-api-key-sentinel"
        self.write_manifest()
        status, headers, body = self.request("/manifest.json")
        self.assertEqual(200, status)
        self.assertNotIn(b"private", body)

    def test_size_mismatch_disables_the_release(self):
        self.manifest["size"] += 1
        self.write_manifest()
        self.assertEqual(404, self.request("/manifest.json")[0])
        self.assertEqual(404, self.request("/" + self.filename)[0])
        self.assertEqual(200, self.request("/install.sh")[0])

    def test_symlinked_published_files_are_refused(self):
        # Also exercise the explicit check on Windows hosts lacking symlink privileges.
        with patch.object(Path, "is_symlink", return_value=True):
            self.assertEqual(404, self.request("/install.sh")[0])
            self.assertEqual(404, self.request("/manifest.json")[0])
            self.assertEqual(404, self.request("/" + self.filename)[0])

    def test_real_symlink_cannot_escape_public_directory(self):
        secret = self.root / "secret.txt"
        secret.write_bytes(self.payload)
        archive = self.releases / self.filename
        archive.unlink()
        try:
            archive.symlink_to(secret)
        except OSError:
            self.skipTest("symlink permission unavailable")
        self.assertEqual(404, self.request("/" + self.filename)[0])
        self.assertEqual(404, self.request("/manifest.json")[0])


if __name__ == "__main__":
    unittest.main()
