"""Regression tests for the dependency-free public-source privacy gate."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import public_release_audit as audit


class PublicReleaseAuditTests(unittest.TestCase):
    def test_current_public_tree_passes(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit.scan(root, audit.tracked_files(root)), [])

    def test_sensitive_markers_are_detected_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "fixture.txt"
            path.write_text("\n".join((
                "/" + "Users" + "/alice/project",
                "ghp_" + "a" * 30,
                "-----BEGIN " + "PRIVATE KEY-----",
                "https://github.com/example/cxdeck-" + "private-archive",
            )))
            categories = {row[0] for row in audit.scan(root, [path])}
            self.assertEqual(categories,
                             {"PERSONAL_USER_PATH", "GITHUB_TOKEN",
                              "PRIVATE_KEY", "PRIVATE_ARCHIVE"})

    def test_synthetic_paths_and_identifiers_are_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "fixture.txt"
            path.write_text("/home/test/.codex\n/Users/example/project\n"
                            "11174064+project@users.noreply.github.com\n"
                            "00000000-0000-4000-8000-000000000001\n")
            self.assertEqual(audit.scan(root, [path]), [])

    def test_scan_does_not_resolve_network_fqdn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "fixture.txt"
            path.write_text("ordinary public source\n")
            with mock.patch.object(audit.socket, "getfqdn",
                                   side_effect=AssertionError("network lookup")):
                self.assertEqual(audit.scan(root, [path]), [])


if __name__ == "__main__":
    unittest.main()
