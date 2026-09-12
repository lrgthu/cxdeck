"""T3 command tests; all runtimes and iTerm providers are synthetic."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import console_entry
import cx_inventory
from cx_store import Store
import workbench
from tests.test_inventory import HOST, HOME, IDS, compose, history, joined


def workspace_record(indexes):
    return {'host': HOST, 'saved_at': 1, 'members': [
        {'home': HOME, 'thread_id': IDS[index - 1], 'live_key': 'historical',
         'title': f'Project {index}', 'cwd': f'/work/project-{index}'}
        for index in indexes]}


class CommandFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.backend = Mock(store=self.store)
        self.backend.clean.side_effect = str
        self.gui = Mock()
        self.base = compose([joined(1), joined(2), joined(3)],
                            saved=[history(1), history(2), history(3)])
        history_patch = patch.object(workbench, '_read_history', return_value=([], False, None))
        history_patch.start()
        self.addCleanup(history_patch.stop)


class ViewsStatusTests(CommandFixture):
    def test_inventory_observation_reuses_preloaded_history_and_reads_other_providers_once(self):
        raw = {'timestamp': 1, 'context': dict(workbench_backend='unused', **{
                   'backend': 'zmx', 'host': HOST, 'runtime_dir': '/tmp/cxdeck-t3-zmx',
                   'runtime_version': '0.8.1'}),
               'sessions': [joined(1)['managed']], 'outside_managed_codex_pids': [],
               'warnings': []}
        # Drop the harmless extra context key so live_key sees the exact normal shape.
        raw['context'].pop('workbench_backend')
        backend = Mock(store=self.store)
        backend.raw_snapshot.return_value = raw
        backend.processes.return_value = {1: {}}
        backend.client_tty_map.return_value = {'cx-t3-1': {'/dev/ttys1'}}
        native = Mock()
        native.codex_home.return_value = HOME
        native.list_history.return_value = ([history()], False)
        native.inventory.return_value = ([joined(1)], [], [], False)
        self.gui.inventory.return_value = [{'guid': 'guid-1', 'tty': '/dev/ttys1'}]
        with patch.object(workbench, 'native', return_value=native):
            result = workbench.inventory_snapshot(backend, gui=self.gui)
        self.assertEqual(result['conversations'][0]['view_state'], 'VERIFIED_VIEW')
        backend.raw_snapshot.assert_called_once_with(bind_threads=False,
                                                     process_table=backend.processes.return_value,
                                                     include_diagnostics=False)
        backend.processes.assert_called_once_with()
        backend.client_tty_map.assert_called_once()
        native.list_history.assert_not_called()
        native.inventory.assert_called_once()
        self.gui.inventory.assert_called_once_with()
        backend.snapshot.assert_not_called()

    def test_status_is_read_only_and_json_is_stable(self):
        before = self.store.read()
        with patch.object(workbench, 'inventory_snapshot', return_value=self.base), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workbench.views_command(['status', '--json'], self.backend,
                                                     gui=self.gui), 0)
        payload = __import__('json').loads(output.getvalue())
        self.assertEqual(payload['schema'], cx_inventory.SCHEMA)
        self.assertEqual(len(payload['conversations']), 3)
        self.assertFalse(any('_managed' in row for row in payload['conversations']))
        self.assertEqual(self.store.read(), before)
        self.gui.focus.assert_not_called()
        self.gui.open.assert_not_called()

    def test_workspace_status_contains_only_exact_members(self):
        self.store.workspace('Daily', workspace_record((1, 3)))
        with patch.object(workbench, 'inventory_snapshot', return_value=self.base), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            workbench.views_command(['status', '--workspace', 'Daily', '--json'],
                                    self.backend, gui=self.gui)
        payload = __import__('json').loads(output.getvalue())
        self.assertEqual([row['identity']['thread_id'] for row in payload['conversations']],
                         [IDS[0], IDS[2]])

    def test_provider_failure_does_not_poison_runtime(self):
        failed = copy.deepcopy(self.base)
        failed['view_provider'] = {'available': False, 'error': 'provider unavailable'}
        failed['conversations'][0]['view_state'] = 'VIEW_UNKNOWN'
        failed['conversations'][0]['view']['state'] = 'VIEW_UNKNOWN'
        with patch.object(workbench, 'inventory_snapshot', return_value=failed), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            workbench.views_command(['status'], self.backend, gui=self.gui)
        self.assertIn('ALIVE', output.getvalue())
        self.assertIn('VIEW_UNKNOWN', output.getvalue())

    def test_client_mapping_failure_degrades_view_health_without_losing_runtime(self):
        raw = {'timestamp': 1, 'context': {'backend': 'zmx', 'host': HOST,
               'runtime_dir': '/tmp/cxdeck-t3-zmx', 'runtime_version': '0.8.1'},
               'sessions': [joined(1)['managed']], 'outside_managed_codex_pids': [],
               'warnings': []}
        backend = Mock(store=self.store)
        backend.processes.return_value = {1: {}}
        backend.raw_snapshot.return_value = raw
        backend.client_tty_map.side_effect = RuntimeError('client tokens unavailable')
        native = Mock()
        native.codex_home.return_value = HOME
        native.inventory.return_value = ([joined(1)], [], [], False)
        self.gui.inventory.return_value = [{'guid': 'guid-1', 'tty': '/dev/ttys1'}]
        with patch.object(workbench, 'native', return_value=native):
            snapshot = workbench.inventory_snapshot(backend, history=[history()], gui=self.gui)
        record = snapshot['conversations'][0]
        self.assertEqual(record['runtime_state'], 'ALIVE')
        self.assertEqual(record['view_state'], 'VIEW_UNKNOWN')
        self.assertFalse(snapshot['view_provider']['available'])
        self.assertIn('client tokens unavailable', snapshot['view_provider']['error'])


class WorkspaceRebuildTests(CommandFixture):
    def setUp(self):
        super().setUp()
        self.store.workspace('Daily', workspace_record((1, 2)))
        self.store.workspace('Other', workspace_record((3,)))

    def test_scope_passes_only_workspace_rows(self):
        fresh = copy.deepcopy(self.base)
        with patch.object(workbench, 'inventory_snapshot', side_effect=[self.base, fresh]), \
             patch.object(workbench, 'focus_rows', return_value={'opened': 0, 'reused': 2}) as show, \
             contextlib.redirect_stdout(io.StringIO()):
            workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        selected = show.call_args.args[0]
        self.assertEqual({row['thread_id'] for row in selected}, {IDS[0], IDS[1]})
        self.assertNotIn(IDS[2], {row['thread_id'] for row in selected})

    def test_overlapping_workspace_scope_never_includes_other_only_members(self):
        self.store.workspace('Overlap', workspace_record((2, 3)))
        with patch.object(workbench, 'inventory_snapshot', side_effect=[self.base, self.base]), \
             patch.object(workbench, 'focus_rows', return_value={'opened': 0, 'reused': 2}) as show, \
             contextlib.redirect_stdout(io.StringIO()):
            workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        selected = {row['thread_id'] for row in show.call_args.args[0]}
        self.assertEqual(selected, {IDS[0], IDS[1]})
        self.assertNotIn(IDS[2], selected)
        self.assertEqual(len(self.store.read()['workspaces']['Overlap']['members']), 2)

    def test_workspace_refresh_is_receipt_only_and_opens_no_client(self):
        stale = copy.deepcopy(self.base)
        stale['conversations'][0]['view_state'] = 'STALE_RECEIPT'
        stale['conversations'][0]['view'].update(state='STALE_RECEIPT', receipt_state='STALE')
        before = copy.deepcopy(self.store.read()['workspaces'])
        with patch.object(workbench, 'inventory_snapshot', side_effect=[stale, self.base]), \
             patch.object(workbench.cx_iterm, 'refresh', return_value={'refreshed': 2, 'missing': 0}) as refresh, \
             patch.object(workbench, 'focus_rows') as show, \
             contextlib.redirect_stdout(io.StringIO()):
            workbench.views_command(['refresh', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        refresh.assert_called_once()
        show.assert_not_called()
        self.gui.open.assert_not_called()
        self.assertEqual(self.store.read()['workspaces'], before)

    def test_no_view_is_created_only_by_explicit_rebuild(self):
        initial = copy.deepcopy(self.base)
        initial['conversations'][1]['view_state'] = 'NO_VIEW'
        initial['conversations'][1]['view'].update(state='NO_VIEW', verified=False,
                                                  guid=None, tty=None, attached_count=0)
        with patch.object(workbench, 'inventory_snapshot', side_effect=[initial, self.base]), \
             patch.object(workbench, 'focus_rows', return_value={'opened': 1, 'reused': 1}) as show, \
             contextlib.redirect_stdout(io.StringIO()):
            workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        show.assert_called_once()

    def test_stale_receipt_is_repaired_only_after_fresh_verification(self):
        stale = copy.deepcopy(self.base)
        stale['conversations'][0]['view_state'] = 'STALE_RECEIPT'
        stale['conversations'][0]['view'].update(state='STALE_RECEIPT', receipt_state='STALE')
        with patch.object(workbench, 'inventory_snapshot', side_effect=[stale, self.base]), \
             patch.object(workbench, 'focus_rows', return_value={'opened': 0, 'reused': 2}), \
             contextlib.redirect_stdout(io.StringIO()):
            workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        receipts = self.store.read()['views']
        self.assertEqual(set(receipts), {row['runtime']['live_key']
                                        for row in self.base['conversations'][:2]})

    def test_ambiguous_client_blocks_before_gui_mutation(self):
        for state in ('MULTIPLE_CLIENTS', 'UNVERIFIED_CLIENT', 'CHANGED_GENERATION', 'VIEW_UNKNOWN'):
            snapshot = copy.deepcopy(self.base)
            snapshot['conversations'][0]['view_state'] = state
            with self.subTest(state=state), \
                 patch.object(workbench, 'inventory_snapshot', return_value=snapshot), \
                 patch.object(workbench, 'focus_rows') as show, \
                 self.assertRaisesRegex(Exception, 'Ambiguous presentation'):
                workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                        gui=self.gui)
            show.assert_not_called()

    def test_saved_only_member_never_resumed_by_rebuild(self):
        snapshot = copy.deepcopy(self.base)
        target = snapshot['conversations'][1]
        target['_managed'] = None
        target['runtime']['managed'] = False
        target['runtime_state'] = 'ABSENT'
        target['view_state'] = 'NOT_APPLICABLE'
        with patch.object(workbench, 'inventory_snapshot', return_value=snapshot), \
             patch.object(workbench, 'focus_rows') as show, \
             self.assertRaisesRegex(Exception, 'never starts runtimes'):
            workbench.views_command(['rebuild', '--workspace', 'Daily'], self.backend,
                                    gui=self.gui)
        show.assert_not_called()

    def test_live_only_workspace_member_is_rejected_as_non_durable(self):
        self.store.workspace('Legacy', {'host': HOST, 'members': [
            {'home': HOME, 'thread_id': None, 'live_key': 'old'}]})
        with patch.object(workbench, 'inventory_snapshot', return_value=self.base), \
             self.assertRaisesRegex(Exception, 'UUID-backed'):
            workbench.views_command(['status', '--workspace', 'Legacy'], self.backend,
                                    gui=self.gui)


class FindAndRoutingTests(CommandFixture):
    def test_find_reports_all_ambiguous_names_without_action(self):
        snapshot = copy.deepcopy(self.base)
        snapshot['conversations'][0]['display']['name'] = 'Shared Name'
        snapshot['conversations'][1]['display']['name'] = 'Shared Name'
        with patch.object(workbench, 'inventory_snapshot', return_value=snapshot), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            workbench.find_command(['shared name'], self.backend)
        self.assertEqual(output.getvalue().count('Shared Name'), 2)
        self.backend.assert_not_called()

    def test_cli_routes_find_and_next_without_normal_focus(self):
        console = Mock()
        backend = Mock()
        with patch.object(console_entry, 'ResumeBackend', return_value=backend), \
             patch.object(workbench, 'find_command', return_value=0) as find:
            self.assertEqual(console_entry.main(['find', 'model'], console), 0)
        find.assert_called_once_with(['model'], backend)
        with patch.object(console_entry, 'ResumeBackend', return_value=backend), \
             patch.object(workbench, 'focus_navigation') as navigate, \
             patch.object(workbench, 'focus_rows') as focus:
            console_entry.main(['focus', '--previous'], console)
        navigate.assert_called_once_with('previous', backend)
        focus.assert_not_called()

    def test_resume_json_uses_canonical_inventory_without_launching(self):
        args = types.SimpleNamespace(limit=2000, list=False, json=True, group=None,
                                     per_tab=0, min_columns=70, min_rows=12)
        native = Mock()
        native.codex_home.return_value = HOME
        native.list_history.return_value = ([history(1)], False)
        with patch.object(workbench, 'native', return_value=native), \
             patch.object(workbench, 'inventory_snapshot', return_value=self.base), \
             patch.object(workbench, 'launch_selected') as launch, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            workbench.resume(args, self.backend)
        self.assertEqual(json.loads(output.getvalue())['schema'], cx_inventory.SCHEMA)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
