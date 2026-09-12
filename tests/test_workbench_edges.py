"""Regression tests for UI command routing, native arguments and cold identity."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import console_entry as entry
import workbench as w
import cx_iterm as ui
from cx_store import Store
try:
    from .test_workbench import record, conversation, CTX, HOME, ID
except ImportError:
    from test_workbench import record, conversation, CTX, HOME, ID


class WorkbenchEdgeTests(unittest.TestCase):
    def backend(self):
        b = Mock()
        b.console.by_label.return_value = None
        b.snapshot.return_value = dict(sessions=[record(i) for i in range(1, 4)], context=CTX, warnings=[])
        b.console.start_agent.side_effect = [record(i) for i in range(1, 4)]
        return b

    def test_unnamed_gui_launch_forwards_flags_after_separator(self):
        b = self.backend()
        with patch.object(ui, 'ITerm'), patch.object(w, 'focus_rows'):
            w.new_agents(['--split', '--', '--profile', 'research', '--model', 'literal'], b)
        b.console.start_agent.assert_called_once_with(['--profile', 'research', '--model', 'literal'], None, detached=True)

    def test_batch_launch_creates_arbitrary_count_from_same_directory(self):
        b = self.backend()
        with patch.object(ui, 'ITerm'), patch.object(w, 'focus_rows') as show:
            w.new_agents(['--count', '3'], b)
        self.assertEqual(b.console.start_agent.call_count, 3)
        self.assertEqual(len(show.call_args.args[0]), 3)
        for call in b.console.start_agent.call_args_list:
            self.assertEqual(call.args, ([], None))

    def test_native_prompt_tokens_do_not_enable_gui_routing(self):
        c = Mock()
        c.main.return_value = 0
        with patch.object(w, 'new_agents') as new:
            entry.main(['new', '--', 'literal prompt', '--split'], c)
        new.assert_not_called()
        c.main.assert_called_once_with(['new', '--', 'literal prompt', '--split'])

    def test_plain_new_retains_original_current_pane_routing(self):
        c = Mock()
        c.main.return_value = 0
        entry.main(['new'], c)
        c.main.assert_called_once_with(['new'])

    def test_views_rebuild_routes_to_presentation_only_command(self):
        c = Mock()
        with patch.object(w, 'views_command', return_value=0) as rebuild:
            self.assertEqual(entry.main(['views', 'rebuild'], c), 0)
        self.assertEqual(rebuild.call_args.args[0], ['rebuild'])
        c.main.assert_not_called()

    def test_config_timestamps_routes_without_agent_operation(self):
        c = Mock()
        with patch.object(w, 'config_command', return_value=0) as configure:
            self.assertEqual(entry.main(['config', 'timestamps', 'off'], c), 0)
        self.assertEqual(configure.call_args.args[0], ['timestamps', 'off'])
        c.main.assert_not_called()

    def test_views_rebuild_batches_only_verified_live_sessions(self):
        b = self.backend()
        b.snapshot.return_value['sessions'][1]['state'] = 'NO_CODEX'
        items = [dict(managed=item, title=item['display_name'])
                 for item in b.snapshot.return_value['sessions']]
        with patch.object(w, 'catalog', return_value=(items, [], [], False)), \
                patch.object(w, 'focus_rows', return_value={'opened': 2, 'reused': 0}) as show, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(w.views_command(['rebuild'], b), 0)
        self.assertEqual(len(show.call_args.args[0]), 2)
        show.assert_called_once()

    def test_upgrade_routes_to_version_compatibility_module(self):
        c = Mock()
        with patch('cx_upgrade.main', return_value=0) as run:
            self.assertEqual(entry.main(['upgrade', 'status', '--json'], c), 0)
        self.assertEqual(run.call_args.args[0], ['status', '--json'])
        c.main.assert_not_called()

    def test_workspace_known_uuid_not_duplicated_when_live_evidence_temporarily_missing(self):
        row = record()
        stored = dict(host=CTX['host'], saved_at=1, members=[dict(live_key=row['_key'],
            home=HOME, thread_id=ID, title='Theory', cwd='/')])
        live = conversation(row, tid=None, key='zmx:' + row['session'])
        saved = conversation()
        r = Mock()
        r.codex_home.return_value = HOME
        r.thread_id.side_effect = str
        with patch.object(w, 'native', return_value=r), patch.object(w, 'catalog', return_value=([saved, live], [], [], False)):
            rows, _, missing = w.workspace_plan(stored, Mock())
        self.assertEqual(rows, [live])
        self.assertEqual(missing, [])

    def test_workspace_member_order_is_preserved(self):
        one, two = record(1), record(2)
        stored = dict(host=CTX['host'], members=[dict(live_key=r['_key'],home=HOME,title=r['task'],thread_id=None) for r in (two,one)])
        available = [conversation(r,tid=None,key='zmx:'+r['session']) for r in (one,two)]
        native = Mock()
        native.codex_home.return_value = HOME
        with patch.object(w, 'native', return_value=native), patch.object(w, 'catalog', return_value=(available, [], [], False)):
            rows, _, _ = w.workspace_plan(stored, Mock())
        self.assertEqual([r['managed']['sid'] for r in rows], ['cx-agent-2', 'cx-agent-1'])

    def test_terminal_snapshot_does_not_create_private_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'state')
            b = Mock()
            data = dict(sessions=[], warnings=[], context=CTX)
            result = w.enrich(data, b, store)
            self.assertFalse(store.root.exists())
            self.assertEqual(result['sessions'], [])

    def test_gui_permission_failure_never_opens_a_view(self):
        b = self.backend()
        with tempfile.TemporaryDirectory() as temp:
            gui = Mock()
            gui.preflight.side_effect = RuntimeError('permission denied')
            with self.assertRaises(RuntimeError):
                ui.show([record()], b, Store(Path(temp) / 'state'), gui=gui)
            gui.open.assert_not_called()
            b.snapshot.assert_not_called()
