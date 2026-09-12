"""zmx-native workbench unit tests. Fake terminal boundaries; no user data."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cx_store as st
import cx_iterm as ui
import workbench as w
import console_entry as entry

ID = '019a0000-1111-7222-8333-444444444444'
HOME = '/test/codex'
CTX = dict(backend='zmx', host=socket.gethostname(), runtime_dir='/tmp/cx-zmx-test',
           runtime_path='/bin/zmx', runtime_version='0.8.1')


def record(index=1, **extra):
    session = f'cx-agent-{index}'
    row = dict(backend='zmx', runtime_version='0.8.1', session=session, sid=session,
        daemon_pid=200 + index, created=index,
        codex_home=HOME, thread_id='', task=f'agent-{index}', launch_cwd='/', root='',
        codex_pids=[100 + index], state='ALIVE', attached=0, cx_version='0.6.0',
        labels={'cx_version': '0.6.0'})
    row.update(extra)
    row.setdefault('generation', dict(host=CTX['host'], runtime_dir=CTX['runtime_dir'],
        session=row['session'], daemon_pid=row['daemon_pid'], created=row['created']))
    row['_key'] = st.live_key(row, CTX)
    row['_thread_key'] = st.thread_key(row['codex_home'], row['thread_id'], CTX['host'])
    row['display_name'] = row['task']
    row['pinned'] = False
    return row


def conversation(row=None, tid=ID, key=None, **extra):
    item = dict(key=key or tid, thread_id=tid, title='Research', cwd='/', home=HOME,
                updated=1, managed=row, external_pids=[], state=row['state'] if row else 'SAVED')
    item.update(extra)
    return item


def options(**extra):
    values = dict(no_iterm=True, no_dashboard=True, allow_unverified_live=False, cwd=None,
                  per_tab=0, min_columns=70, min_rows=12, yolo=True)
    values.update(extra)
    return types.SimpleNamespace(**values)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'workbench'
        self.store = st.Store(self.root)

    def test_missing_read_has_no_write(self):
        self.assertEqual(self.store.read()['workspaces'], {})
        self.assertFalse(self.root.exists())

    def test_atomic_updates_backup_and_permissions(self):
        self.store.annotate(['key'], name='审阅')
        self.store.annotate(['key'], pinned=True)
        state = self.store.read()
        self.assertEqual(state['agents']['key'], dict(name='审阅', pinned=True))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        old = json.loads((self.root / 'state.previous.json').read_text())
        self.assertNotIn('pinned', old['agents']['key'])

    def test_workspace_overwrite_requires_explicit_replace(self):
        self.store.workspace('daily', dict(members=[]))
        with self.assertRaises(st.StateError):
            self.store.workspace('daily', dict(members=[1]))
        self.store.workspace('daily', dict(members=[1]), replace=True)
        self.assertEqual(self.store.read()['workspaces']['daily']['members'], [1])

    def test_lock_excludes_other_launcher(self):
        with self.store.lock('views'):
            with self.assertRaises(st.StateError):
                with self.store.lock('views'):
                    self.fail('lock was not exclusive')

    def test_corrupt_state_is_not_overwritten(self):
        self.root.mkdir()
        self.store.path.write_text('{broken')
        with self.assertRaises(st.StateError):
            self.store.annotate(['x'], pinned=True)
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_symlink_file_refused(self):
        self.root.mkdir()
        target = self.root / 'target'
        target.write_text('{}')
        self.store.path.symlink_to(target)
        with self.assertRaises((st.StateError, OSError)):
            self.store.read()
        self.assertEqual(target.read_text(), '{}')

    def test_symlink_directory_refused(self):
        dest = self.root.parent / 'real'
        dest.mkdir()
        self.root.symlink_to(dest)
        with self.assertRaises(st.StateError):
            self.store.annotate(['x'], pinned=True)

    def test_unknown_schema_refused(self):
        self.root.mkdir()
        self.store.path.write_text('{"version": 99}')
        with self.assertRaises(st.StateError):
            self.store.read()

    def test_terminal_controls_rejected_in_names(self):
        for value in ('', '\x1b[2J', 'a\nb', 'x' * 129):
            with self.subTest(value=value), self.assertRaises(st.StateError):
                st.name(value)
        self.assertEqual(st.name('审阅 $(not-executed)'), '审阅 $(not-executed)')

    def test_identity_is_not_label_or_repo(self):
        row = record()
        self.assertEqual(st.live_key(row, CTX), st.live_key(dict(row, task='new', root='/other'), CTX))
        moved = copy.deepcopy(row)
        moved['generation']['runtime_dir'] = '/tmp/other-zmx'
        self.assertNotEqual(st.live_key(row, CTX), st.live_key(moved, dict(CTX, runtime_dir='/tmp/other-zmx')))
        recycled = copy.deepcopy(row)
        recycled['created'] = recycled['generation']['created'] = 99
        self.assertNotEqual(st.live_key(row, CTX), st.live_key(recycled, CTX))

    def test_live_key_requires_explicit_zmx_context_and_row(self):
        row = record()
        with self.assertRaises(st.StateError):
            st.live_key(dict(row, backend='other'), CTX)
        with self.assertRaises(st.StateError):
            st.live_key(row, dict(CTX, backend='other'))

    def test_thread_identity_is_namespaced(self):
        a = st.thread_key(HOME, ID, 'mac')
        self.assertNotEqual(a, st.thread_key('/other', ID, 'mac'))
        self.assertNotEqual(a, st.thread_key(HOME, ID, 'dell'))


class FakeGUI:
    def __init__(self, clients, views=()):
        self.views = list(views)
        self.clients = clients
        self.calls = []
        self.focused = []
        self.targets = []
        self.presented = {}
        self.configured = []

    def configure(self, timestamps):
        self.configured.append(timestamps)

    def preflight(self, *args):
        pass

    def inventory(self):
        return list(self.views)

    def focus(self, view):
        self.focused.append(view)

    def present(self, view, display_name):
        self.presented[view['guid']] = display_name

    def open(self, commands, *args, names=None):
        self.calls.append((commands, args, names))
        created = []
        for sid in self.targets:
            v = dict(guid='g' + sid, tty='/dev/tty' + sid.rsplit('-', 1)[-1])
            self.clients.setdefault(sid, set()).add(v['tty'])
            self.views.append(v)
            created.append(v)
        return created


class WorkbenchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = st.Store(Path(self.tmp.name).resolve() / 'workbench')
        self.b = Mock()
        self.b.store = self.store
        self.rows = [record()]
        self.b.snapshot.side_effect = lambda: dict(sessions=copy.deepcopy(self.rows), context=CTX, warnings=[])
        self.b.clean.side_effect = str


class ViewTests(WorkbenchFixture):
    def show(self, gui, rows=None, **kwargs):
        with patch.object(ui, 'client_map', side_effect=lambda b: gui.clients), \
             patch.object(ui.shutil, 'which', side_effect=lambda x: '/bin/' + x):
            return ui.show(self.rows if rows is None else rows, self.b, self.store, gui=gui, **kwargs)

    def test_verified_existing_tty_is_focused_not_duplicated(self):
        v = dict(guid='existing', tty='/dev/ttys1')
        gui = FakeGUI({'cx-agent-1': {'/dev/ttys1'}}, [v])
        result = self.show(gui)
        self.assertEqual(result, dict(opened=0, reused=1))
        self.assertEqual(gui.focused, [v])
        self.assertFalse(gui.calls)

    def test_repeated_open_reuses_newly_attached_view(self):
        gui = FakeGUI({})
        gui.targets = ['cx-agent-1']
        self.assertEqual(self.show(gui)['opened'], 1)
        self.assertEqual(self.show(gui)['reused'], 1)
        self.assertEqual(len(gui.calls), 1)

    def test_mixed_batch_only_opens_missing_views(self):
        self.rows = [record(i) for i in range(1, 6)]
        gui = FakeGUI({'cx-agent-1': {'/dev/tty1'}}, [dict(guid='g1', tty='/dev/tty1')])
        gui.targets = ['cx-agent-2', 'cx-agent-3', 'cx-agent-4', 'cx-agent-5']
        result = self.show(gui, per_tab=3)
        self.assertEqual(result, dict(opened=4, reused=1))
        self.assertEqual(len(gui.calls[0][0]), 4)

    def test_second_client_race_is_not_accepted_as_preferred_view(self):
        gui = FakeGUI({})
        gui.targets = ['cx-agent-1']
        original_open = gui.open

        def open_with_racing_client(*args, **kwargs):
            created = original_open(*args, **kwargs)
            gui.clients['cx-agent-1'].add('/dev/ttys-race')
            return created

        gui.open = open_with_racing_client
        with self.assertRaisesRegex(st.StateError, 'Multiple zmx clients attached'):
            self.show(gui)
        self.assertEqual(len(gui.calls), 1)

    def test_stale_cached_view_refuses_duplicate(self):
        v = dict(guid='old', tty='/dev/tty1')
        self.store.change(lambda s: s['views'].update({self.rows[0]['_key']: v}))
        gui = FakeGUI({}, [v])
        with self.assertRaisesRegex(st.StateError, 'no verified'):
            self.show(gui)
        self.assertFalse(gui.calls)

    def test_closed_view_can_be_recreated(self):
        self.store.change(lambda s: s['views'].update({self.rows[0]['_key']: dict(guid='old', tty='/dev/old')}))
        gui = FakeGUI({})
        gui.targets = ['cx-agent-1']
        self.assertEqual(self.show(gui)['opened'], 1)

    def test_generation_change_refused(self):
        old = self.rows.copy()
        self.rows = [record(created=2)]
        gui = FakeGUI({})
        with self.assertRaises(st.StateError):
            self.show(gui, old)
        self.assertFalse(gui.calls)

    def test_empty_selection_no_gui(self):
        gui = Mock()
        self.assertEqual(ui.show([], self.b, self.store, gui=gui), dict(opened=0, reused=0))
        gui.preflight.assert_not_called()

    def test_partial_gui_failure_does_not_kill_agents(self):
        gui = FakeGUI({})
        gui.open = Mock(side_effect=st.StateError('GUI failed'))
        with self.assertRaises(st.StateError):
            self.show(gui)

    def test_attachment_pins_zmx_generation_and_quotes_runtime(self):
        ctx = dict(CTX, runtime_dir="/tmp/space ' $(never)")
        row = record()
        row['generation']['runtime_dir'] = ctx['runtime_dir']
        with patch.object(ui.shutil, 'which', side_effect=lambda x: '/bin/' + x):
            argv = shlex.split(ui.attachment(row, ctx))
        self.assertEqual(argv[-4], ctx['runtime_dir'])
        self.assertEqual(argv[-5], ctx['runtime_path'])
        self.assertEqual(argv[-3:], ['cx-agent-1', '201', '1'])
        self.assertIn('attach-verified', argv)
        self.assertNotIn('send-keys', argv[2])

    def test_malformed_view_response_is_not_guessed(self):
        for text in ('garbage', 'g\twrong-device', 'g\t/dev/a\ng\t/dev/b'):
            with self.subTest(text=text), self.assertRaises(st.StateError):
                ui.parse_views(text)

    def test_identical_transient_iterm_inventory_rows_are_deduplicated(self):
        row = 'same-guid\t/dev/ttys001'
        self.assertEqual(ui.parse_views(row + '\n' + row),
                         [{'guid': 'same-guid', 'tty': '/dev/ttys001'}])

    def test_script_never_writes_text_into_an_existing_pane(self):
        for forbidden in ('write text', 'write contents', 'keystroke'):
            self.assertNotIn(forbidden, ui.APPLESCRIPT)


class ManagementTests(WorkbenchFixture):
    def test_exact_resolve_and_ambiguous_alias(self):
        self.assertEqual(w.resolve('cx-agent-1', self.b)['sid'], 'cx-agent-1')
        with self.assertRaises(st.StateError):
            w.resolve('cx-agent', self.b)
        self.rows.append(record(2, task='agent-1'))
        with self.assertRaises(st.StateError):
            w.resolve('agent-1', self.b)

    def test_rename_does_not_change_native_identity(self):
        self.rows[0]['attached'] = 1
        item = conversation(self.rows[0])
        with patch.object(w, 'catalog', return_value=([item], [], [], False)), \
             patch.object(ui, 'refresh', return_value=dict(refreshed=1, missing=0)) as refresh:
            w.annotate('cx-agent-1', self.b, title='Theory / 理论')
        self.assertEqual(self.rows[0]['task'], 'agent-1')
        self.assertEqual(self.rows[0]['codex_pids'], [101])
        self.assertEqual(self.store.read()['agents'][self.rows[0]['_key']]['name'], 'Theory / 理论')
        self.assertEqual(self.store.read()['agents'][st.thread_key(HOME, ID, CTX['host'])]['name'], 'Theory / 理论')
        self.assertEqual(refresh.call_args.args[0][0]['display_name'], 'Theory / 理论')

    def test_new_permission_failure_precedes_agent_launch(self):
        self.b.console.by_label.return_value = None
        with patch.object(ui, 'ITerm') as gui:
            gui.return_value.preflight.side_effect = st.StateError('denied')
            with self.assertRaises(st.StateError):
                w.new_agents(['--split', '--count', '3'], self.b)
        self.b.console.start_agent.assert_not_called()

    def test_invalid_new_count_launches_nothing(self):
        for args in (['--count', '0'], ['--count', '-2'], ['--count', '3', 'label']):
            with self.subTest(args=args), self.assertRaises(st.StateError):
                w.new_agents(args, self.b)
        self.b.console.start_agent.assert_not_called()

    def test_workspace_subset_and_live_only_marker(self):
        self.rows = [record(1), record(2)]
        with patch.object(w, 'catalog', return_value=([conversation(self.rows[0])], [], [], False)), \
             patch.object(w, 'native', return_value=types.SimpleNamespace(codex_home=lambda: HOME)):
            saved = w.workspace_save('daily', self.b, ['cx-agent-2'])
        self.assertEqual(len(saved['members']), 1)
        self.assertIsNone(saved['members'][0]['thread_id'])
        self.assertEqual(saved['members'][0]['cwd'], '/')
        self.assertNotIn('env', saved['members'][0])

    def test_workspace_wrong_host_is_not_restored(self):
        with self.assertRaises(st.StateError):
            w.workspace_plan(dict(host='other-host', members=[]), self.b)

    def test_missing_live_only_workspace_is_not_guessed(self):
        saved = dict(host=socket.gethostname(), members=[dict(live_key='old', home=HOME, title='Old', thread_id=None)])
        with patch.object(w, 'native', return_value=types.SimpleNamespace(codex_home=lambda: HOME)), \
             patch.object(w, 'catalog', return_value=([], [], [], False)):
            rows, history, missing = w.workspace_plan(saved, self.b)
        self.assertEqual((rows, history), ([], []))
        self.assertIn('no cold identity', missing[0])

    def test_foreign_codex_home_never_selected_even_when_live(self):
        saved = dict(host=socket.gethostname(), members=[dict(live_key=self.rows[0]['_key'], home='/other', title='Other')])
        with patch.object(w, 'native', return_value=types.SimpleNamespace(codex_home=lambda: HOME)), \
             patch.object(w, 'catalog', return_value=([conversation(self.rows[0], tid=None, key='zmx:cx-agent-1')], [], [], False)):
            rows, _, missing = w.workspace_plan(saved, self.b)
        self.assertEqual(rows, [])
        self.assertTrue(missing)

    def test_workspace_open_propagates_cold_launch_policy(self):
        self.store.workspace('research', dict(host=socket.gethostname(), members=[],
                                             layout=dict(per_tab=0, min_columns=70, min_rows=12)))
        planned = [conversation()]
        for flags, expected in (([], True), (['--yolo'], True), (['--safe'], False),
                                (['--no-yolo'], False)):
            with self.subTest(flags=flags), patch.object(w, 'workspace_plan', return_value=(planned, [], [])), \
                 patch.object(w, 'launch_selected') as launch:
                w.workspace_command(['open', 'research', '--all', *flags], self.b)
                self.assertIs(launch.call_args.args[2].yolo, expected)


class ResumeSafetyTests(WorkbenchFixture):
    def run_case(self, chosen, fresh=None, unknown=(), failed=False, opts=None):
        r = Mock()
        r.codex_home.return_value = HOME
        r.launch_lock.return_value = contextlib.nullcontext()
        r.ensure.return_value = (self.rows[0], 'reused')
        fresh = chosen if fresh is None else fresh
        with patch.object(w, 'native', return_value=r), \
             patch.object(w, 'catalog', return_value=(fresh, unknown, [], failed)), \
             patch.object(w, 'focus_rows'), contextlib.redirect_stdout(io.StringIO()):
            try:
                w.launch_selected(chosen, [], opts or options(), self.b)
                error = None
            except st.StateError as exc:
                error = exc
        return r, error

    def test_empty_selection_starts_nothing(self):
        r, error = self.run_case([])
        self.assertIsNone(error)
        r.ensure.assert_not_called()

    def test_external_copy_blocks_even_with_override(self):
        r, error = self.run_case([conversation(external_pids=[77])], opts=options(allow_unverified_live=True))
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()

    def test_unknown_pids_block_cold_resume(self):
        r, error = self.run_case([conversation()], unknown=[77])
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()

    def test_failed_process_inspection_blocks_cold_resume(self):
        r, error = self.run_case([conversation()], failed=True)
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()

    def test_unknown_selected_state_blocks_even_with_override(self):
        r, error = self.run_case([conversation(record(state='UNKNOWN'))], opts=options(allow_unverified_live=True))
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()

    def test_live_reattach_allowed_despite_unrelated_unknown_pid(self):
        for yolo in (True, False):
            with self.subTest(yolo=yolo):
                chosen = [conversation(self.rows[0])]
                r, error = self.run_case(chosen, unknown=[999], opts=options(yolo=yolo))
                self.assertIsNone(error)
                r.ensure.assert_called_once_with(chosen[0], self.b, None, yolo=yolo)
                r.planned_cwd.assert_not_called()

    def test_cold_resume_policy_is_forwarded_to_native_launcher(self):
        for yolo in (True, False):
            with self.subTest(yolo=yolo):
                chosen = [conversation()]
                r, error = self.run_case(chosen, opts=options(yolo=yolo))
                self.assertIsNone(error)
                r.ensure.assert_called_once_with(chosen[0], self.b, None, yolo=yolo)

    def test_disappeared_selection_starts_nothing(self):
        r, error = self.run_case([conversation()], fresh=[])
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()

    def test_recycled_live_only_name_starts_nothing(self):
        old = conversation(record(), tid=None, key='zmx:cx-agent-1')
        new = conversation(record(created=99), tid=None, key=old['key'])
        r, error = self.run_case([old], fresh=[new])
        self.assertIsNotNone(error)
        r.ensure.assert_not_called()


class DashboardTests(WorkbenchFixture):
    def screen(self, keys):
        win = Mock()
        win.getmaxyx.return_value = (15, 80)
        win.get_wch.side_effect = keys
        r = types.SimpleNamespace(clip=lambda text, width: text[:width])
        with patch.object(w, 'native', return_value=r), patch.object(w.sys.stdin, 'isatty', return_value=True), \
             patch.object(w.sys.stdout, 'isatty', return_value=True), patch.object(w.curses, 'curs_set'), \
             patch.object(w.curses, 'wrapper', side_effect=lambda fn: fn(win)):
            w.dashboard(self.b)
        return win

    def test_quit_never_stops_agent(self):
        self.screen(['q'])

    def test_search_mode_q_is_text_not_quit(self):
        win = self.screen(['/', 'q', '\n', 'q'])
        self.assertEqual(win.get_wch.call_count, 4)

    def test_empty_dashboard_navigation_is_safe(self):
        self.rows = []
        self.screen(['j', 'k', ' ', '\n', 'q'])

    def test_noninteractive_dashboard_is_one_snapshot(self):
        with patch.object(w.sys.stdin, 'isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            w.dashboard(self.b)
        self.assertEqual(self.b.snapshot.call_count, 1)

    def test_json_status_has_backend_core_and_explicit_unknowns(self):
        with patch.object(w.sys.stdin, 'isatty', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            w.dashboard(self.b, as_json=True)
        row = json.loads(output.getvalue())['sessions'][0]
        self.assertEqual(row['backend'], 'zmx')
        self.assertEqual(row['runtime_version'], '0.8.1')
        self.assertEqual(row['thread_id'], 'UNKNOWN')
        self.assertEqual(row['launch_policy'], 'UNKNOWN')
        self.assertEqual(row['launch_mode'], 'UNKNOWN')
        self.assertEqual(row['cx_version'], '0.6.0')
        self.assertEqual(row['upgrade_state'], 'UPGRADE_AVAILABLE')
        self.assertEqual(row['state'], 'ALIVE')

    def test_invalid_interval_refused(self):
        for value in (0, float('nan'), float('inf')):
            with self.assertRaises(st.StateError):
                w.dashboard(self.b, value, once=True)


if __name__ == '__main__':
    unittest.main()
