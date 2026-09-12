"""T2 exact-restore planning and coordination tests; no live GUI/runtime."""
from __future__ import annotations

import contextlib
import copy
import io
from pathlib import Path
import socket
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import cx_store
import cx_workspace_restore as restore
import workbench


HOST = socket.gethostname()
RUNTIME = '/private/tmp/cxdeck-t2-test'


def uuid_for(index):
    return f'00000000-0000-4000-8000-{index:012d}'


def identity(home, index):
    return {'host': HOST, 'codex_home': home, 'thread_id': uuid_for(index)}


def conversation(home, index):
    return {'type': 'conversation', 'identity': identity(home, index)}


def split(axis, *children, ratios=None):
    value = {'type': 'split', 'axis': axis, 'children': list(children)}
    if ratios is not None:
        value['ratio_hints'] = ratios
    return value


def make_layout(home, roots, frames=False):
    windows = []
    for wi, tabs in enumerate(roots):
        value = {'tabs': [{'root': root} for root in tabs]}
        if frames:
            value['frame_hint'] = {'x': wi * 900, 'y': 0, 'width': 900, 'height': 700}
        windows.append(value)
    return {'schema': 'cxdeck.workspace-layout/v1',
            'capture': {'provider': 'iterm2-python', 'provider_version': '2.23',
                        'fidelity': 'L3', 'captured_at': '2026-09-10T12:00:00Z'},
            'windows': windows}


def indexes_in(node):
    if node['type'] == 'conversation':
        return [int(node['identity']['thread_id'][-12:])]
    return [index for child in node['children'] for index in indexes_in(child)]


def workspace(home, layout):
    indexes = [index for window in layout['windows'] for tab in window['tabs']
               for index in indexes_in(tab['root'])]
    return {'host': HOST, 'members': [
        {'home': home, 'thread_id': uuid_for(index), 'title': f'Agent {index}',
         'cwd': home, 'live_key': f'key-{index}', 'session': f'session-{index}'}
        for index in indexes], 'layout': {'per_tab': 0}, 'exact_layout': layout}


def managed(home, index, *, state='ALIVE', created=None):
    created = created or 1000 + index
    row = {'backend': 'zmx', 'session': f'session-{index}', 'sid': f'session-{index}',
           'daemon_pid': 2000 + index, 'created': created, 'state': state,
           'attached': 1, 'codex_home': home, 'thread_id': uuid_for(index),
           'codex_pids': [3000 + index], '_key': f'key-{index}', 'task': f'Agent {index}'}
    row['generation'] = {'host': HOST, 'runtime_dir': RUNTIME,
                         'session': row['session'], 'daemon_pid': row['daemon_pid'],
                         'created': row['created']}
    return row


def inventory(home, index, live=True, external=(), state='ALIVE'):
    row = managed(home, index, state=state) if live else None
    return {'key': uuid_for(index), 'thread_id': uuid_for(index), 'home': home,
            'title': f'Agent {index}', 'cwd': home, 'managed': row,
            'external_pids': list(external), 'state': row['state'] if row else 'SAVED'}


def observation(rows, *, unknown=(), failed=False):
    sessions = []
    for item in rows:
        if item.get('managed'):
            row = copy.deepcopy(item['managed'])
            row['external_pids'] = list(item.get('external_pids') or [])
            sessions.append(row)
    return {'context': {'backend': 'zmx', 'host': HOST, 'runtime_dir': RUNTIME,
                        'runtime_path': '/bin/zmx'},
            'sessions': sessions,
            'clients': {row['sid']: [f'/dev/ttys{n + 1}']
                        for n, row in enumerate(sessions)},
            'views': {}, 'inventory': rows, 'unknown_pids': list(unknown),
            'process_failed': failed, 'warnings': []}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str((Path(self.temp.name) / 'codex').resolve())
        self.layout = make_layout(self.home, [[split('columns', conversation(self.home, 1),
                                                    conversation(self.home, 2))]])
        self.workspace = workspace(self.home, self.layout)
        self.context = {'host': HOST, 'runtime_dir': RUNTIME}

    def plan(self, rows, **kwargs):
        return restore.build_runtime_plan(
            self.layout, self.workspace, rows, kwargs.pop('unknown', []),
            kwargs.pop('failed', False), host=kwargs.pop('host', HOST),
            codex_home=kwargs.pop('home', self.home), context=self.context,
            planned_cwd=lambda row, override: override or row['cwd'], **kwargs)

    def test_all_live_all_saved_and_mixed_classification(self):
        live = self.plan([inventory(self.home, 1), inventory(self.home, 2)])
        self.assertEqual([item.classification for item in live.items],
                         ['LIVE_MANAGED', 'LIVE_MANAGED'])
        saved = self.plan([inventory(self.home, 1, False), inventory(self.home, 2, False)])
        self.assertEqual((saved.cold_count, [item.classification for item in saved.items]),
                         (2, ['SAVED_ONLY', 'SAVED_ONLY']))
        mixed = self.plan([inventory(self.home, 1), inventory(self.home, 2, False)])
        self.assertEqual(mixed.cold_count, 1)

    def test_duplicate_managed_uuid_hard_blocks(self):
        first = inventory(self.home, 1)
        second = copy.deepcopy(first)
        second['managed']['created'] = second['managed']['generation']['created'] = 9999
        with self.assertRaisesRegex(restore.RestoreError, 'Two managed generations'):
            self.plan([first, second, inventory(self.home, 2)])

    def test_known_external_duplicate_hard_blocks(self):
        with self.assertRaisesRegex(restore.RestoreError, 'Known external'):
            self.plan([inventory(self.home, 1, external=[77]), inventory(self.home, 2)])

    def test_unidentified_process_blocks_saved_only_unless_explicit(self):
        rows = [inventory(self.home, 1), inventory(self.home, 2, False)]
        with self.assertRaisesRegex(restore.RestoreError, 'Unidentified live Codex'):
            self.plan(rows, unknown=[88])
        self.assertEqual(self.plan(rows, unknown=[88], allow_unverified_live=True).cold_count, 1)
        with self.assertRaisesRegex(restore.RestoreError, 'Unidentified live Codex'):
            self.plan(rows, failed=True)

    def test_missing_history_wrong_home_and_wrong_host_block(self):
        with self.assertRaisesRegex(restore.RestoreError, 'absent from saved history'):
            self.plan([inventory(self.home, 1)])
        with self.assertRaisesRegex(restore.RestoreError, 'active CODEX_HOME'):
            self.plan([inventory(self.home, 1), inventory(self.home, 2)], home='/other')
        wrong = copy.deepcopy(self.workspace)
        wrong['host'] = 'other-host'
        with self.assertRaisesRegex(restore.RestoreError, 'another host'):
            restore.build_runtime_plan(self.layout, wrong, [], [], False,
                                       host=HOST, codex_home=self.home)

    def test_changed_or_nonlive_generation_blocks(self):
        changed = inventory(self.home, 1)
        changed['managed']['generation']['created'] += 1
        with self.assertRaisesRegex(restore.RestoreError, 'generation disagrees'):
            self.plan([changed, inventory(self.home, 2)])
        with self.assertRaisesRegex(restore.RestoreError, 'not safely live'):
            self.plan([inventory(self.home, 1, state='NO_CODEX'), inventory(self.home, 2)])

    def test_layout_membership_mismatch_blocks(self):
        wrong = copy.deepcopy(self.workspace)
        wrong['members'].pop()
        with self.assertRaisesRegex(restore.RestoreError, 'do not equal'):
            restore.build_runtime_plan(self.layout, wrong, [], [], False,
                                       host=HOST, codex_home=self.home)

    def test_control_char_display_name_blocks_before_runtime_or_presentation(self):
        self.workspace['members'][0]['title'] = 'unsafe\x1b]0;title'
        with self.assertRaisesRegex(restore.RestoreError, 'unsafe presentation name'):
            self.plan([inventory(self.home, 1), inventory(self.home, 2)])


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str((Path(self.temp.name) / 'codex').resolve())

    def ids(self, *indexes):
        return [tuple(identity(self.home, index).values()) for index in indexes]

    def test_columns_rows_mixed_opposite_and_nary_moves(self):
        fixtures = [
            (split('columns', conversation(self.home, 1), conversation(self.home, 2)),
             [('columns', False)]),
            (split('rows', conversation(self.home, 1), conversation(self.home, 2)),
             [('rows', True)]),
            (split('columns', conversation(self.home, 1),
                   split('rows', conversation(self.home, 2), conversation(self.home, 3))),
             [('columns', False), ('rows', True)]),
            (split('rows', split('columns', conversation(self.home, 1), conversation(self.home, 2)),
                   conversation(self.home, 3)), [('rows', True), ('columns', False)]),
            (split('columns', conversation(self.home, 1), conversation(self.home, 2),
                   conversation(self.home, 3)), [('columns', False), ('columns', False)]),
        ]
        for root, expected_moves in fixtures:
            with self.subTest(root=root):
                blueprint = restore.reconstruction_blueprint(make_layout(self.home, [[root]]))
                self.assertEqual([(move['axis'], move['before']) for move in blueprint['moves']],
                                 expected_moves)

    def test_multi_window_tab_anchors_preserve_order(self):
        layout = make_layout(self.home, [
            [conversation(self.home, 1), conversation(self.home, 2)],
            [split('columns', conversation(self.home, 3), conversation(self.home, 4))]])
        plan = restore.reconstruction_blueprint(layout)
        self.assertEqual([window['anchor'][2] for window in plan['windows']],
                         [uuid_for(1), uuid_for(3)])
        self.assertEqual([tab['anchor'][2] for tab in plan['windows'][0]['tabs']],
                         [uuid_for(1), uuid_for(2)])

    def test_structural_comparison_ignores_only_hints(self):
        first = make_layout(self.home, [[split('columns', conversation(self.home, 1),
                                              conversation(self.home, 2), ratios=[0.4, 0.6])]], frames=True)
        second = copy.deepcopy(first)
        second['windows'][0]['frame_hint']['width'] = 1500
        second['windows'][0]['tabs'][0]['root']['ratio_hints'] = [0.2, 0.8]
        second['capture']['captured_at'] = '2027-01-01T00:00:00Z'
        self.assertEqual(restore.structural_layout(first), restore.structural_layout(second))
        second['windows'][0]['tabs'][0]['root']['children'].reverse()
        self.assertNotEqual(restore.structural_layout(first), restore.structural_layout(second))

    def test_window_stacking_order_is_a_hint_but_tab_order_is_exact(self):
        first = make_layout(self.home, [[conversation(self.home, 1),
                                         conversation(self.home, 2)],
                                        [conversation(self.home, 3)]])
        window_reordered = copy.deepcopy(first)
        window_reordered['windows'].reverse()
        self.assertEqual(restore.structural_layout(first),
                         restore.structural_layout(window_reordered))
        tab_reordered = copy.deepcopy(first)
        tab_reordered['windows'][0]['tabs'].reverse()
        self.assertNotEqual(restore.structural_layout(first),
                            restore.structural_layout(tab_reordered))


class RuntimeRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = str((Path(self.temp.name) / 'codex').resolve())
        self.layout = make_layout(self.home, [[conversation(self.home, 1)]])
        self.record = workspace(self.home, self.layout)
        row = inventory(self.home, 1)
        self.plan = restore.build_runtime_plan(
            self.layout, self.record, [row], [], False, host=HOST,
            codex_home=self.home, context={'host': HOST, 'runtime_dir': RUNTIME})
        self.observed = observation([row])

    def test_exact_generation_and_pid_revalidate(self):
        rows = restore._runtime_rows(self.plan.items, self.observed)
        self.assertEqual(rows[self.plan.items[0].identity]['daemon_pid'], 2001)

    def test_generation_change_fails_closed(self):
        changed = copy.deepcopy(self.observed)
        changed['sessions'][0]['created'] = 9999
        changed['sessions'][0]['generation']['created'] = 9999
        with self.assertRaisesRegex(restore.RestoreError, 'generation changed'):
            restore._runtime_rows(self.plan.items, changed)

    def test_pid_change_fails_closed(self):
        changed = copy.deepcopy(self.observed)
        changed['sessions'][0]['codex_pids'] = [9999]
        with self.assertRaisesRegex(restore.RestoreError, 'PID set changed'):
            restore._runtime_rows(self.plan.items, changed)

    def test_external_duplicate_appearing_fails_closed(self):
        changed = copy.deepcopy(self.observed)
        changed['sessions'][0]['external_pids'] = [444]
        with self.assertRaisesRegex(restore.RestoreError, 'external duplicate'):
            restore._runtime_rows(self.plan.items, changed)

    def test_disappeared_or_duplicate_generation_fails_closed(self):
        with self.assertRaisesRegex(restore.RestoreError, 'exactly one'):
            restore._runtime_rows(self.plan.items, observation([]))
        changed = copy.deepcopy(self.observed)
        changed['sessions'].append(copy.deepcopy(changed['sessions'][0]))
        changed['sessions'][1]['created'] = 8888
        changed['sessions'][1]['generation']['created'] = 8888
        with self.assertRaisesRegex(restore.RestoreError, 'exactly one'):
            restore._runtime_rows(self.plan.items, changed)

    def test_unknown_process_does_not_block_all_live_plan(self):
        plan = restore.build_runtime_plan(
            self.layout, self.record, [inventory(self.home, 1)], [555], False,
            host=HOST, codex_home=self.home,
            context={'host': HOST, 'runtime_dir': RUNTIME})
        self.assertEqual(plan.cold_count, 0)

    def test_exact_observation_uses_one_process_and_one_client_snapshot(self):
        store = cx_store.Store(Path(self.temp.name) / 'observation-state')
        backend = Mock(store=store)
        process_table = {2001: {'stat': 'S'}}
        backend.processes.return_value = process_table
        raw_row = managed(self.home, 1)
        backend.raw_snapshot.return_value = {
            'context': {'backend': 'zmx', 'host': HOST, 'runtime_dir': RUNTIME},
            'sessions': [raw_row], 'warnings': []}
        backend.client_tty_map.return_value = {raw_row['sid']: {'/dev/ttys1'}}
        native = Mock()
        native.inventory.return_value = ([inventory(self.home, 1)], [], [], False)
        with patch.object(workbench, 'native', return_value=native):
            result = workbench._exact_runtime_observation(backend, [], self.home)
        backend.processes.assert_called_once_with()
        backend.raw_snapshot.assert_called_once_with(
            bind_threads=False, process_table=process_table, include_diagnostics=False)
        backend.client_tty_map.assert_called_once()
        backend.snapshot.assert_not_called()
        self.assertEqual(result['clients'][raw_row['sid']], ['/dev/ttys1'])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = str(self.root / 'codex')
        self.layout = make_layout(self.home, [[conversation(self.home, 1)]])
        self.record = workspace(self.home, self.layout)
        self.store = cx_store.Store(self.root / 'state')
        self.store.workspace('daily', self.record)
        self.backend = types.SimpleNamespace(VERSION='0.8.0-dev', store=self.store,
                                             clean=lambda value: str(value))
        self.args = types.SimpleNamespace(no_iterm=False, allow_unverified_live=False,
                                          cwd=None, yolo=True, no_dashboard=True)
        self.native = Mock()
        self.native.codex_home.return_value = self.home
        self.native.list_history.return_value = ([inventory(self.home, 1, False)], False)
        self.native.planned_cwd.side_effect = lambda row, override: override or row['cwd']
        self.native.launch_lock.return_value = contextlib.nullcontext()

    def result(self):
        return restore.RestoreResult('COMPLETE', 0, 0, 0, 1, (),
                                     {'key-1': {'guid': 'g1', 'tty': '/dev/ttys1'}})

    def execute(self, observations, **patches):
        with patch.object(workbench, 'native', return_value=self.native), \
             patch.object(workbench, '_exact_runtime_observation', side_effect=observations), \
             patch.object(restore, 'preflight_live', **patches.get('preflight', {})) as preflight, \
             patch.object(restore, 'restore_live', return_value=self.result(),
                          **patches.get('restore', {})) as apply, \
             patch.object(workbench.cx_iterm, 'caller_tty', return_value='/dev/caller'), \
             patch.object(workbench.cx_iterm, 'ensure_profile'), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                value = workbench.workspace_open_exact('daily', self.record, self.args, self.backend)
                error = None
            except cx_store.StateError as exc:
                value, error = None, exc
        return value, error, preflight, apply

    def test_saved_only_uses_existing_exact_resume_then_presentation(self):
        saved = observation([inventory(self.home, 1, False)])
        live_row = inventory(self.home, 1)
        live = observation([live_row])
        self.native.ensure.return_value = (live_row['managed'], 'resume launched in zmx')
        value, error, preflight, apply = self.execute([saved, saved, live, live])
        self.assertIsNone(error)
        self.native.ensure.assert_called_once()
        self.assertTrue(self.native.ensure.call_args.kwargs['yolo'])
        preflight.assert_called_once()
        apply.assert_called_once()
        self.assertEqual(value.state, 'COMPLETE')

    def test_saved_only_safe_policy_is_forwarded(self):
        self.args.yolo = False
        saved = observation([inventory(self.home, 1, False)])
        live_row = inventory(self.home, 1)
        live = observation([live_row])
        self.native.ensure.return_value = (live_row['managed'], 'resume launched in zmx')
        _, error, _, _ = self.execute([saved, saved, live, live])
        self.assertIsNone(error)
        self.assertFalse(self.native.ensure.call_args.kwargs['yolo'])

    def test_plan_failure_mutates_no_runtime_or_gui(self):
        blocked = observation([inventory(self.home, 1, False)], unknown=[99])
        _, error, preflight, apply = self.execute([blocked])
        self.assertIn('RUNTIME_BLOCKED', str(error))
        self.native.ensure.assert_not_called()
        preflight.assert_not_called()
        apply.assert_not_called()

    def test_presentation_preflight_failure_precedes_cold_resume(self):
        saved = observation([inventory(self.home, 1, False)])
        _, error, _, apply = self.execute(
            [saved], preflight={'side_effect': restore.RestoreError(
                'PRESENTATION_CONFLICT', 'caller pane')})
        self.assertIn('PRESENTATION_CONFLICT', str(error))
        self.native.ensure.assert_not_called()
        apply.assert_not_called()

    def test_partial_runtime_launch_never_starts_presentation(self):
        layout = make_layout(self.home, [[split('columns', conversation(self.home, 1),
                                               conversation(self.home, 2))]])
        self.record = workspace(self.home, layout)
        saved = observation([inventory(self.home, 1, False), inventory(self.home, 2, False)])
        mixed = observation([inventory(self.home, 1), inventory(self.home, 2, False)])
        self.native.ensure.side_effect = [(managed(self.home, 1), 'created'), RuntimeError('failed')]
        _, error, _, apply = self.execute([saved, saved, mixed])
        self.assertIn('RUNTIME_PARTIAL', str(error))
        self.assertEqual(self.native.ensure.call_count, 2)
        apply.assert_not_called()

    def test_new_duplicate_before_second_resume_stops_remaining_batch(self):
        layout = make_layout(self.home, [[split('columns', conversation(self.home, 1),
                                               conversation(self.home, 2))]])
        self.record = workspace(self.home, layout)
        saved = observation([inventory(self.home, 1, False), inventory(self.home, 2, False)])
        changed = observation([inventory(self.home, 1),
                               inventory(self.home, 2, False, external=[91])])
        self.native.ensure.return_value = (managed(self.home, 1), 'created')
        _, error, _, apply = self.execute([saved, saved, changed])
        self.assertIn('RUNTIME_PARTIAL', str(error))
        self.assertIn('Known external', str(error))
        self.assertEqual(self.native.ensure.call_count, 1)
        apply.assert_not_called()

    def test_external_duplicate_appearing_after_launch_blocks_gui(self):
        saved = observation([inventory(self.home, 1, False)])
        created = inventory(self.home, 1)
        duplicate = inventory(self.home, 1, external=[77])
        self.native.ensure.return_value = (created['managed'], 'created')
        _, error, _, apply = self.execute([saved, saved, observation([duplicate])])
        self.assertIn('RUNTIME_PARTIAL', str(error))
        apply.assert_not_called()

    def test_gui_failure_reports_presentation_only_after_runtime_lands(self):
        saved = observation([inventory(self.home, 1, False)])
        live = observation([inventory(self.home, 1)])
        self.native.ensure.return_value = (live['sessions'][0], 'created')
        _, error, _, _ = self.execute(
            [saved, saved, live, live],
            restore={'side_effect': restore.RestoreError('PRESENTATION_PARTIAL', 'GUI stopped')})
        self.assertIn('PRESENTATION_PARTIAL', str(error))
        self.assertIn('runtimes were retained', str(error))
        self.assertEqual(self.native.ensure.call_count, 1)

    def test_no_iterm_resolves_runtime_without_optional_dependency(self):
        self.args.no_iterm = True
        saved = observation([inventory(self.home, 1, False)])
        live = observation([inventory(self.home, 1)])
        self.native.ensure.return_value = (live['sessions'][0], 'created')
        value, error, preflight, apply = self.execute([saved, saved, live])
        self.assertIsNone(error)
        self.assertEqual(value['state'], 'RUNTIME_RESOLVED')
        preflight.assert_not_called()
        apply.assert_not_called()

    def test_list_is_observational(self):
        self.args.list = True
        live = observation([inventory(self.home, 1)])
        value, error, preflight, apply = self.execute([live])
        self.assertIsNone(error)
        self.assertEqual(value.items[0].classification, 'LIVE_MANAGED')
        self.native.ensure.assert_not_called()
        preflight.assert_not_called()
        apply.assert_not_called()

    def test_view_receipts_commit_as_one_validated_batch(self):
        self.store.set_view_receipts({'one': {'guid': 'g', 'tty': '/dev/ttys1'}})
        before = copy.deepcopy(self.store.read())
        with self.assertRaises(cx_store.StateError):
            self.store.set_view_receipts({'two': {'guid': '', 'tty': 'bad'}})
        self.assertEqual(self.store.read(), before)


if __name__ == '__main__':
    unittest.main()
