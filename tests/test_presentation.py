"""CX Deck presentation tests. No real iTerm, Codex, or zmx process is used."""
import copy
import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import cx_iterm
from cx_store import Store, StateError, live_key, name as validate_name
import workbench


CTX = dict(backend='zmx', host=socket.gethostname(), runtime_dir='/tmp/cxdeck-test-zmx',
           runtime_path='/opt/homebrew/bin/zmx', runtime_version='0.8.1')


def row(index, display=None, attached=0):
    session = f'cx-agent-{index}'
    item = dict(backend='zmx', session=session, sid=session, daemon_pid=500 + index,
                created=100 + index, attached=attached, state='ALIVE', task=f'Task {index}',
                display_name=display or f'Research Project {index}', codex_pids=[700 + index],
                generation=dict(host=CTX['host'], runtime_dir=CTX['runtime_dir'], session=session,
                                daemon_pid=500 + index, created=100 + index))
    item['_key'] = live_key(item, CTX)
    return item


class FakeGUI:
    def __init__(self, rows):
        self.rows = rows
        self.views = []
        self.clients = {}
        self.names = []
        self.configured = []
        self.open_calls = 0

    def configure(self, enabled):
        self.configured.append(enabled)

    def preflight(self, *args):
        return None

    def inventory(self):
        return list(self.views)

    def open(self, commands, *args, names=None):
        self.open_calls += 1
        self.names.extend(names)
        made = []
        for item, display in zip(self.rows, names):
            view = dict(guid=f'guid-{item["sid"]}', tty=f'/dev/ttys{len(self.views) + 10}')
            self.views.append(view)
            self.clients[item['sid']] = {view['tty']}
            made.append(view)
        return made

    def present(self, view, display):
        self.names.append(display)

    def focus(self, view):
        return None


class PresentationProfileTests(unittest.TestCase):
    def test_profile_is_scoped_and_uses_native_badge_and_timestamps(self):
        enabled = cx_iterm.profile_payload(True)['Profiles'][0]
        disabled = cx_iterm.profile_payload(False)['Profiles'][0]
        self.assertEqual(enabled['Name'], 'CX Deck')
        self.assertEqual(enabled['Badge Text'], r'\(user.cxdeck_name)')
        self.assertIs(enabled['Timestamps Visible'], True)
        self.assertEqual(enabled['Timestamps Style'], 1)
        self.assertIs(disabled['Timestamps Visible'], False)
        self.assertNotIn('Default Profile', json.dumps(enabled))
        for forbidden in ('TERM', 'Mouse', 'Scrollback', 'Clipboard', 'Key Mapping'):
            self.assertNotIn(forbidden, enabled)

    def test_profile_write_is_atomic_idempotent_and_leaves_unrelated_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unrelated = home / 'Library/Application Support/iTerm2/DynamicProfiles/Personal.json'
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text('{"Profiles": [{"Name": "Personal"}]}')
            path, changed = cx_iterm.ensure_profile(True, home)
            self.assertTrue(changed)
            self.assertEqual(cx_iterm.ensure_profile(True, home), (path, False))
            self.assertEqual(unrelated.read_text(), '{"Profiles": [{"Name": "Personal"}]}')
            self.assertEqual(json.loads(path.read_text()), cx_iterm.profile_payload(True))
            cx_iterm.ensure_profile(False, home)
            self.assertFalse(json.loads(path.read_text())['Profiles'][0]['Timestamps Visible'])

    def test_existing_unowned_profile_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = cx_iterm._profile_path(temporary)
            path.parent.mkdir(parents=True)
            path.write_text('{"Profiles": [{"Name": "CX Deck", "Guid": "other"}]}')
            before = path.read_text()
            with self.assertRaises(StateError):
                cx_iterm.ensure_profile(True, temporary)
            self.assertEqual(path.read_text(), before)

    def test_profile_repairs_permissions_and_removal_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile, _ = cx_iterm.ensure_profile(True, home)
            profile.chmod(0o644)
            path, changed = cx_iterm.ensure_profile(True, home)
            self.assertTrue(changed)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = home / 'outside'
            outside.mkdir()
            (home / 'Library').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(StateError, 'symlink'):
                cx_iterm.validate_profile_removal(home)

    def test_current_iterm_view_uses_supported_profile_and_variable_controls(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            output = Terminal()
            changed = cx_iterm.prepare_current_view(
                'Visual Encoding Study', False, stream=output,
                environ={'TERM_PROGRAM': 'iTerm.app'}, platform='darwin', home=temporary)
            self.assertTrue(changed)
            controls = output.getvalue()
            encoded = base64.b64encode(b'Visual Encoding Study').decode('ascii')
            self.assertIn('\x1b]1337;SetProfile=CX Deck\x1b\\', controls)
            self.assertIn('SetUserVar=cxdeck_name=' + encoded, controls)
            self.assertNotIn('\n', controls)
            profile = json.loads(cx_iterm._profile_path(temporary).read_text())
            self.assertFalse(profile['Profiles'][0]['Timestamps Visible'])

    def test_current_view_controls_are_skipped_outside_iterm(self):
        output = io.StringIO()
        self.assertFalse(cx_iterm.prepare_current_view(
            'Research Project', stream=output, environ={}, platform='darwin'))
        self.assertEqual(output.getvalue(), '')


class PresentationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'workbench')
        self.rows = [row(i, display=name) for i, name in enumerate((
            'Visual Encoding Study', 'Model Evaluation', 'Family Archive Demo',
            'Research Project', 'Posterior Analysis'), 1)]
        self.backend = Mock()
        self.backend.snapshot.side_effect = lambda: dict(context=CTX, sessions=copy.deepcopy(self.rows))
        self.backend.normalize_generation.side_effect = lambda item: (
            item['generation']['host'], item['generation']['runtime_dir'], item['session'],
            item['daemon_pid'], item['created'])

    def test_five_panes_receive_exact_full_names_without_cross_assignment(self):
        gui = FakeGUI(self.rows)
        with patch.object(cx_iterm, 'client_map', side_effect=lambda backend, *args: gui.clients):
            result = cx_iterm.show(self.rows, self.backend, self.store, gui=gui)
        self.assertEqual(result, dict(opened=5, reused=0))
        self.assertEqual(gui.open_calls, 1)
        self.assertEqual(gui.names, [item['display_name'] for item in self.rows])
        self.assertEqual(gui.configured, [True])

    def test_view_reuse_uses_one_nonbinding_runtime_and_client_snapshot(self):
        target = row(1, attached=1)
        view = {'guid': 'guid-current', 'tty': '/dev/ttys1'}
        gui = FakeGUI([target])
        gui.views = [view]
        class Backend:
            def __init__(inner):
                inner.process_calls = 0
                inner.raw_calls = 0
                inner.client_calls = 0
                inner.snapshot_calls = 0
            def processes(inner):
                inner.process_calls += 1
                return {999: {'stat': 'S'}}
            def raw_snapshot(inner, **kwargs):
                inner.raw_calls += 1
                self.assertEqual(kwargs, {'bind_threads': False,
                                          'process_table': {999: {'stat': 'S'}},
                                          'include_diagnostics': False})
                return {'context': CTX, 'sessions': [copy.deepcopy(target)]}
            def client_tty_map(inner, rows, processes):
                inner.client_calls += 1
                return {target['sid']: {view['tty']}}
            def snapshot(inner):
                inner.snapshot_calls += 1
                raise AssertionError('binding snapshot must not be used by presentation')
        backend = Backend()
        result = cx_iterm.show([target], backend, self.store, gui=gui)
        self.assertEqual(result, {'opened': 0, 'reused': 1})
        self.assertEqual((backend.process_calls, backend.raw_calls,
                          backend.client_calls, backend.snapshot_calls), (1, 1, 1, 0))

    def test_disabled_timestamp_preference_is_used_by_view_rebuild(self):
        self.store.set_preference('timestamps', False)
        gui = FakeGUI(self.rows)
        with patch.object(cx_iterm, 'client_map', side_effect=lambda backend, *args: gui.clients):
            cx_iterm.show(self.rows, self.backend, self.store, gui=gui)
        self.assertEqual(gui.configured, [False])

    def test_full_name_is_independent_of_terminal_width(self):
        full = 'Visual Computation Convergence Across Narrow Panes'
        profile = cx_iterm.profile_payload(True)['Profiles'][0]
        for columns in (160, 100, 80, 60):
            with self.subTest(columns=columns):
                self.assertEqual(profile['Badge Text'], r'\(user.cxdeck_name)')
                self.assertEqual(validate_name(full), full)

    def test_codex_title_is_used_when_no_custom_name_exists(self):
        fallback = row(9)
        fallback.pop('display_name')
        gui = FakeGUI([fallback])
        snapshot = dict(context=CTX, sessions=[copy.deepcopy(fallback)])
        with patch.object(self.backend, 'snapshot', return_value=snapshot), \
             patch.object(cx_iterm, 'client_map', side_effect=lambda backend, *args: gui.clients):
            cx_iterm.show([fallback], self.backend, self.store, gui=gui)
        self.assertEqual(gui.names, ['Task 9'])

    def test_refresh_changes_only_verified_presentation_and_never_opens(self):
        gui = FakeGUI(self.rows)
        for item in self.rows:
            item['attached'] = 1
            view = dict(guid=f'guid-{item["sid"]}', tty=f'/dev/ttys{len(gui.views) + 10}')
            gui.views.append(view)
            gui.clients[item['sid']] = {view['tty']}
        with patch.object(cx_iterm, 'client_map', return_value=gui.clients):
            result = cx_iterm.refresh(self.rows, self.backend, self.store, gui=gui)
        self.assertEqual(result, dict(refreshed=5, missing=0))
        self.assertEqual(gui.open_calls, 0)
        self.assertEqual(gui.names, [item['display_name'] for item in self.rows])

    def test_timestamp_configuration_failure_keeps_durable_preference(self):
        backend = Mock(store=self.store)
        with patch.object(workbench.sys, 'platform', 'darwin'), \
             patch.object(cx_iterm, 'ensure_profile', side_effect=StateError('GUI config failed')):
            with self.assertRaisesRegex(StateError, 'preference was saved'):
                workbench.config_command(['timestamps', 'off'], backend)
        self.assertIs(self.store.preference('timestamps', True), False)
        backend.assert_not_called()

    def test_timestamp_preference_on_headless_host_does_not_create_iterm_files(self):
        backend = Mock(store=self.store)
        with patch.object(workbench.sys, 'platform', 'linux'), \
             patch.object(cx_iterm, 'ensure_profile') as configure:
            workbench.config_command(['timestamps', 'off'], backend)
        self.assertFalse(self.store.preference('timestamps', True))
        configure.assert_not_called()

    def test_rename_gui_failure_keeps_store_and_runtime_identity(self):
        current = self.rows[0]
        current['attached'] = 1
        self.backend.store = self.store
        before = (copy.deepcopy(current['generation']), list(current['codex_pids']))
        with patch.object(workbench, 'resolve', return_value=current), \
             patch.object(workbench, 'catalog', return_value=([], [], [], False)), \
             patch.object(cx_iterm, 'refresh', side_effect=StateError('iTerm unavailable')):
            with self.assertRaisesRegex(StateError, 'Display name was saved'):
                workbench.annotate(current['session'], self.backend, title='Durable Name')
        self.assertEqual(self.store.read()['agents'][current['_key']]['name'], 'Durable Name')
        self.assertEqual((current['generation'], current['codex_pids']), before)


if __name__ == '__main__':
    unittest.main()
