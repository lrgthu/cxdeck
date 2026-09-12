"""Unit tests for the narrow zmx provider. No real sessions or user state."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cx_zmx as zmx

ID = "019a0000-1111-7222-8333-444444444444"


def info(version="0.8.1"):
    return {"version": version, "version_tuple": tuple(map(int, version.split("."))),
            "socket_dir": os.path.realpath("/tmp/zmx"), "path": "/bin/zmx"}


def listed(name="cx-chat-test", pid=42, created=1700000000, clients=0):
    return (f"name={name}\tpid={pid}\tclients={clients}\tcreated={created}\tcwd=/tmp\tcmd=codex"
            f"\tcx_managed=1\tcx_version=0.6.0\tcx_zmx_version=0.8.1"
            f"\tcx_thread_id={ID}\tcx_codex_home={zmx.encode_path('/tmp/codex home')}"
            f"\tcx_launch_cwd={zmx.encode_path('/tmp')}\tcx_launch_policy=yolo\tcx_launch_mode=resume\n")


class ZmxTests(unittest.TestCase):
    def test_version_parsing_and_minimum(self):
        parsed = zmx.parse_version("zmx\t\t0.8.1\nghostty_vt\t0.1\nsocket_dir\t/tmp/zmx\nlog_dir\t/tmp/log\n")
        self.assertEqual(parsed["version_tuple"], (0, 8, 1))
        with patch.object(zmx, "version", return_value=dict(parsed, version="0.8.0", version_tuple=(0, 8, 0))), \
                self.assertRaisesRegex(zmx.Error, "0.8.1"):
            zmx.preflight("/bin/zmx")

    def test_missing_and_malformed_version_fail_closed(self):
        with patch.object(zmx.shutil, "which", return_value=None), self.assertRaisesRegex(zmx.Error, "brew install"):
            zmx.version()
        for text in ("", "zmx\t0.8.1\n", "socket_dir\t/tmp\nzmx\tdev\n"):
            with self.subTest(text=text), self.assertRaises(zmx.Error):
                zmx.parse_version(text)

    def test_path_label_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="cx path ") as directory:
            encoded = zmx.encode_path(directory)
            self.assertRegex(encoded, r"\Ab64_[A-Za-z0-9_.-]+\Z")
            self.assertEqual(zmx.decode_path(encoded), os.path.realpath(directory))

    def test_label_serialization_is_strict(self):
        labels = {"cx_managed": "1", "cx_launch_policy": "safe"}
        self.assertEqual(zmx.parse_labels(zmx.serialize_labels(labels)), labels)
        for values in ({"name": "value with space"}, {"bad/key": "x"}):
            with self.subTest(values=values), self.assertRaises(zmx.Error):
                zmx.serialize_labels(values)

    def test_list_normalizes_backend_labels_and_generation(self):
        row = zmx.parse_list(listed(), "/tmp/zmx", host="host-a")[0]
        self.assertEqual(row["thread_id"], ID)
        self.assertEqual(row["codex_home"], os.path.realpath("/tmp/codex home"))
        self.assertEqual(row["cx_version"], "0.6.0")
        self.assertEqual(row["launch_policy"], "yolo")
        self.assertEqual(zmx.normalize_generation(row),
                         ("host-a", os.path.realpath("/tmp/zmx"), "cx-chat-test", 42, 1700000000))

    def test_name_alone_is_not_generation_identity(self):
        one = zmx.parse_list(listed(pid=42, created=1), "/tmp/zmx", "host")[0]
        two = zmx.parse_list(listed(pid=43, created=2), "/tmp/zmx", "host")[0]
        self.assertEqual(one["session"], two["session"])
        self.assertNotEqual(zmx.normalize_generation(one), zmx.normalize_generation(two))
        self.assertNotEqual(zmx.generation_token(one), zmx.generation_token(two))

    def test_unmanaged_and_malformed_records_are_not_guessed(self):
        self.assertEqual(zmx.parse_list(listed().replace("cx_managed=1", "cx_managed=0"), "/tmp/zmx"), [])
        for text in ("name=cx-chat-x\tpid=1\tclients=0\n",
                     "name=cx-chat-x\tpid=x\tclients=0\tcreated=1\tcx_managed=1\n",
                     "name=cx-chat-x\tpid=1\tpid=2\tclients=0\tcreated=1\tcx_managed=1\n"):
            with self.subTest(text=text), self.assertRaises(zmx.Error):
                zmx.parse_list(text, "/tmp/zmx")

    def test_detached_create_uses_direct_attach_atomic_labels_and_env(self):
        row = zmx.parse_list(listed(), "/tmp/zmx", socket.gethostname())[0]
        with tempfile.TemporaryDirectory() as cwd, patch.object(zmx, "preflight", return_value=info()), \
                patch.object(zmx, "_zmx", return_value=""), \
                patch.object(zmx, "_wait_created", return_value=row), \
                patch.object(zmx.subprocess, "run", return_value=Mock(returncode=0, stderr="")) as run:
            zmx.create("cx-chat-test", ["/bin/codex", "resume", ID], cwd,
                       {"cx_managed": "1"}, detached=True, binary="/bin/zmx")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/bin/zmx", "attach", "--labels"])
        self.assertEqual(argv[-4:], ["cx-chat-test", "/bin/codex", "resume", ID])
        labels = zmx.parse_labels(argv[3])
        self.assertEqual(labels["cx_version"], "0.7.0")
        self.assertEqual(labels["cx_zmx_version"], "0.8.1")
        self.assertEqual(zmx.decode_path(labels["cx_launch_cwd"]), os.path.realpath(cwd))
        self.assertEqual(run.call_args.kwargs["env"]["ZMX_NO_DETACH_KEY"], "1")
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_create_refuses_same_name_even_when_existing_session_is_unmanaged(self):
        raw = "name=cx-chat-test\tpid=9\tclients=0\tcreated=1\tcwd=/tmp\tcmd=shell\n"
        with tempfile.TemporaryDirectory() as cwd, patch.object(zmx, "preflight", return_value=info()), \
                patch.object(zmx, "_zmx", return_value=raw), \
                patch.object(zmx.subprocess, "run") as run, self.assertRaisesRegex(zmx.Error, "already exists"):
            zmx.create("cx-chat-test", ["/bin/codex"], cwd, {"cx_managed": "1"},
                       detached=True, binary="/bin/zmx")
        run.assert_not_called()

    def test_attach_rechecks_generation_and_marks_client(self):
        row = zmx.parse_list(listed(), "/tmp/zmx", socket.gethostname())[0]
        with patch.object(zmx, "preflight", return_value=info()), \
                patch.object(zmx, "exact_session", return_value=row), \
                patch.object(zmx.sys.stdin, "isatty", return_value=True), \
                patch.object(zmx.sys.stdout, "isatty", return_value=True), \
                patch.object(zmx.subprocess, "call", return_value=0) as call:
            zmx.attach(row)
        self.assertEqual(call.call_args.args[0], ["/bin/zmx", "attach", "cx-chat-test"])
        self.assertEqual(call.call_args.kwargs["env"]["ZMX_NO_DETACH_KEY"], "1")
        self.assertEqual(call.call_args.kwargs["env"]["CX_ZMX_GENERATION"], zmx.generation_token(row))

    def test_verified_iterm_attach_uses_the_recorded_runtime(self):
        runtime = os.path.realpath("/tmp/private zmx runtime")
        row = zmx.parse_list(listed(), runtime, socket.gethostname())[0]
        provider = dict(info(), socket_dir=runtime)
        with patch.object(zmx, "preflight", return_value=provider) as preflight, \
                patch.object(zmx, "exact_session", return_value=row) as exact, \
                patch.object(zmx.os, "execve", side_effect=OSError("test stop")) as execute, \
                self.assertRaisesRegex(OSError, "test stop"):
            zmx._attach_verified(["/bin/zmx", runtime, row["session"],
                                  str(row["daemon_pid"]), str(row["created"])])
        self.assertEqual(preflight.call_args.kwargs["env"]["ZMX_DIR"], runtime)
        self.assertEqual(exact.call_args.kwargs["env"]["ZMX_DIR"], runtime)
        self.assertEqual(execute.call_args.args[2]["ZMX_DIR"], runtime)
        self.assertEqual(execute.call_args.args[2]["ZMX_NO_DETACH_KEY"], "1")

    def test_stale_attach_and_noninteractive_kill_fail_without_mutation(self):
        row = zmx.parse_list(listed(), "/tmp/zmx", socket.gethostname())[0]
        recycled = zmx.parse_list(listed(pid=99, created=2), "/tmp/zmx", socket.gethostname())[0]
        with patch.object(zmx, "preflight", return_value=info()), \
                patch.object(zmx, "exact_session", return_value=recycled), self.assertRaises(zmx.Error):
            zmx.attach_argv(row)
        with patch.object(zmx.sys.stdin, "isatty", return_value=False), \
                patch.object(zmx, "_zmx") as command, self.assertRaises(zmx.Error):
            zmx.kill_session(row)
        command.assert_not_called()

    def test_explicit_kill_requires_exact_generation_and_full_name(self):
        row = zmx.parse_list(listed(), "/tmp/zmx", socket.gethostname())[0]
        with patch.object(zmx.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value=row["session"]), \
                patch.object(zmx, "exact_session", return_value=row) as exact, \
                patch.object(zmx, "_zmx") as command, \
                patch("sys.stdout"):
            zmx.kill_session(row)
        exact.assert_called_once_with(row["session"], generation=zmx.normalize_generation(row))
        command.assert_called_once_with(None, "kill", row["session"])

    def test_process_inventory_and_no_codex_state_are_exact(self):
        parsed = zmx.parse_ps(
            " 10 1 0.0 1024 02:00 Ss ?? /bin/zsh\n"
            " 11 10 1.0 2048 01:59 S ?? /usr/local/bin/node\n"
            " 12 11 2.5 4096 01:58 S ttys001 /opt/vendor dir/codex\n"
            " 13 12 30.0 8192 01:50 R ?? /usr/bin/python3\n"
            " 15 1 99.0 8192 05:00 R ?? /usr/bin/codex-helper\n")
        self.assertEqual(zmx.descendants([10], parsed), {10, 11, 12, 13})
        self.assertTrue(zmx.is_codex(parsed[12]))
        self.assertFalse(zmx.is_codex(parsed[15]))
        row = zmx.parse_list(listed(pid=10), "/tmp/zmx", socket.gethostname())[0]
        with patch.object(zmx, "preflight", return_value=info()), \
                patch.object(zmx, "list_sessions", return_value=[row]), \
                patch.object(zmx, "processes", return_value={10: parsed[10], 11: parsed[11]}), \
                patch.object(zmx, "gpu_state", return_value={"jobs": []}):
            result = zmx.snapshot(bind_threads=False)
        self.assertEqual(result["sessions"][0]["state"], "NO_CODEX")
        self.assertEqual(result["sessions"][0]["codex_pids"], [])

    def test_provider_has_no_orchestration_command_literals(self):
        source = Path(zmx.__file__).read_text()
        for forbidden in ('"send"', '"print"', '"tail"', '"history"', '"write"', 'shell=True', 'os.system'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
