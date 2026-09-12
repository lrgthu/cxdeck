"""State-directory upgrade tests. Only temporary directories are changed."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cx_paths
from cx_store import Store


class StatePathUpgradeTests(unittest.TestCase):
    def test_current_empty_home_is_not_mutated_by_upgrade_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(cx_paths.upgrade_state_path(home), 'CURRENT')
            self.assertFalse(cx_paths.state_home(home).exists())

    def test_store_read_never_runs_the_install_time_path_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            old = home / cx_paths.LEGACY_STATE_RELATIVE / 'workbench'
            old.mkdir(parents=True)
            (old / 'state.json').write_text(json.dumps({
                'version': 1, 'agents': {}, 'views': {}, 'workspaces': {},
                'groups': {}, 'config': {}}))
            with patch('pathlib.Path.home', return_value=home):
                self.assertEqual(Store().read()['workspaces'], {})
            self.assertTrue(old.exists())
            self.assertFalse((cx_paths.state_home(home) / 'workbench').exists())

    def test_v060_workbench_moves_atomically_and_historical_files_stay_inert(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            old = home / cx_paths.LEGACY_STATE_RELATIVE
            workbench = old / 'workbench'
            workbench.mkdir(parents=True)
            payload = {"version": 1, "agents": {"agent": {"name": "Model Evaluation", "pinned": True}},
                       "views": {"view": {"guid": "synthetic", "tty": "/dev/ttys999"}},
                       "workspaces": {"daily": {"members": []}}, "groups": {"g": {"name": "Studies"}}}
            (workbench / 'state.json').write_text(json.dumps(payload))
            journal = old / 'historical-journal.json'
            journal.write_text('inert')
            self.assertEqual(cx_paths.upgrade_state_path(home), 'UPGRADED')
            new = cx_paths.state_home(home) / 'workbench/state.json'
            self.assertEqual(json.loads(new.read_text()), payload)
            self.assertFalse(workbench.exists())
            self.assertEqual(journal.read_text(), 'inert')
            self.assertEqual(cx_paths.upgrade_state_path(home), 'CURRENT')

    def test_conflicting_old_and_new_state_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            old = home / cx_paths.LEGACY_STATE_RELATIVE / 'workbench'
            new = cx_paths.state_home(home) / 'workbench'
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (old / 'state.json').write_text('old')
            (new / 'state.json').write_text('new')
            with self.assertRaises(cx_paths.PathUpgradeError):
                cx_paths.upgrade_state_path(home)
            self.assertEqual((old / 'state.json').read_text(), 'old')
            self.assertEqual((new / 'state.json').read_text(), 'new')

    def test_symlinked_state_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / 'target'
            target.mkdir()
            path = home / '.local/state'
            path.parent.mkdir()
            path.symlink_to(target)
            with self.assertRaises(cx_paths.PathUpgradeError):
                cx_paths.upgrade_state_path(home)


if __name__ == '__main__':
    unittest.main()
