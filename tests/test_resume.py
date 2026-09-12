"""Resume metadata, selection, quoting and safety tests; no Codex API calls."""
import argparse
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import codex_resume as r
import cx_zmx as cx
import console_entry as entry

ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"
TEST_HOME = os.path.realpath("/tmp/cx-home")


def row(tid=ID1, cwd="/", managed=None):
    return dict(key=tid, thread_id=tid, title="审阅 / same home", cwd=cwd, home=TEST_HOME,
                updated=10, managed=managed, state="SAVED", external_pids=[])


def args(**kw):
    result = dict(limit=200, json=False, list=False, select=[ID1], cwd=None, no_iterm=True,
                  no_dashboard=True, per_tab=0, min_columns=70, min_rows=12,
                  allow_unverified_live=False, yolo=True)
    result.update(kw)
    return SimpleNamespace(**result)


class IdentityAndLayoutTests(unittest.TestCase):
    def test_home_not_repo_identity(self):
        self.assertNotEqual(r.chat_name("/home/me/.codex", ID1), r.chat_name("/home/me/.codex", ID2))
        self.assertNotEqual(r.chat_name("/one", ID1), r.chat_name("/two", ID1))
        self.assertEqual(r.chat_name("/a/../b", ID1), r.chat_name("/b", ID1))

    def test_reject_non_uuid(self):
        for x in (None, "--last", "review", "$(touch /tmp/x)", "", "../secret"):
            with self.assertRaises(r.ResumeError):
                r.thread_id(x)

    def test_label_and_unicode_clip(self):
        self.assertEqual(r.label("A\x1b[31m;#{B}"), "A [31m,{B}")
        self.assertEqual(r.clip("研究abc", 5), "研究a")
        self.assertNotIn("\x1b", r.clip("bad\x1b", 100))

    def test_resume_policy_flags_are_mutually_exclusive_and_default_yolo(self):
        parser = argparse.ArgumentParser()
        r.add_arguments(parser.add_subparsers(dest="command"))
        default = parser.parse_args(["resume"])
        explicit = parser.parse_args(["resume", "--yolo"])
        self.assertEqual(vars(default), vars(explicit))
        self.assertTrue(default.yolo)
        self.assertFalse(parser.parse_args(["resume", "--safe"]).yolo)
        self.assertFalse(parser.parse_args(["resume", "--no-yolo"]).yolo)
        for pair in (("--yolo", "--safe"), ("--yolo", "--no-yolo"),
                     ("--safe", "--no-yolo")):
            with self.subTest(pair=pair), patch("sys.stderr", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit):
                parser.parse_args(["resume", *pair])

    def test_missing_cwd_needs_explicit_choice(self):
        for cwd in (None, "relative/path", "/does-not-exist-cx-unique"):
            with self.assertRaises(r.ResumeError):
                r.planned_cwd(row(cwd=cwd))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(r.planned_cwd(row(cwd=None), d), str(Path(d).resolve()))

    def test_new_default_delegates_without_repo_argument(self):
        console = Mock()
        console.main.return_value = 0
        self.assertEqual(entry.main([], console), 0)
        console.main.assert_called_once_with([])

    def test_resume_registered_not_treated_as_task(self):
        console = Mock()
        backend = Mock()
        with patch.object(entry, 'ResumeBackend', return_value=backend), \
             patch('workbench.resume') as execute:
            self.assertEqual(entry.main(["resume", "--list"], console), 0)
        self.assertTrue(execute.call_args.args[0].list)
        self.assertIs(execute.call_args.args[1], backend)
        console.main.assert_not_called()

    def test_resume_policy_flags_use_custom_resume_routing(self):
        for flag, expected in (("--yolo", True), ("--safe", False), ("--no-yolo", False)):
            console = Mock()
            backend = Mock()
            with self.subTest(flag=flag), patch.object(entry, 'ResumeBackend', return_value=backend), \
                 patch('workbench.resume') as execute:
                self.assertEqual(entry.main(["resume", flag, "--list"], console), 0)
                self.assertIs(execute.call_args.args[0].yolo, expected)
                console.main.assert_not_called()

    def test_native_resume_flags_preserve_previous_routing(self):
        console = Mock()
        console.main.return_value = 0
        for argv in (["resume", "--all"], ["resume", ID1], ["run", "--", "resume", ID2]):
            entry.main(argv, console)
            console.main.assert_called_with(argv)


class HistoryTests(unittest.TestCase):
    def test_pagination_global_and_no_turn_loading(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.request.side_effect = [
            {"data": [{"id": ID1, "name": "Named", "cwd": "/", "updatedAt": 1}], "nextCursor": "next"},
            {"data": [{"id": ID2, "preview": "Fallback preview", "cwd": "/", "updatedAt": 2}], "nextCursor": None}]
        with patch.object(r.shutil, "which", return_value="/fake/codex"):
            rows, truncated = r.list_history("/tmp/home", 200, lambda *a: client)
        self.assertFalse(truncated)
        self.assertEqual([x["key"] for x in rows], [ID2, ID1])
        for call in client.request.call_args_list:
            self.assertEqual(call.args[0], "thread/list")
            self.assertNotIn("cwd", call.args[1])
            self.assertEqual(call.args[1]["sourceKinds"], ["cli"])
        self.assertEqual(client.request.call_args_list[1].args[1]["cursor"], "next")

    def test_cap_explicit_not_claim_all_history(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.request.return_value = {"data": [{"id": ID1}], "nextCursor": "older"}
        with patch.object(r.shutil, "which", return_value="/fake"):
            rows, capped = r.list_history("/tmp", 1, lambda *a: client)
        self.assertTrue(capped)
        self.assertEqual(len(rows), 1)

    def test_repeated_cursor_fails_closed(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.request.return_value = {"data": [{"id": ID1}], "nextCursor": "same"}
        with patch.object(r.shutil, "which", return_value="/fake"), self.assertRaises(r.ResumeError):
            r.list_history("/tmp", 200, lambda *a: client)

    def test_rpc_allowlist(self):
        client = r.HistoryClient("/fake", "/tmp")
        for method in ("thread/start", "thread/resume", "thread/read", "turn/start", "thread/archive"):
            with self.assertRaises(r.ResumeError):
                client.request(method, {})

    def test_real_stdio_handshake_with_fake_server(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "codex"
            log = Path(d) / "requests"
            script.write_text("#!" + sys.executable + "\n" + '''import sys,json,os
for line in sys.stdin:
    m=json.loads(line)
    with open(os.path.join(os.environ['CODEX_HOME'],'requests'),'a') as f: f.write(m['method']+'\\n')
    if 'id' in m:
        result = {'data': [], 'nextCursor': None} if m['method']=='thread/list' else {}
        print(json.dumps({'id': m['id'], 'result': result}),flush=True)
''')
            script.chmod(0o700)
            with patch.object(r.shutil, "which", return_value=str(script)):
                rows, capped = r.list_history(d)
            self.assertEqual(rows, [])
            self.assertFalse(capped)
            self.assertEqual(log.read_text().splitlines(), ["initialize", "initialized", "thread/list"])

    def test_timeout_kills_only_owned_helper(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "codex"
            script.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(15)\n")
            script.chmod(0o700)
            client = r.HistoryClient(str(script), d, timeout=0.1)
            with self.assertRaises(r.ResumeError):
                with client:
                    pass
            self.assertIsNotNone(client.process.poll())


class PickerTests(unittest.TestCase):
    def picker(self, keys):
        fake = Mock()
        fake.getmaxyx.return_value = (30, 160)
        fake.get_wch.side_effect = keys
        with patch.object(r.sys.stdin, "isatty", return_value=True), patch.object(r.sys.stdout, "isatty", return_value=True), patch.object(r.curses, "wrapper", side_effect=lambda f: f(fake)):
            return r.choose([row(), row(ID2)])

    def test_empty_enter_is_noop(self):
        self.assertEqual(self.picker(["\n"]), [])

    def test_multiselect(self):
        self.assertEqual([x["key"] for x in self.picker([" ", "j", " ", "\n"])], [ID1, ID2])

    def test_toggle_and_cancel(self):
        self.assertEqual(self.picker([" ", " ", "\n"]), [])
        self.assertEqual(self.picker(["a", "q"]), [])

    def test_filter_by_uuid_not_repo(self):
        chosen = self.picker(["/", "2", "2", "2", "2", "\n", " ", "\n"])
        self.assertEqual([x["key"] for x in chosen], [ID2])


class ColdLaunchPolicyTests(unittest.TestCase):
    def launch(self, yolo=True):
        backend = Mock()
        backend.Error = RuntimeError
        backend.VERSION = '0.6.0'
        backend.preflight.return_value = {"path": "/test/bin/zmx"}
        backend.sessions.return_value = []
        backend.encode_path.return_value = 'encoded-home'
        backend.store = None
        backend.create.return_value = {'session': 'managed', 'sid': 'managed', 'state': 'ALIVE',
            'generation': {'host': 'test', 'runtime_dir': '/tmp/zmx', 'session': 'managed',
                           'daemon_pid': 42, 'created': 7}}
        with patch.object(r.shutil, 'which', side_effect=lambda tool: '/test/bin/' + tool):
            r.ensure(row(), backend, yolo=yolo)
        return backend.create.call_args.args[1], backend

    def test_default_cold_resume_is_exact_yolo_argv_and_metadata(self):
        native, backend = self.launch()
        self.assertEqual(native, ['/test/bin/codex', '--yolo', 'resume', ID1, '--cd', '/'])
        self.assertEqual(backend.create.call_args.args[3]['cx_launch_policy'], 'yolo')

    def test_safe_cold_resume_omits_yolo_and_records_metadata(self):
        native, backend = self.launch(False)
        self.assertEqual(native, ['/test/bin/codex', 'resume', ID1, '--cd', '/'])
        self.assertEqual(backend.create.call_args.args[3]['cx_launch_policy'], 'safe')

    def test_cold_resume_uses_only_zmx_create(self):
        for yolo in (True, False):
            with self.subTest(yolo=yolo):
                _, backend = self.launch(yolo)
                backend.create.assert_called_once()


class InventoryAndSafetyTests(unittest.TestCase):
    def data(self, sessions=(), outside=()):
        return dict(sessions=list(sessions), outside_managed_codex_pids=list(outside), warnings=[])

    def test_live_metadata_merges_saved_row(self):
        s = dict(session="cx-chat-test", sid="$1", thread_id=ID1, codex_home=TEST_HOME, task="Task", root="/", created=1, codex_pids=[7], state="ALIVE")
        with patch.object(cx, "snapshot", return_value=self.data([s])), patch.object(r, "open_thread_files", return_value={7: set()}):
            rows, unknown, _, _ = r.inventory([row()], TEST_HOME, cx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(unknown, [])
        self.assertEqual(rows[0]["managed"]["sid"], "$1")

    def test_external_live_thread_is_marked(self):
        with patch.object(cx, "snapshot", return_value=self.data(outside=[7])), patch.object(r, "open_thread_files", return_value={7: {ID1}}):
            rows, unknown, _, _ = r.inventory([row()], TEST_HOME, cx)
        self.assertEqual(rows[0]["state"], "LIVE-OUTSIDE")
        self.assertEqual(rows[0]["external_pids"], [7])
        self.assertEqual(unknown, [])

    def test_known_external_without_saved_history_is_an_exact_record(self):
        with patch.object(cx, "snapshot", return_value=self.data(outside=[7])), \
             patch.object(r, "open_thread_files", return_value={7: {ID1}}):
            rows, unknown, _, _ = r.inventory([], TEST_HOME, cx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["thread_id"], ID1)
        self.assertEqual(rows[0]["state"], "LIVE-OUTSIDE")
        self.assertEqual(rows[0]["external_pids"], [7])
        self.assertEqual(unknown, [])

    def test_ambiguous_managed_identity_fails_closed(self):
        s = dict(session="cx-chat-test", sid="$1", thread_id=ID1, codex_home=TEST_HOME, task="Task", root="/", created=1, codex_pids=[7], state="ALIVE")
        with patch.object(cx, "snapshot", return_value=self.data([s, dict(s, sid="$2")])), patch.object(r, "open_thread_files", return_value={7: set()}), self.assertRaises(r.ResumeError):
            r.inventory([row()], TEST_HOME, cx)

    def test_alive_attach_never_starts_or_changes_cwd(self):
        s = {"state": "ALIVE", "sid": "$7"}
        with patch.object(cx, "create") as create:
            result, action = r.ensure(row(cwd="/missing", managed=s), cx)
        self.assertIs(result, s)
        self.assertEqual(action, "reuse live zmx session")
        create.assert_not_called()

    def test_unknown_liveness_never_restarts(self):
        with patch.object(cx, "create") as create, self.assertRaises(r.ResumeError):
            r.ensure(row(managed={"state": "UNKNOWN"}), cx)
        create.assert_not_called()

    def test_unidentified_external_process_blocks_cold_resume(self):
        with patch.object(r, "list_history", return_value=([row()], False)), patch.object(r, "inventory", return_value=([row()], [42], [], False)), patch.object(r, "launch_lock", return_value=nullcontext()), patch.object(r, "ensure") as ensure, self.assertRaises(r.ResumeError):
            r.execute(args(), cx)
        ensure.assert_not_called()

    def test_known_external_blocks_even_with_override(self):
        external = dict(row(), external_pids=[42])
        with patch.object(r, "list_history", return_value=([external], False)), patch.object(r, "inventory", return_value=([external], [], [], False)), patch.object(r, "launch_lock", return_value=nullcontext()), patch.object(r, "ensure") as ensure, self.assertRaises(r.ResumeError):
            r.execute(args(allow_unverified_live=True), cx)
        ensure.assert_not_called()

    def test_cancel_does_not_open_ui_or_launch(self):
        with patch.object(r, "list_history", return_value=([row()], False)), patch.object(r, "inventory", return_value=([row()], [], [], False)), patch.object(r, "choose", return_value=[]), patch.object(r, "ensure") as ensure, patch("sys.stdout", new_callable=io.StringIO):
            r.execute(args(select=None, no_iterm=False), cx)
        ensure.assert_not_called()

    def test_json_listing_never_launches(self):
        with patch.object(r, "list_history", return_value=([row()], True)), patch.object(r, "inventory", return_value=([row()], [], [], False)), patch.object(r, "ensure") as ensure, patch("sys.stdout", new_callable=io.StringIO) as out:
            r.execute(args(json=True), cx)
        self.assertTrue(json.loads(out.getvalue())["truncated"])
        ensure.assert_not_called()

    def test_internal_gui_execution_refuses_before_launch(self):
        with patch.object(r, "list_history", return_value=([row()], False)), patch.object(r, "inventory", return_value=([row()], [], [], False)), patch.object(r, "ensure") as ensure, self.assertRaisesRegex(r.ResumeError, "workbench"):
            r.execute(args(no_iterm=False), cx)
        ensure.assert_not_called()

    def test_whole_batch_cwd_preflight(self):
        bad = row(ID2, cwd="/missing-cx-unique")
        with patch.object(r, "list_history", return_value=([row(), bad], False)), patch.object(r, "inventory", return_value=([row(), bad], [], [], False)), patch.object(r, "launch_lock", return_value=nullcontext()), patch.object(r, "ensure") as ensure, self.assertRaises(r.ResumeError):
            r.execute(args(select=[ID1, ID2]), cx)
        ensure.assert_not_called()

    def test_history_failure_still_lists_live_managed_sessions(self):
        live = dict(row(), state="ALIVE")
        with patch.object(r, "list_history", side_effect=r.ResumeError("History unavailable")), patch.object(r, "inventory", return_value=([live], [], [], False)), patch("sys.stdout", new_callable=io.StringIO) as out:
            r.execute(args(json=True), cx)
        self.assertEqual(len(json.loads(out.getvalue())["rows"]), 1)
        self.assertIn("managed sessions only", json.loads(out.getvalue())["warnings"][0])

    def test_invalid_layout_rejected_before_history_or_launch(self):
        for overrides in ({"per_tab": -1}, {"min_columns": 1}, {"min_rows": 1}):
            with patch.object(r, "list_history") as history, self.assertRaises(r.ResumeError):
                r.execute(args(**overrides), cx)
            history.assert_not_called()

    def test_resume_lock_excludes_second_launcher(self):
        with tempfile.TemporaryDirectory() as d, patch.object(r.Path, "home", return_value=Path(d)):
            with r.launch_lock("/codex/home"):
                with self.assertRaises(r.ResumeError):
                    with r.launch_lock("/codex/home"):
                        self.fail("second lock should not be acquired")


if __name__ == "__main__":
    unittest.main()
