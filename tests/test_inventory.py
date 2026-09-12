"""Pure T3 inventory, view-health, search, ordering, and navigation tests."""
import copy
import contextlib
import io
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import cx_inventory as inv
from cx_store import Store, live_key, thread_key
import workbench


HOST = socket.gethostname()
HOME = os.path.realpath('/tmp/cxdeck-t3-codex')
CTX = {'backend': 'zmx', 'host': HOST, 'runtime_dir': '/tmp/cxdeck-t3-zmx',
       'runtime_version': '0.8.1'}
IDS = [f'00000000-0000-4000-8000-{index:012d}' for index in range(1, 10)]


def managed(index=1, *, tid=None, attached=1, state='ALIVE'):
    sid = f'cx-t3-{index}'
    row = {'backend': 'zmx', 'session': sid, 'sid': sid, 'daemon_pid': 1000 + index,
           'created': 2000 + index, 'codex_home': HOME, 'thread_id': tid,
           'task': f'Project {index}', 'launch_cwd': f'/work/project-{index}',
           'root': '', 'codex_pids': [3000 + index], 'state': state,
           'attached': attached, 'launch_policy': 'yolo', 'launch_mode': 'resume',
           'runtime_version': '0.8.1', 'cx_version': '0.7.0', 'labels': {},
           'generation': {'host': HOST, 'runtime_dir': CTX['runtime_dir'],
                          'session': sid, 'daemon_pid': 1000 + index,
                          'created': 2000 + index}}
    return row


def history(index=1, *, updated=10):
    return {'key': IDS[index - 1], 'thread_id': IDS[index - 1],
            'title': f'Project {index}', 'cwd': f'/work/project-{index}',
            'updated': updated, 'home': HOME, 'managed': None,
            'state': 'SAVED', 'external_pids': []}


def joined(index=1, *, saved=True, external=(), attached=1, state='ALIVE', bound=True,
           updated=10):
    row = history(index, updated=updated)
    if not saved:
        row['updated'] = 2000 + index
    row['managed'] = managed(index, tid=IDS[index - 1] if bound else None,
                             attached=attached, state=state)
    row['state'] = state
    row['external_pids'] = list(external)
    if not bound:
        row.update(key='zmx:' + row['managed']['session'], thread_id=None)
    return row


def compose(rows, *, saved=(), clients=None, views=None, state=None, view_error=None,
            client_error=None, unknown=(), process_table=None):
    clients = clients if clients is not None else {
        row['managed']['sid']: ([f'/dev/ttys{index + 1}'] if row.get('managed') and
                                row['managed'].get('attached') else [])
        for index, row in enumerate(rows) if row.get('managed')}
    views = views if views is not None else [
        {'guid': f'guid-{index + 1}', 'tty': f'/dev/ttys{index + 1}'}
        for index, row in enumerate(rows) if row.get('managed') and row['managed'].get('attached')]
    if process_table is None:
        process_table = {pid: {'stat': 'S'} for row in rows for pid in row.get('external_pids', [])}
    return inv.compose(list(saved), rows, CTX, state or
                       {'agents': {}, 'views': {}, 'groups': {}, 'workspaces': {}}, clients,
                       iterm_views=views, view_error=view_error, client_error=client_error,
                       unknown_pids=unknown, timestamp=123, process_table=process_table)


class InventoryCompositionTests(unittest.TestCase):
    def test_saved_only_dimensions_are_separate(self):
        snap = compose([history()], saved=[history()])
        row = snap['conversations'][0]
        self.assertEqual((row['conversation_state'], row['runtime_state'], row['view_state']),
                         ('SAVED', 'ABSENT', 'NOT_APPLICABLE'))

    def test_live_managed_and_saved_live_states(self):
        live = compose([joined(saved=False)])['conversations'][0]
        both = compose([joined()], saved=[history()])['conversations'][0]
        self.assertEqual(live['conversation_state'], 'LIVE_MANAGED')
        self.assertEqual(both['conversation_state'], 'SAVED_AND_LIVE_MANAGED')
        self.assertEqual(both['runtime_state'], 'ALIVE')
        self.assertEqual(both['view_state'], 'VERIFIED_VIEW')

    def test_external_only_and_saved_external(self):
        external = history()
        external.update(updated=0, state='LIVE-OUTSIDE', external_pids=[77])
        one = compose([external])['conversations'][0]
        two = compose([external], saved=[history()])['conversations'][0]
        self.assertEqual(one['conversation_state'], 'LIVE_EXTERNAL')
        self.assertEqual(two['conversation_state'], 'SAVED_AND_LIVE_EXTERNAL')
        self.assertEqual(one['runtime_state'], 'ALIVE')
        self.assertEqual(one['runtime_source'], 'external')
        self.assertEqual(one['external'], {'pids': [77], 'state': 'ALIVE'})

    def test_stopped_external_process_is_reported_as_process_fact(self):
        external = history()
        external.update(updated=0, state='LIVE-OUTSIDE', external_pids=[77])
        row = inv.compose([], [external], CTX,
                          {'agents': {}, 'views': {}, 'groups': {}, 'workspaces': {}}, {},
                          iterm_views=[], process_table={77: {'stat': 'T'}})['conversations'][0]
        self.assertEqual(row['runtime_state'], 'STOPPED')

    def test_managed_external_conflict_is_one_record(self):
        row = compose([joined(external=[71])], saved=[history()])['conversations'][0]
        self.assertEqual(row['conversation_state'], 'LIVE_MANAGED_EXTERNAL_CONFLICT')
        self.assertEqual(row['runtime_source'], 'managed')
        self.assertEqual(row['external'], {'pids': [71], 'state': 'ALIVE'})

    def test_managed_state_remains_top_level_when_external_copy_exists(self):
        row = compose([joined(external=[71], state='NO_CODEX')],
                      saved=[history()])['conversations'][0]
        self.assertEqual(row['conversation_state'], 'LIVE_MANAGED_EXTERNAL_CONFLICT')
        self.assertEqual(row['runtime_state'], 'NO_CODEX')
        self.assertEqual(row['runtime_source'], 'managed')
        self.assertEqual(row['runtime']['managed_state'], 'NO_CODEX')
        self.assertEqual(row['external']['state'], 'ALIVE')

    def test_external_state_is_unknown_without_a_process_snapshot(self):
        # The helper normally supplies a table; call the composer directly to
        # prove missing process evidence is not promoted to ALIVE.
        direct = inv.compose([], [history() | {'external_pids': [71]}], CTX,
                             {'agents': {}, 'views': {}, 'groups': {}, 'workspaces': {}}, {},
                             iterm_views=[], process_table=None)['conversations'][0]
        self.assertEqual(direct['runtime_state'], 'UNKNOWN')
        self.assertEqual(direct['external']['state'], 'UNKNOWN')

    def test_absent_runtime_has_explicit_source_and_external_state(self):
        row = compose([history()], saved=[history()])['conversations'][0]
        self.assertEqual((row['runtime_source'], row['external']['state']), ('none', 'ABSENT'))

    def test_managed_generation_and_process_health_are_independent(self):
        no_codex = compose([joined(state='NO_CODEX')], saved=[history()])['conversations'][0]
        unknown = compose([joined(state='UNKNOWN')], saved=[history()],
                          clients={'cx-t3-1': None},
                          client_error=RuntimeError('process inventory unavailable'))['conversations'][0]
        self.assertEqual(no_codex['conversation_state'], 'SAVED_AND_LIVE_MANAGED')
        self.assertEqual(no_codex['runtime_state'], 'NO_CODEX')
        self.assertEqual(no_codex['view_state'], 'NOT_APPLICABLE')
        self.assertEqual(unknown['conversation_state'], 'SAVED_AND_LIVE_MANAGED')
        self.assertEqual(unknown['runtime_state'], 'UNKNOWN')
        self.assertEqual(unknown['view_state'], 'VIEW_UNKNOWN')

    def test_unbound_runtime_never_gets_fake_identity(self):
        row = compose([joined(saved=False, bound=False)])['conversations'][0]
        self.assertEqual(row['conversation_state'], 'LIVE_ONLY_UNBOUND')
        self.assertIsNone(row['identity'])
        self.assertTrue(row['key'].startswith('zmx:'))

    def test_duplicate_managed_generation_fails_closed(self):
        with self.assertRaisesRegex(inv.InventoryError, 'Two managed'):
            compose([joined(1), joined(2, saved=False) | {
                'thread_id': IDS[0], 'home': HOME,
                'managed': managed(2, tid=IDS[0])}], saved=[history()])

    def test_group_name_workspace_membership_and_custom_name(self):
        row = joined()
        key = thread_key(HOME, IDS[0], HOST)
        state = {'agents': {key: {'name': 'Custom', 'pinned': True, 'group_id': 'g'}},
                 'views': {}, 'groups': {'g': {'name': 'Studies', 'order': 0}},
                 'workspaces': {'Daily': {'host': HOST, 'members': [
                     {'home': HOME, 'thread_id': IDS[0], 'live_key': 'old'}]}}}
        result = compose([row], saved=[history()], state=state)['conversations'][0]
        self.assertEqual(result['display'], {'name': 'Custom', 'pinned': True,
                                             'group_id': 'g', 'group': 'Studies',
                                             'workspaces': ['Daily']})

    def test_malformed_workspace_members_fail_with_inventory_error(self):
        state = {'agents': {}, 'views': {}, 'groups': {},
                 'workspaces': {'Broken': {'host': HOST, 'members': None}}}
        with self.assertRaisesRegex(inv.InventoryError, 'Invalid workspace metadata'):
            compose([history()], saved=[history()], state=state)

    def test_recency_is_only_saved_history_timestamp(self):
        saved = compose([joined(updated=91)], saved=[history(updated=91)])['conversations'][0]
        live = compose([joined(saved=False, updated=999)])['conversations'][0]
        self.assertEqual((saved['recent_at'], saved['recent_source']),
                         (91.0, 'codex_history_updated'))
        self.assertEqual((live['recent_at'], live['recent_source']), (None, None))

    def test_unidentified_pids_stay_at_snapshot_scope(self):
        snap = compose([history()], saved=[history()], unknown=[88, 89])
        self.assertEqual(snap['unidentified_pids'], [88, 89])
        self.assertNotIn('unidentified_pids', snap['conversations'][0])


class ViewHealthTests(unittest.TestCase):
    def row(self, **kwargs):
        return compose([joined(**kwargs)], saved=[history()])['conversations'][0]

    def test_no_client_is_no_view(self):
        self.assertEqual(self.row(attached=0)['view_state'], 'NO_VIEW')

    def test_multiple_clients(self):
        row = joined(attached=2)
        snap = compose([row], saved=[history()],
                       clients={row['managed']['sid']: ['/dev/a', '/dev/b']},
                       views=[{'guid': 'a', 'tty': '/dev/a'}, {'guid': 'b', 'tty': '/dev/b'}])
        self.assertEqual(snap['conversations'][0]['view_state'], 'MULTIPLE_CLIENTS')

    def test_client_missing_from_iterm_is_unverified(self):
        row = joined()
        result = compose([row], saved=[history()],
                         clients={row['managed']['sid']: ['/dev/ttys1']}, views=[])
        self.assertEqual(result['conversations'][0]['view_state'], 'UNVERIFIED_CLIENT')

    def test_attached_count_disagrees_with_exact_client(self):
        row = joined(attached=0)
        result = compose([row], saved=[history()],
                         clients={row['managed']['sid']: ['/dev/ttys1']},
                         views=[{'guid': 'g', 'tty': '/dev/ttys1'}])
        self.assertEqual(result['conversations'][0]['view_state'], 'UNVERIFIED_CLIENT')

    def test_stale_receipt_does_not_hide_verified_current_view(self):
        row = joined()
        key = live_key(row['managed'], CTX)
        state = {'agents': {}, 'views': {key: {'guid': 'old', 'tty': '/dev/old'}},
                 'groups': {}, 'workspaces': {}}
        result = compose([row], saved=[history()], state=state)['conversations'][0]
        self.assertEqual(result['view_state'], 'STALE_RECEIPT')
        self.assertTrue(result['view']['verified'])
        self.assertEqual(result['view']['guid'], 'guid-1')

    def test_current_receipt_is_verified(self):
        row = joined()
        key = live_key(row['managed'], CTX)
        state = {'agents': {}, 'views': {key: {'guid': 'guid-1', 'tty': '/dev/ttys1'}},
                 'groups': {}, 'workspaces': {}}
        result = compose([row], saved=[history()], state=state)['conversations'][0]
        self.assertEqual(result['view_state'], 'VERIFIED_VIEW')
        self.assertEqual(result['view']['receipt_state'], 'CURRENT')

    def test_prior_generation_receipt_is_changed_generation_without_client(self):
        row = joined(attached=0)
        state = {'agents': {}, 'views': {'old': {'guid': 'old', 'tty': '/dev/old'}},
                 'groups': {}, 'workspaces': {'Daily': {'host': HOST, 'members': [
                     {'home': HOME, 'thread_id': IDS[0], 'live_key': 'old'}]}}}
        result = compose([row], saved=[history()], state=state)['conversations'][0]
        self.assertEqual(result['view_state'], 'CHANGED_GENERATION')

    def test_provider_unavailable_preserves_runtime_health(self):
        row = joined()
        result = compose([row], saved=[history()], views=None,
                         view_error=RuntimeError('iTerm unavailable'))['conversations'][0]
        self.assertEqual(result['runtime_state'], 'ALIVE')
        self.assertEqual(result['view_state'], 'VIEW_UNKNOWN')

    def test_generation_disagreement_is_changed_generation(self):
        row = joined()
        row['managed']['generation']['created'] += 1
        result = compose([row], saved=[history()])['conversations'][0]
        self.assertEqual(result['view_state'], 'CHANGED_GENERATION')

    def test_process_client_provider_unavailable(self):
        row = joined()
        snapshot = compose([row], saved=[history()], clients={row['managed']['sid']: None},
                           client_error=RuntimeError('ps unavailable'))
        self.assertEqual(snapshot['conversations'][0]['view_state'], 'VIEW_UNKNOWN')
        self.assertFalse(snapshot['view_provider']['available'])
        self.assertFalse(snapshot['view_provider']['client_verification_available'])
        self.assertTrue(snapshot['view_provider']['iterm_available'])


class SearchAndOrderingTests(unittest.TestCase):
    def records(self):
        rows = [joined(1, updated=10), joined(2, updated=20), joined(3, updated=20)]
        state = {'agents': {thread_key(HOME, IDS[2], HOST): {'name': 'Family Archive', 'pinned': True},
                            thread_key(HOME, IDS[1], HOST): {'group_id': 'g'}},
                 'views': {}, 'groups': {'g': {'name': 'Model Studies'}}, 'workspaces': {}}
        return compose(rows, saved=[history(1, updated=10), history(2, updated=20),
                                    history(3, updated=20)], state=state)['conversations']

    def test_pinned_then_recent_then_stable_name(self):
        records = self.records()
        self.assertEqual([r['identity']['thread_id'] for r in records], [IDS[2], IDS[1], IDS[0]])

    def test_name_group_cwd_exact_uuid_and_prefix(self):
        records = self.records()
        self.assertEqual(len(inv.search(records, 'archive')), 1)
        self.assertEqual(len(inv.search(records, 'model studies')), 1)
        self.assertEqual(len(inv.search(records, 'PROJECT-1')), 1)
        self.assertEqual(inv.search(records, IDS[1])[0]['identity']['thread_id'], IDS[1])
        # Prefixes are discovery-only; an ambiguous prefix returns every match.
        self.assertEqual(len(inv.search(records, IDS[1][:12])), 3)

    def test_duplicate_names_are_all_returned_and_no_match_is_empty(self):
        records = self.records()
        for record in records[:2]:
            record['display']['name'] = 'Same'
        self.assertEqual(len(inv.search(records, 'same')), 2)
        self.assertEqual(inv.search(records, 'not present'), [])

    def test_control_char_and_empty_queries_are_rejected(self):
        for query in ('', '   ', 'bad\nquery', '\x1b'):
            with self.subTest(query=query), self.assertRaises(inv.InventoryError):
                inv.search(self.records(), query)

    def test_public_json_removes_operation_reference(self):
        public = inv.public(compose([joined()], saved=[history()]))
        self.assertNotIn('_managed', public['conversations'][0])


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.gui = Mock()
        self.backend = Mock()
        self.backend.clean.side_effect = str
        self.records = compose([joined(1), joined(2), joined(3)],
                               saved=[history(1), history(2), history(3)])

    def navigate(self, direction, caller, snapshots=None):
        values = snapshots or [self.records, copy.deepcopy(self.records)]
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', side_effect=values):
            return workbench.focus_navigation(direction, self.backend, gui=self.gui,
                                               caller_tty=caller)

    def test_next_previous_wrap_and_outside_start(self):
        order = [row['view']['tty'] for row in self.records['conversations']]
        self.navigate('next', order[0])
        self.assertEqual(self.gui.focus.call_args.args[0]['tty'], order[1])
        self.gui.reset_mock()
        self.navigate('next', order[-1])
        self.assertEqual(self.gui.focus.call_args.args[0]['tty'], order[0])
        self.gui.reset_mock()
        self.navigate('previous', order[0])
        self.assertEqual(self.gui.focus.call_args.args[0]['tty'], order[-1])
        self.gui.reset_mock()
        self.navigate('next', '/dev/not-cx')
        self.assertEqual(self.gui.focus.call_args.args[0]['tty'], order[0])

    def test_only_one_view_focuses_it_and_zero_views_fail(self):
        single = compose([joined()], saved=[history()])
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', side_effect=[single, copy.deepcopy(single)]):
            workbench.focus_navigation('next', self.backend, gui=self.gui, caller_tty='/dev/ttys1')
        empty = compose([joined(attached=0)], saved=[history()])
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', return_value=empty), \
             self.assertRaisesRegex(Exception, 'No uniquely verified'):
            workbench.focus_navigation('next', self.backend, gui=self.gui, caller_tty='/dev/no')

    def test_ambiguous_clients_excluded_and_stale_verified_included(self):
        first = copy.deepcopy(self.records)
        first['conversations'][0]['view_state'] = 'MULTIPLE_CLIENTS'
        first['conversations'][0]['view']['verified'] = False
        first['conversations'][1]['view_state'] = 'STALE_RECEIPT'
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', side_effect=[first, copy.deepcopy(first)]):
            chosen = workbench.focus_navigation('next', self.backend, gui=self.gui,
                                                caller_tty='/dev/outside')
        self.assertEqual(chosen['key'], first['conversations'][1]['key'])

    def test_race_fails_before_focus(self):
        changed = copy.deepcopy(self.records)
        changed['conversations'][0]['view']['guid'] = 'replacement'
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', side_effect=[self.records, changed]), \
             self.assertRaisesRegex(Exception, 'changed during navigation'):
            workbench.focus_navigation('next', self.backend, gui=self.gui,
                                       caller_tty='/dev/outside')
        self.gui.focus.assert_not_called()

    def test_generation_race_fails_even_when_guid_and_tty_are_unchanged(self):
        changed = copy.deepcopy(self.records)
        changed['conversations'][0]['runtime']['generation']['created'] += 1
        with patch.object(workbench, '_read_history', return_value=([], False, None)), \
             patch.object(workbench, 'inventory_snapshot', side_effect=[self.records, changed]), \
             self.assertRaisesRegex(Exception, 'changed during navigation'):
            workbench.focus_navigation('next', self.backend, gui=self.gui,
                                       caller_tty='/dev/outside')
        self.gui.focus.assert_not_called()


if __name__ == '__main__':
    unittest.main()
