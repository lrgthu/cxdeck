"""Version compatibility tests; no runtime process is changed."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cx_upgrade as upgrade


class Backend:
    VERSION = "0.8.0"
    MIN_VERSION = (0, 8, 1)
    Error = RuntimeError

    def __init__(self, versions=("0.8.0",), error=None):
        self.versions = versions
        self.error = error
        self.calls = []

    def clean(self, value):
        return str(value)

    def version(self):
        self.calls.append("version")
        if self.error:
            raise self.Error(self.error)
        return {"version": "0.8.1", "version_tuple": (0, 8, 1)}

    def snapshot(self, *, bind_threads=True):
        self.calls.append("snapshot:" + str(bind_threads))
        return {"sessions": [
            {"session": f"cx-agent-{index}", "state": "ALIVE",
             "labels": {"cx_version": version}}
            for index, version in enumerate(self.versions)
        ]}


class UpgradeTests(unittest.TestCase):
    def test_semantic_versions_use_numeric_not_lexicographic_order(self):
        self.assertGreater(upgrade.parse_version("0.10.0"), upgrade.parse_version("0.9.9"))
        self.assertLess(upgrade.parse_version("1.0.0-rc.1"), upgrade.parse_version("1.0.0"))
        with self.assertRaises(ValueError):
            upgrade.parse_version("0.6")

    def test_semantic_prerelease_order_and_build_metadata(self):
        ordered = ("1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
                   "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                   "1.0.0-rc.1", "1.0.0")
        parsed = [upgrade.parse_version(value) for value in ordered]
        self.assertEqual(parsed, sorted(parsed))
        self.assertLess(upgrade.parse_version("1.0.0-1"),
                        upgrade.parse_version("1.0.0-alpha"))
        self.assertEqual(upgrade.parse_version("1.2.3+build.1"),
                         upgrade.parse_version("1.2.3+other"))

    def test_malformed_numeric_prerelease_fails_closed(self):
        for value in ("1.0.0-01", "1.0.0-alpha.00", "01.0.0", "1.0.0-"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                upgrade.parse_version(value)
            self.assertEqual(upgrade.runtime_compatibility(value, "1.0.0"),
                             "INCOMPATIBLE")

    def test_same_version_is_current(self):
        self.assertEqual(upgrade.runtime_compatibility("0.8.0", "0.8.0"), "CURRENT")

    def test_older_compatible_runtime_has_upgrade_available(self):
        for launched in ("0.6.0", "0.7.0"):
            with self.subTest(launched=launched):
                self.assertEqual(upgrade.runtime_compatibility(launched, "0.8.0"),
                                 "UPGRADE_AVAILABLE")

    def test_explicit_runtime_boundary_requires_upgrade(self):
        rules = (("0.8.0", "0.7.0"),)
        self.assertEqual(upgrade.runtime_compatibility("0.6.0", "0.8.0", rules),
                         "UPGRADE_REQUIRED")
        self.assertEqual(upgrade.runtime_compatibility("0.7.0", "0.8.0", rules),
                         "UPGRADE_AVAILABLE")

    def test_unsupported_or_future_runtime_is_incompatible(self):
        for session, installed in (("0.5.9", "0.6.0"), ("0.7.0", "0.6.0"),
                                   ("UNKNOWN", "0.6.0")):
            with self.subTest(session=session, installed=installed):
                self.assertEqual(upgrade.runtime_compatibility(session, installed),
                                 "INCOMPATIBLE")

    def test_v060_and_v070_labels_remain_compatible_without_regeneration(self):
        data = upgrade.status_data(Backend(("0.6.0", "0.7.0")))
        self.assertEqual(data["status"], "AVAILABLE")
        self.assertEqual(data["counts"], {"CURRENT": 0, "UPGRADE_AVAILABLE": 2,
                         "UPGRADE_REQUIRED": 0, "INCOMPATIBLE": 0})
        self.assertTrue(all(row["upgrade_state"] == "UPGRADE_AVAILABLE"
                            for row in data["sessions"]))

    def test_upgrade_status_json_is_stable_and_read_only(self):
        backend = Backend(("0.6.0", "0.6.0"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(upgrade.main(["status", "--json"], backend), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["installed_cx_version"], "0.8.0")
        self.assertEqual(data["zmx"], {"installed_version": "0.8.1",
                         "minimum_version": "0.8.1", "compatible": True,
                         "error": None})
        self.assertEqual(data["live_sessions"], 2)
        self.assertEqual(backend.calls, ["version", "snapshot:False"])

    def test_upgrade_is_declarative_when_every_session_is_current(self):
        backend = Backend()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(upgrade.main([], backend), 0)
        self.assertIn("All live sessions are current.", output.getvalue())
        self.assertEqual(backend.calls, ["version", "snapshot:False"])

    def test_upgrade_available_reports_compatibility_without_changes(self):
        backend = Backend(("0.6.0",))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(upgrade.main([], backend), 0)
        self.assertIn("No restart is required", output.getvalue())
        self.assertEqual(backend.calls, ["version", "snapshot:False"])

    def test_incompatible_runtime_fails_closed(self):
        backend = Backend(("UNKNOWN",))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(upgrade.main([], backend), 1)
        self.assertIn("No sessions were changed", output.getvalue())
        self.assertEqual(backend.calls, ["version", "snapshot:False"])

    def test_incompatible_zmx_fails_closed_without_runtime_actions(self):
        backend = Backend(error="zmx too old")
        data = upgrade.status_data(backend)
        self.assertEqual(data["status"], "INCOMPATIBLE")
        self.assertFalse(data["zmx"]["compatible"])
        self.assertEqual(backend.calls, ["version"])

    def test_old_zmx_reports_actual_version_and_does_not_list_sessions(self):
        backend = Backend()
        backend.MIN_VERSION = (0, 8, 2)
        data = upgrade.status_data(backend)
        self.assertEqual(data["zmx"]["installed_version"], "0.8.1")
        self.assertEqual(data["zmx"]["minimum_version"], "0.8.2")
        self.assertFalse(data["zmx"]["compatible"])
        self.assertEqual(backend.calls, ["version"])


if __name__ == "__main__":
    unittest.main()
