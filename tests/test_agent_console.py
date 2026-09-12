"""Agent-first zmx unit tests; no Codex API calls or user state."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_console as agent


def session(name="cx-agent-test", task="test"):
    generation = {"host": "test", "runtime_dir": "/tmp/zmx", "session": name,
                  "daemon_pid": 17, "created": 123}
    return {"backend": "zmx", "session": name, "sid": name, "task": task,
            "created": 123, "daemon_pid": 17, "generation": generation,
            "codex_home": os.path.realpath("/home/test/.codex"), "thread_id": "",
            "root": "", "launch_cwd": "/home/test", "labels": {}, "state": "ALIVE"}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.Error = RuntimeError
        self.backend.validate.side_effect = lambda value: value
        self.backend.sessions.return_value = []
        self.backend.list_sessions.return_value = []
        self.backend.preflight.return_value = {"path": "/test/bin/zmx", "version": "0.8.1",
                                               "socket_dir": "/tmp/zmx"}
        self.backend.encode_path.return_value = "encoded-home"
        self.backend.create.side_effect = lambda name, *args, **kwargs: session(name)
        self.backend.normalize_generation.side_effect = lambda row: (
            row['generation']['host'], row['generation']['runtime_dir'], row['session'],
            row['generation']['daemon_pid'], row['generation']['created'])
        self.backend.clean.side_effect = str
        self.patches = [patch("agent_console.backend", return_value=self.backend),
                        patch("agent_console._annotate_created"),
                        patch("agent_console.shutil.which", side_effect=lambda name: "/test/bin/" + name),
                        patch("agent_console.os.getcwd", return_value="/home/test"),
                        patch.dict(os.environ, {"CODEX_HOME": "/home/test/.codex"})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_utility_and_interactive_routing(self):
        for argv in (["exec", "task"], ["--version"], ["mcp", "list"], ["resume", "--help"],
                     ["agents"], ["plugin", "list"], ["doctor"], ["queue", "id"]):
            self.assertTrue(agent.invocation(argv)[0])
        for argv in ([], ["resume", "--all"], ["fork", "id"], ["literal prompt"], ["--", "exec"]):
            self.assertFalse(agent.invocation(argv)[0])

    def test_provisional_name_is_unique_and_cwd_independent(self):
        fixed = agent.identity("review")
        with patch("agent_console.os.getcwd", return_value="/elsewhere"):
            self.assertEqual(fixed, agent.identity("review"))
        self.assertEqual(len({agent.identity()[1] for _ in range(8)}), 8)

    def test_provisional_name_namespaces_codex_home(self):
        first = agent.identity("review")
        with patch.dict(os.environ, {"CODEX_HOME": "/other"}):
            self.assertNotEqual(first, agent.identity("review"))

    def test_new_agent_uses_direct_zmx_create_and_required_labels(self):
        agent.start_agent([], detached=True)
        call = self.backend.create.call_args
        self.assertEqual(call.args[1], ["/test/bin/codex"])
        self.assertEqual(call.args[2], os.path.realpath("/home/test"))
        self.assertEqual(call.args[3], {"cx_managed": "1", "cx_version": "0.8.1",
            "cx_codex_home": "encoded-home", "cx_launch_policy": "safe", "cx_launch_mode": "new"})
        self.assertTrue(call.kwargs["detached"])
        self.assertEqual(call.kwargs["env"]["CX_MANAGED"], "1")

    def test_current_pane_gets_presentation_before_blocking_attach(self):
        with patch('sys.stdin.isatty', return_value=True), \
             patch('sys.stdout.isatty', return_value=True), \
             patch.object(agent, 'by_label', return_value=None), \
             patch.object(agent, '_prepare_current_view') as prepare:
            made = agent.start_agent([], 'Visual Encoding Study', detached=False)
        prepare.assert_called_once_with('Visual Encoding Study')
        self.backend.attach.assert_called_once_with(made)

    def test_current_pane_presentation_failure_is_only_a_warning(self):
        store = Mock()
        store.preference.return_value = True
        error = RuntimeError('profile unavailable')
        stderr = io.StringIO()
        with patch('cx_store.Store', return_value=store), \
             patch('cx_iterm.prepare_current_view', side_effect=error), \
             contextlib.redirect_stderr(stderr):
            agent._prepare_current_view('Research Project')
        self.assertIn('presentation warning', stderr.getvalue())
        self.assertIn('unchanged', stderr.getvalue())

    def test_unattached_existing_generation_uses_current_pane(self):
        row = dict(session(task='review'), attached=0)
        self.backend.snapshot.return_value = {'sessions': [row]}
        with patch.object(agent, '_prepare_current_view') as prepare:
            agent._attach_or_focus(row, 'Review', self.backend)
        self.backend.snapshot.assert_called_once_with(bind_threads=False)
        prepare.assert_called_once_with('Review')
        self.backend.attach.assert_called_once_with(row)

    def test_attached_existing_generation_focuses_verified_view_without_second_attach(self):
        row = dict(session(task='review'), attached=1)
        self.backend.snapshot.return_value = {'sessions': [row]}
        adapter, target = Mock(), dict(row, _key='key')
        with patch('console_entry.ResumeBackend', return_value=adapter), \
             patch('workbench.resolve', return_value=target) as resolve, \
             patch('workbench.focus_rows') as focus, \
             patch.object(agent, '_prepare_current_view') as prepare:
            agent._attach_or_focus(row, 'Review', self.backend)
        resolve.assert_called_once_with(row['session'], adapter, bind_threads=False)
        focus.assert_called_once_with([target], adapter)
        self.backend.attach.assert_not_called()
        prepare.assert_not_called()

    def test_ambiguous_attached_generation_refuses_without_attach(self):
        row = dict(session(task='review'), attached=2)
        self.backend.snapshot.return_value = {'sessions': [row]}
        with self.assertRaisesRegex(agent.Error, 'Multiple zmx clients'):
            agent._attach_or_focus(row, 'Review', self.backend)
        self.backend.attach.assert_not_called()

    def test_exact_resume_argv_and_uuid_label_are_literal(self):
        tid = "019a0000-1111-7222-8333-444444444444"
        argv = ["resume", tid, "--cd", "/tmp/space ' directory"]
        agent.start_agent(argv, detached=True)
        call = self.backend.create.call_args
        self.assertEqual(call.args[1], ["/test/bin/codex", *argv])
        self.assertEqual(call.args[3]["cx_thread_id"], tid)
        self.assertEqual(call.args[3]["cx_launch_mode"], "resume")

    def test_wrapper_does_not_inject_yolo_prompt_or_zmx_orchestration(self):
        agent.start_agent(["resume", "--all"], detached=True)
        self.assertEqual(self.backend.create.call_args.args[1],
                         ["/test/bin/codex", "resume", "--all"])
        calls = str(self.backend.method_calls)
        for forbidden in ("send", "print", "tail", "history", "write", "mouse", "kill"):
            self.assertNotIn(forbidden, calls)

    def test_named_existing_reuses_and_new_args_refuse(self):
        row = session(task="review")
        with patch.object(agent, "by_label", return_value=row):
            self.assertEqual(agent.start_agent([], "review", True), row)
            with self.assertRaises(agent.Error):
                agent.start_agent(["resume", "--all"], "review", True)
        self.backend.create.assert_not_called()

    def test_ambiguous_display_name_and_prefix_are_never_guessed(self):
        rows = [session("cx-agent-a", "main"), session("cx-agent-b", "main")]
        with patch.object(agent, "_enriched_rows", return_value=({}, rows)), self.assertRaises(agent.Error):
            agent.by_label("main")
        self.backend.sessions.return_value = [session("cx-agent-one", "one")]
        with patch.object(agent, "by_label", return_value=None), self.assertRaises(agent.Error):
            agent.resolve("cx-agent")

    def test_runtime_name_collision_fails_without_reuse(self):
        self.backend.list_sessions.side_effect = lambda *a, **k: [session(agent.identity("review")[1])]
        with patch.object(agent, "by_label", return_value=None), self.assertRaises(agent.Error):
            agent.start_agent([], "review", True)
        self.backend.create.assert_not_called()

    def test_noninteractive_launch_requires_explicit_detach(self):
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(agent.Error):
            agent.start_agent([])

    def test_utility_passthrough_never_creates_session(self):
        with patch("agent_console.subprocess.call", return_value=7) as call:
            self.assertEqual(agent.run_codex(["exec", "literal;prompt"]), 7)
            call.assert_called_once_with(["/test/bin/codex", "exec", "literal;prompt"])
        self.backend.create.assert_not_called()

    def test_main_preserves_bare_label_resume_and_raw_routing(self):
        with patch("agent_console.start_agent") as start:
            agent.main([])
            start.assert_called_once_with([])
        with patch("agent_console.start_agent") as start:
            agent.main(["theory", "--", "--search"])
            start.assert_called_once_with(["--search"], "theory", False)
        with patch("agent_console.run_codex", return_value=0) as run:
            agent.main(["resume", "--all"])
            run.assert_called_once_with(["resume", "--all"])

    def test_project_annotation_updates_store_without_relaunch(self):
        row = session(task="one")
        self.backend.git.return_value = "/projects/real-repo\n"
        self.backend.snapshot.return_value = {"sessions": [row], "context": {
            "backend": "zmx", "host": "test", "runtime_dir": "/tmp/zmx"}}
        store = Mock()
        with patch.object(agent, "resolve", return_value=row), patch("cx_store.Store", return_value=store):
            agent.bind_project("/projects/real-repo/subdir", "one")
        self.assertEqual(store.annotate.call_args.kwargs["root"], "/projects/real-repo")
        self.backend.create.assert_not_called()

    def test_doctor_reports_only_current_runtime_diagnostics(self):
        self.backend.MIN_VERSION = (0, 8, 1)
        self.backend.version.return_value = {"version": "0.8.1", "version_tuple": (0, 8, 1)}
        self.backend.optional.return_value = "codex-cli 1.2.3"
        self.backend.snapshot.return_value = {"sessions": [{
            "session": "cx-agent-test", "state": "ALIVE",
            "labels": {"cx_version": "0.6.0"}}]}
        diagnostics = {"sessions": [{"launch_policy": "yolo"}],
                       "outside_managed_codex_pids": [], "warnings": []}
        with patch.object(agent, "snapshot", return_value=diagnostics), \
                patch.object(agent.sys, "platform", "linux"):
            agent.doctor()
        output = self.output.getvalue()
        for expected in ("CX Deck 0.8.1", "Python ", "codex path:", "codex version:",
                         "zmx version: 0.8.1 (minimum 0.8.1: PASS)",
                         "zmx runtime/socket directory: /tmp/zmx",
                         "managed zmx sessions: 1", "YOLO=1 SAFE=0 UNKNOWN=0",
                         "unidentified external Codex PIDs: []", "upgrade status: AVAILABLE"):
            self.assertIn(expected, output)
        self.backend.snapshot.assert_called_once_with(bind_threads=False)

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(agent.Error):
            agent.identity("a" * 129)
        for interval in (0, float("nan"), float("inf")):
            with self.assertRaises(agent.Error):
                agent.dashboard(interval)


if __name__ == "__main__":
    unittest.main()
