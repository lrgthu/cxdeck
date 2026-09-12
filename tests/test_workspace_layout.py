"""Production workspace-layout capture contract tests; no live runtimes or GUI."""
from __future__ import annotations

import asyncio
import contextlib
import copy
from datetime import datetime, timezone
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import cx_store
import cx_workspace_layout as wl
import workbench


HOST = socket.gethostname()
RUNTIME = '/private/tmp/cxdeck-layout-test'


def tid(index):
    return f'00000000-0000-4000-8000-{index:012d}'


def leaf(index, *, columns=80, rows=24, guid=None, tty=None):
    return {'type': 'session', 'guid': guid or f'guid-{index}',
            'tty': tty or f'/dev/ttys{index}',
            'grid': {'columns': columns, 'rows': rows},
            'frame': {'x': index * 10, 'y': 0, 'width': columns * 8, 'height': rows * 16}}


def split(axis, *children):
    return {'type': 'split', 'axis': axis, 'children': list(children)}


def raw(root=None, *, tabs=None, windows=None, frame=None):
    if windows is None:
        tabs = tabs or [{'tab_id': 'tab-1', 'minimized_session_ids': [],
                         'root': root or leaf(1)}]
        windows = [{'window_id': 'window-1',
                    'frame': frame or {'x': 0, 'y': 0, 'width': 1200, 'height': 800},
                    'tabs': tabs}]
    return {'provider': wl.PROVIDER, 'provider_version': '2.23', 'windows': windows}


def workspace(home, indexes=(1, 2)):
    return {'host': HOST, 'members': [
        {'home': home, 'thread_id': tid(index), 'title': f'Agent {index}',
         'live_key': f'key-{index}', 'session': f'session-{index}'}
        for index in indexes], 'layout': {'per_tab': 0}}


def row(home, index, *, attached=1, state='ALIVE', thread=None, codex_home=None,
        created=None, generation=None):
    created = created or 1000 + index
    result = {'backend': 'zmx', 'session': f'session-{index}', 'sid': f'sid-{index}',
              'daemon_pid': 2000 + index, 'created': created,
              'codex_home': home if codex_home is None else codex_home,
              'thread_id': tid(index) if thread is None else thread,
              'state': state, 'attached': attached, 'task': f'Agent {index}',
              '_key': f'key-{index}'}
    result['generation'] = generation or {
        'host': HOST, 'runtime_dir': RUNTIME, 'session': result['session'],
        'daemon_pid': result['daemon_pid'], 'created': result['created']}
    return result


def runtime(home, indexes=(1, 2), *, rows=None, clients=None, views=None):
    rows = rows or [row(home, index) for index in indexes]
    clients = clients or {f'sid-{index}': [f'/dev/ttys{index}'] for index in indexes}
    return {'context': {'backend': 'zmx', 'host': HOST, 'runtime_dir': RUNTIME},
            'sessions': rows, 'clients': clients, 'views': views or {}}


class SequenceProvider:
    def __init__(self, values):
        self.values = [copy.deepcopy(value) for value in values]
        self.calls = 0

    async def read(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return copy.deepcopy(value)


class SequenceRuntime:
    def __init__(self, values):
        self.values = [copy.deepcopy(value) for value in values]
        self.calls = 0

    def __call__(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return copy.deepcopy(value)


def capture(ws, topology, observation, *, attempts=1, raws=None, runtimes=None):
    provider = SequenceProvider(raws or [topology, topology])
    reader = SequenceRuntime(runtimes or [observation, observation])
    clock = lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    return asyncio.run(wl.capture_contract(ws, provider, reader, attempts=attempts, clock=clock))


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name).resolve() / 'codex')
        root = split('columns', leaf(1), split('rows', leaf(2), leaf(3)))
        result = capture(workspace(self.home, (1, 2, 3)), raw(root),
                         runtime(self.home, (1, 2, 3)))
        self.layout = result.layout

    def reject(self, edit):
        value = copy.deepcopy(self.layout)
        edit(value)
        with self.assertRaises(wl.LayoutError):
            wl.validate_layout(value)

    def test_valid_nested_layout_and_conversation_leaves(self):
        self.assertEqual(wl.validate_layout(self.layout), self.layout)
        self.assertEqual(len(wl.conversation_identities(self.layout)), 3)
        self.assertEqual(self.layout['windows'][0]['tabs'][0]['root']['axis'], 'columns')

    def test_nary_split_and_ratios(self):
        result = capture(workspace(self.home, (1, 2, 3)),
                         raw(split('columns', leaf(1, columns=40), leaf(2, columns=80),
                                   leaf(3, columns=40))), runtime(self.home, (1, 2, 3)))
        root = result.layout['windows'][0]['tabs'][0]['root']
        self.assertEqual(len(root['children']), 3)
        self.assertEqual(root['ratio_hints'], [0.25, 0.5, 0.25])

    def test_single_child_live_root_is_normalized(self):
        result = capture(workspace(self.home, (1,)),
                         raw(split('rows', leaf(1))), runtime(self.home, (1,)))
        self.assertEqual(result.layout['windows'][0]['tabs'][0]['root']['type'], 'conversation')

    def test_duplicate_conversation_is_rejected(self):
        root = self.layout['windows'][0]['tabs'][0]['root']
        self.reject(lambda value: value['windows'][0]['tabs'][0].__setitem__(
            'root', {'type': 'split', 'axis': 'rows', 'children':
                     [copy.deepcopy(root['children'][0]), copy.deepcopy(root['children'][0])] }))

    def test_bad_uuid_and_relative_home_are_rejected(self):
        def identity(value):
            return value['windows'][0]['tabs'][0]['root']['children'][0]['identity']
        self.reject(lambda value: identity(value).__setitem__('thread_id', 'not-a-uuid'))
        self.reject(lambda value: identity(value).__setitem__('codex_home', 'relative/home'))

    def test_bad_schema_and_unknown_node_are_rejected(self):
        self.reject(lambda value: value.__setitem__('schema', 'cxdeck.workspace-layout/v2'))
        self.reject(lambda value: value['windows'][0]['tabs'][0].__setitem__('root', {'type': 'shell'}))

    def test_excessive_nesting_and_oversized_leaf_sets_are_rejected(self):
        def durable(index):
            return {'type': 'conversation', 'identity': {
                'host': HOST, 'codex_home': self.home, 'thread_id': tid(index)}}
        deep = durable(1)
        for index in range(2, 72):
            deep = {'type': 'split', 'axis': 'columns',
                    'children': [deep, durable(index)]}
        value = copy.deepcopy(self.layout)
        value['windows'][0]['tabs'][0]['root'] = deep
        with self.assertRaisesRegex(wl.LayoutError, 'excessively deep'):
            wl.validate_layout(value)
        value = copy.deepcopy(self.layout)
        value['windows'][0]['tabs'][0]['root'] = {
            'type': 'split', 'axis': 'columns',
            'children': [durable(index) for index in range(1, 4098)]}
        with self.assertRaisesRegex(wl.LayoutError, 'too many conversations'):
            wl.validate_layout(value)

    def test_bad_ratio_values_are_rejected(self):
        root = lambda value: value['windows'][0]['tabs'][0]['root']
        self.reject(lambda value: root(value).__setitem__('ratio_hints', [1.0]))
        self.reject(lambda value: root(value).__setitem__('ratio_hints', [-0.1, 1.1]))
        self.reject(lambda value: root(value).__setitem__('ratio_hints', [float('nan'), 0.5]))

    def test_invalid_axis_and_small_split_are_rejected(self):
        root = lambda value: value['windows'][0]['tabs'][0]['root']
        self.reject(lambda value: root(value).__setitem__('axis', 'horizontal'))
        self.reject(lambda value: root(value).__setitem__('children', root(value)['children'][:1]))

    def test_invalid_frame_hints_are_rejected(self):
        self.reject(lambda value: value['windows'][0]['frame_hint'].__setitem__('width', -1))
        self.reject(lambda value: value['windows'][0]['frame_hint'].__setitem__('height', float('inf')))
        self.reject(lambda value: value['windows'][0]['frame_hint'].__setitem__('monitor', 1))

    def test_unknown_durable_fields_are_rejected(self):
        self.reject(lambda value: value['windows'][0].__setitem__('window_id', 'ephemeral'))
        self.reject(lambda value: value['windows'][0]['tabs'][0]['root'].__setitem__('guid', 'ephemeral'))


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name).resolve() / 'codex')
        self.ws = workspace(self.home)
        self.topology = raw(split('columns', leaf(1), leaf(2)))
        self.runtime = runtime(self.home)

    def assert_refused(self, observation=None, topology=None, text=None):
        with self.assertRaises(wl.LayoutError) as raised:
            capture(self.ws, topology or self.topology, observation or self.runtime)
        if text:
            self.assertIn(text, str(raised.exception))

    def test_exact_guid_tty_client_generation_binding(self):
        result = capture(self.ws, self.topology, self.runtime)
        self.assertEqual(result.receipt['windows'][0]['tabs'][0]['root']['children'][0]['guid'], 'guid-1')
        durable = result.layout['windows'][0]['tabs'][0]['root']['children'][0]
        self.assertEqual(set(durable['identity']), {'host', 'codex_home', 'thread_id'})
        self.assertNotIn('guid', str(result.layout))
        self.assertNotIn('/dev/ttys', str(result.layout))
        self.assertNotIn('session-1', str(result.layout))

    def test_zero_and_two_client_matches_are_rejected(self):
        missing = copy.deepcopy(self.runtime)
        missing['clients']['sid-1'] = []
        self.assert_refused(missing, text='No verified zmx client')
        multiple = copy.deepcopy(self.runtime)
        multiple['sessions'][0]['attached'] = 2
        multiple['clients']['sid-1'] = ['/dev/ttys1', '/dev/ttys9']
        self.assert_refused(multiple, text='Multiple zmx clients')

    def test_generation_disagreement_is_rejected(self):
        changed = copy.deepcopy(self.runtime)
        changed['sessions'][0]['generation']['created'] += 1
        self.assert_refused(changed, text='generation disagrees')

    def test_missing_uuid_and_home_are_rejected(self):
        missing_uuid = runtime(self.home, rows=[row(self.home, 1, thread=''), row(self.home, 2)])
        self.assert_refused(missing_uuid, text='not a verified live managed')
        missing_home = runtime(self.home, rows=[row(self.home, 1, codex_home=''), row(self.home, 2)])
        self.assert_refused(missing_home, text='not a verified live managed')

    def test_two_managed_generations_claiming_identity_are_rejected(self):
        duplicate = row(self.home, 9)
        duplicate['thread_id'] = tid(1)
        observation = runtime(self.home, rows=self.runtime['sessions'] + [duplicate],
                              clients={**self.runtime['clients'], 'sid-9': ['/dev/ttys9']})
        self.assert_refused(observation, text='Two managed zmx generations')

    def test_duplicate_leaf_and_generation_are_rejected(self):
        duplicate_leaf = raw(split('columns', leaf(1),
                                   leaf(1, guid='guid-other', tty='/dev/ttys1')))
        self.assert_refused(topology=duplicate_leaf, text='duplicate session GUID/TTY')
        duplicate_generation = copy.deepcopy(self.runtime)
        duplicate_generation['sessions'][1]['generation'] = copy.deepcopy(
            duplicate_generation['sessions'][0]['generation'])
        duplicate_generation['sessions'][1].update(
            session='session-1', daemon_pid=2001, created=1001)
        self.assert_refused(duplicate_generation, text='one zmx generation')

    def test_stale_cached_view_is_rejected(self):
        stale = copy.deepcopy(self.runtime)
        stale['views']['key-1'] = {'guid': 'old-guid', 'tty': '/dev/ttys1'}
        self.assert_refused(stale, text='Cached iTerm view receipt disagrees')

    def test_mixed_unmanaged_pane_is_rejected(self):
        mixed = raw(split('columns', leaf(1), leaf(2), leaf(99)))
        self.assert_refused(topology=mixed, text='Unverified iTerm pane')

    def test_unexpected_managed_pane_is_rejected(self):
        mixed = raw(split('columns', leaf(1), leaf(2), leaf(3)))
        observation = runtime(self.home, (1, 2, 3))
        self.assert_refused(observation, mixed, 'Unexpected managed conversation')

    def test_unrelated_other_window_is_ignored(self):
        topology = raw(windows=[
            {'window_id': 'selected', 'frame': {'x': 0, 'y': 0, 'width': 800, 'height': 600},
             'tabs': [{'tab_id': 'selected-tab', 'minimized_session_ids': [],
                       'root': split('columns', leaf(1), leaf(2))}]},
            {'window_id': 'unrelated', 'frame': {'x': 800, 'y': 0, 'width': 800, 'height': 600},
             'tabs': [{'tab_id': 'unrelated-tab', 'minimized_session_ids': [], 'root': leaf(99)}]}])
        result = capture(self.ws, topology, self.runtime)
        self.assertEqual(len(result.layout['windows']), 1)

    def test_minimized_session_state_is_rejected(self):
        topology = copy.deepcopy(self.topology)
        topology['windows'][0]['tabs'][0]['minimized_session_ids'] = ['guid-2']
        self.assert_refused(topology=topology, text='minimized/maximized')


class DoubleReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name).resolve() / 'codex')
        self.ws = workspace(self.home)
        self.a = raw(split('columns', leaf(1), leaf(2)))
        self.observation = runtime(self.home)

    def changed(self, mutate_raw=None, mutate_runtime=None):
        b = copy.deepcopy(self.a)
        rb = copy.deepcopy(self.observation)
        if mutate_raw:
            mutate_raw(b)
        if mutate_runtime:
            mutate_runtime(rb)
        with self.assertRaises(wl.LayoutChanged):
            capture(self.ws, self.a, self.observation, raws=[self.a, b],
                    runtimes=[self.observation, rb])

    def test_equal_reads_pass(self):
        self.assertEqual(capture(self.ws, self.a, self.observation).layout['schema'], wl.SCHEMA)

    def test_pane_added_or_removed_fails(self):
        self.changed(lambda value: value['windows'][0]['tabs'][0].__setitem__(
            'root', split('columns', leaf(1), leaf(2), leaf(99))))
        self.changed(lambda value: value['windows'][0]['tabs'][0].__setitem__('root', leaf(1)))

    def test_tab_reorder_and_session_move_fail(self):
        base = raw(tabs=[{'tab_id': 'tab-1', 'minimized_session_ids': [], 'root': leaf(1)},
                         {'tab_id': 'tab-2', 'minimized_session_ids': [], 'root': leaf(2)}])
        swapped = copy.deepcopy(base)
        swapped['windows'][0]['tabs'].reverse()
        with self.assertRaises(wl.LayoutChanged):
            capture(self.ws, base, self.observation, raws=[base, swapped])
        moved = copy.deepcopy(base)
        moved['windows'][0]['tabs'][0]['root'], moved['windows'][0]['tabs'][1]['root'] = (
            moved['windows'][0]['tabs'][1]['root'], moved['windows'][0]['tabs'][0]['root'])
        with self.assertRaises(wl.LayoutChanged):
            capture(self.ws, base, self.observation, raws=[base, moved])

    def test_tree_axis_and_geometry_change_fail(self):
        self.changed(lambda value: value['windows'][0]['tabs'][0]['root'].__setitem__('axis', 'rows'))
        self.changed(lambda value: value['windows'][0]['tabs'][0]['root']['children'][0]['grid'].__setitem__('columns', 70))

    def test_zmx_client_or_generation_change_fails(self):
        def client(value):
            value['clients']['sid-1'] = ['/dev/ttys9']
        self.changed(mutate_runtime=client)
        def generation(value):
            target = value['sessions'][0]
            target['created'] = target['generation']['created'] = 9999
        self.changed(mutate_runtime=generation)

    def test_cached_view_receipt_change_fails(self):
        def cached(value):
            value['views']['key-1'] = {'guid': 'guid-1', 'tty': '/dev/ttys1'}
        self.changed(mutate_runtime=cached)

    def test_one_changing_attempt_then_stable_passes(self):
        changed = copy.deepcopy(self.a)
        changed['windows'][0]['frame']['width'] = 1250
        provider = SequenceProvider([self.a, changed, self.a, self.a])
        reader = SequenceRuntime([self.observation] * 4)
        result = asyncio.run(wl.capture_contract(self.ws, provider, reader, attempts=2))
        self.assertEqual(provider.calls, 4)
        self.assertEqual(result.layout['schema'], wl.SCHEMA)

    def test_all_attempts_changing_fail_explicitly(self):
        changed = copy.deepcopy(self.a)
        changed['windows'][0]['frame']['width'] = 1250
        with self.assertRaises(wl.LayoutChanged) as raised:
            capture(self.ws, self.a, self.observation, attempts=3,
                    raws=[self.a, changed, self.a, changed, self.a, changed],
                    runtimes=[self.observation] * 6)
        self.assertIn('3 complete capture attempts', str(raised.exception))


class WorkspacePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = str(self.root / 'codex')
        self.store = cx_store.Store(self.root / 'state')
        self.ws = workspace(self.home)
        self.ws['saved_at'] = 1
        self.store.workspace('daily', self.ws)
        self.topology = raw(split('columns', leaf(1), leaf(2)))

    def layout(self):
        return capture(self.ws, self.topology, runtime(self.home)).layout

    def backend(self):
        outer = self
        class Backend:
            VERSION = '0.8.0-dev'
            def __init__(inner):
                inner.store = outer.store
                inner.raw_calls = 0
                inner.process_calls = 0
                inner.client_calls = 0
            def raw_snapshot(inner, *, bind_threads=True, process_table=None,
                             include_diagnostics=True):
                outer.assertFalse(bind_threads)
                outer.assertEqual(process_table, {})
                outer.assertFalse(include_diagnostics)
                inner.raw_calls += 1
                value = runtime(outer.home)
                return {'context': value['context'], 'sessions': value['sessions']}
            def processes(inner):
                inner.process_calls += 1
                return {}
            def client_tty_map(inner, records, procs):
                inner.client_calls += 1
                return {record['sid']: {f"/dev/ttys{record['session'].split('-')[-1]}"}
                        for record in records}
            @staticmethod
            def clean(value):
                return str(value)
        return Backend()

    def test_legacy_workspace_stays_valid_and_capture_adds_only_layout(self):
        before = self.store.read()['workspaces']['daily']
        self.assertNotIn('exact_layout', before)
        self.store.set_workspace_layout('daily', self.layout(), before, {}, replace=False)
        after = self.store.read()['workspaces']['daily']
        self.assertEqual({key: value for key, value in after.items() if key != 'exact_layout'}, before)
        self.assertEqual(after['exact_layout']['schema'], wl.SCHEMA)

    def test_replace_updates_only_layout_and_needs_flag(self):
        first = self.layout()
        before = self.store.read()['workspaces']['daily']
        self.store.set_workspace_layout('daily', first, before, {}, replace=False)
        current = self.store.read()['workspaces']['daily']
        second = copy.deepcopy(first)
        second['capture']['captured_at'] = '2027-01-01T00:00:00Z'
        with self.assertRaises(cx_store.StateError):
            self.store.set_workspace_layout('daily', second, current, {}, replace=False)
        self.store.set_workspace_layout('daily', second, current, {}, replace=True)
        final = self.store.read()['workspaces']['daily']
        self.assertEqual(final['members'], self.ws['members'])
        self.assertEqual(final['layout'], self.ws['layout'])
        self.assertEqual(final['exact_layout'], second)

    def test_failed_atomic_commit_preserves_previous_layout_and_other_state(self):
        original = self.layout()
        expected = self.store.read()['workspaces']['daily']
        self.store.set_workspace_layout('daily', original, expected, {}, replace=False)
        self.store.change(lambda data: (data['agents'].__setitem__('a', {'name': 'Named', 'pinned': True}),
                                        data['groups'].__setitem__('g', {'name': 'Group'})))
        stable = copy.deepcopy(self.store.read())
        replacement = copy.deepcopy(original)
        replacement['capture']['captured_at'] = '2027-01-01T00:00:00Z'
        with self.assertRaises(cx_store.StateError):
            self.store.set_workspace_layout('daily', replacement,
                                            stable['workspaces']['daily'], {'stale': {}}, replace=True)
        self.assertEqual(self.store.read(), stable)

    def test_workspace_command_captures_without_runtime_mutation(self):
        backend = self.backend()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            workbench.workspace_capture('daily', backend, provider=SequenceProvider(
                [self.topology, self.topology]))
        self.assertIn('topology: L3', output.getvalue())
        self.assertEqual((backend.raw_calls, backend.process_calls, backend.client_calls), (2, 2, 2))
        self.assertEqual(self.store.read()['workspaces']['daily']['exact_layout']['schema'], wl.SCHEMA)

    def test_failed_capture_keeps_existing_exact_layout_byte_for_byte(self):
        original = self.layout()
        expected = self.store.read()['workspaces']['daily']
        self.store.set_workspace_layout('daily', original, expected, {}, replace=False)
        stable = copy.deepcopy(self.store.read())
        changed = copy.deepcopy(self.topology)
        changed['windows'][0]['frame']['width'] += 1
        with self.assertRaises(cx_store.StateError):
            workbench.workspace_capture(
                'daily', self.backend(), replace=True, attempts=1,
                provider=SequenceProvider([self.topology, changed]))
        self.assertEqual(self.store.read(), stable)

    def test_workspace_capture_cli_routes_replace_explicitly(self):
        backend = object()
        with patch.object(workbench, 'workspace_capture', return_value={}) as operation:
            workbench.workspace_command(['capture', 'daily', '--replace'], backend)
        operation.assert_called_once_with('daily', backend, replace=True)

    def test_five_hundred_leaf_workspace_stays_well_below_store_cap(self):
        members = [{'home': self.home, 'thread_id': tid(index), 'title': f'Agent {index}',
                    'live_key': f'key-{index}', 'session': f'session-{index}'}
                   for index in range(1, 501)]
        record = {'host': HOST, 'members': members, 'saved_at': 1,
                  'layout': {'per_tab': 0, 'min_columns': 70, 'min_rows': 12}}
        store = cx_store.Store(self.root / 'large-state')
        store.workspace('large', record)
        exact = {
            'schema': wl.SCHEMA,
            'capture': {'provider': wl.PROVIDER, 'provider_version': '2.23',
                        'fidelity': wl.FIDELITY, 'captured_at': '2026-01-02T03:04:05Z'},
            'windows': [{'tabs': [{'root': {'type': 'split', 'axis': 'columns',
                'children': [{'type': 'conversation', 'identity': {
                    'host': HOST, 'codex_home': self.home, 'thread_id': tid(index)}}
                    for index in range(1, 501)]}}]}],
        }
        before = store.read()['workspaces']['large']
        store.set_workspace_layout('large', exact, before, {}, replace=False)
        self.assertLess(store.path.stat().st_size, 512 * 1024)
        self.assertEqual(len(wl.conversation_identities(
            store.read()['workspaces']['large']['exact_layout'])), 500)


if __name__ == '__main__':
    unittest.main()
