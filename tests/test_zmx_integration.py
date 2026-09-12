"""Real zmx 0.8.1 tests with private runtime, HOME, fake Codex and PTYs."""
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_resume
import cx_upgrade
import cx_zmx

ZMX = os.environ.get("ZMX_TEST_BINARY") or shutil.which("zmx")
REQUIRES = bool(ZMX and shutil.which("cc"))
ID1 = "019a0000-1111-7222-8333-444444444444"
ID2 = "019a0000-1111-7222-8333-555555555555"
STUB = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>

int main(int argc, char **argv) {
    const char *dir = getenv("FAKE_CODEX_LOG_DIR");
    const char *name = getenv("CX_AGENT_NAME");
    char path[PATH_MAX], cwd[PATH_MAX];
    if (dir && name) {
        snprintf(path, sizeof(path), "%s/%s.args", dir, name);
        FILE *f = fopen(path, "w");
        if (!f) return 8;
        fprintf(f, "CWD=%s\n", getcwd(cwd, sizeof(cwd)) ? cwd : "ERROR");
        fprintf(f, "NO_DETACH=%s\n", getenv("ZMX_NO_DETACH_KEY") ?: "");
        fprintf(f, "CODEX_HOME=%s\n", getenv("CODEX_HOME") ?: "");
        for (int i = 1; i < argc; ++i) fprintf(f, "ARGV=%s\n", argv[i]);
        fclose(f);
    }
    for (;;) sleep(1);
}
'''


@unittest.skipUnless(REQUIRES, "requires zmx (or ZMX_TEST_BINARY) and cc")
class ZmxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cx-zmx-int-", dir="/tmp")
        self.home = Path(self.temp.name)
        self.bin = self.home / "bin"
        self.logs = self.home / "logs"
        self.codex_home = self.home / "codex-home"
        self.work = self.home / "work with ' space"
        for path in (self.bin, self.logs, self.codex_home, self.work):
            path.mkdir()
        shutil.copy2(str(ZMX), self.bin / "zmx")
        source = self.home / "codex.c"
        source.write_text(STUB)
        subprocess.run(["cc", str(source), "-o", str(self.bin / "codex")], check=True,
                       capture_output=True)
        self.runtime = "/tmp/cxzi-" + uuid.uuid4().hex[:10]
        base = dict(os.environ)
        base.update(HOME=str(self.home), CODEX_HOME=str(self.codex_home),
                    ZMX_DIR=self.runtime, ZMX_DIR_MODE="0700", ZMX_LOG_MODE="0600",
                    FAKE_CODEX_LOG_DIR=str(self.logs),
                    PATH=str(self.bin) + os.pathsep + base.get("PATH", "/usr/bin:/bin"))
        for key in ("ZMX_SESSION", "ZMX_SESSION_PREFIX"):
            base.pop(key, None)
        self.env = base
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()

    def tearDown(self):
        try:
            result = subprocess.run([str(self.bin / "zmx"), "list", "--short"], env=self.env,
                                    text=True, capture_output=True, timeout=5)
            names = [line.strip().removeprefix("→ ") for line in result.stdout.splitlines()
                     if line.strip()]
            if names:
                subprocess.run([str(self.bin / "zmx"), "kill", *names, "--force"], env=self.env,
                               text=True, capture_output=True, timeout=5)
        finally:
            self.environment.stop()
            self.temp.cleanup()
            shutil.rmtree(self.runtime, ignore_errors=True)

    def labels(self, thread="", policy="safe", mode="new"):
        values = {"cx_managed": "1", "cx_version": "0.6.0",
                  "cx_codex_home": cx_zmx.encode_path(str(self.codex_home)),
                  "cx_launch_policy": policy, "cx_launch_mode": mode}
        if thread:
            values["cx_thread_id"] = thread
        return values

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("Timed out waiting for private zmx state")

    def capture(self, name):
        path = self.logs / (name + ".args")
        self.wait(path.exists)
        return path.read_text().splitlines()

    def create(self, name, argv=None, labels=None):
        child_env = dict(self.env, CX_AGENT_NAME=name, CX_MANAGED="1",
                         CODEX_HOME=str(self.codex_home))
        return cx_zmx.create(name, argv or [str(self.bin / "codex")], str(self.work),
                             labels or self.labels(), detached=True,
                             binary=str(self.bin / "zmx"), env=child_env)

    def test_headless_direct_attach_has_persistent_pty_labels_and_alive_codex(self):
        row = self.create("cx-agent-headless-test")
        snap = cx_zmx.snapshot(bind_threads=False)
        current = next(item for item in snap["sessions"] if item["session"] == row["session"])
        self.assertEqual((current["backend"], current["runtime_version"], current["state"]),
                         ("zmx", "0.8.1", "ALIVE"))
        self.assertEqual(current["attached"], 0)
        self.assertEqual(current["generation"]["runtime_dir"], os.path.realpath(self.runtime))
        self.assertEqual(cx_zmx.get_labels(row["session"]), current["labels"])
        captured = self.capture(row["session"])
        self.assertIn("NO_DETACH=1", captured)
        self.assertIn("CWD=" + os.path.realpath(self.work), captured)

    def test_live_v060_generation_is_compatible_without_label_rewrite(self):
        row = self.create("cx-agent-upgrade-status")
        before = cx_zmx.get_labels(row["session"])
        data = cx_upgrade.status_data(cx_zmx)
        current = next(item for item in data["sessions"]
                       if item["session"] == row["session"])
        self.assertEqual(current["upgrade_state"], "UPGRADE_AVAILABLE")
        self.assertEqual(data["status"], "AVAILABLE")
        self.assertEqual(cx_zmx.get_labels(row["session"]), before)

    def test_saved_only_exact_resume_uses_yolo_and_safe_argv(self):
        for thread, yolo in ((ID1, True), (ID2, False)):
            row = {"thread_id": thread, "title": "Saved conversation", "cwd": str(self.work),
                   "home": str(self.codex_home), "managed": None, "external_pids": []}
            created, action = codex_resume.ensure(row, cx_zmx, yolo=yolo)
            self.assertEqual(action, "resume launched in zmx")
            expected = (["ARGV=--yolo"] if yolo else []) + ["ARGV=resume", "ARGV=" + thread,
                "ARGV=--cd", "ARGV=" + os.path.realpath(self.work)]
            actual = [line for line in self.capture(created["session"]) if line.startswith("ARGV=")]
            self.assertEqual(actual, expected)
            self.assertEqual(created["labels"]["cx_launch_policy"], "yolo" if yolo else "safe")

    def test_verified_client_hangup_and_reattach_preserve_generation_and_process(self):
        row = self.create("cx-agent-reattach-test")
        before = next(item for item in cx_zmx.snapshot(bind_threads=False)["sessions"]
                      if item["session"] == row["session"])

        def attach_client():
            pid, master = pty.fork()
            if pid == 0:
                argv = [sys.executable, str(ROOT / "cx_zmx.py"), "attach-verified",
                        str(self.bin / "zmx"),
                        before["generation"]["runtime_dir"], before["session"],
                        str(before["daemon_pid"]), str(before["created"])]
                os.execvpe(sys.executable, argv, self.env)
            return pid, master

        first_pid, first_master = attach_client()
        try:
            attached = self.wait(lambda: cx_zmx.exact_session(row["session"])
                                 if cx_zmx.exact_session(row["session"])["attached"] == 1 else None)
            self.assertEqual(attached["daemon_pid"], before["daemon_pid"])
            self.assertTrue(cx_zmx.client_ttys(before))
        finally:
            os.kill(first_pid, signal.SIGHUP)
            os.close(first_master)
            os.waitpid(first_pid, 0)
        self.wait(lambda: cx_zmx.exact_session(row["session"])["attached"] == 0)
        after = next(item for item in cx_zmx.snapshot(bind_threads=False)["sessions"]
                     if item["session"] == row["session"])
        self.assertEqual(cx_zmx.normalize_generation(after), cx_zmx.normalize_generation(before))
        self.assertEqual(after["codex_pids"], before["codex_pids"])
        second_pid, second_master = attach_client()
        try:
            self.wait(lambda: cx_zmx.exact_session(row["session"])["attached"] == 1)
        finally:
            os.kill(second_pid, signal.SIGHUP)
            os.close(second_master)
            os.waitpid(second_pid, 0)

    def test_five_sessions_are_distinct_and_arguments_are_literal(self):
        marker = self.home / "must-not-exist"
        payload = "$(touch " + str(marker) + ")"
        rows = []
        for index in range(5):
            name = "cx-agent-many-" + str(index)
            rows.append(self.create(name, [str(self.bin / "codex"), payload,
                                           "space ' quote", "semi;colon"]))
        snap = cx_zmx.snapshot(bind_threads=False)
        selected = [row for row in snap["sessions"] if row["session"].startswith("cx-agent-many-")]
        self.assertEqual(len(selected), 5)
        self.assertEqual(len({row["daemon_pid"] for row in selected}), 5)
        self.assertEqual(len({row["codex_pids"][0] for row in selected}), 5)
        for row in rows:
            actual = [line for line in self.capture(row["session"]) if line.startswith("ARGV=")]
            self.assertEqual(actual, ["ARGV=" + payload, "ARGV=space ' quote", "ARGV=semi;colon"])
        self.assertFalse(marker.exists())

    def test_installed_shell_wrapper_launches_only_zmx_backend(self):
        subprocess.run(["zsh", str(ROOT / "install.sh")], env=self.env,
                       check=True, text=True, capture_output=True, timeout=15)
        command = 'source "$HOME/.cxdeck.zsh"; source "$HOME/.cxdeck.zsh"; cx run --detach -- wrapper-literal'
        result = subprocess.run(["zsh", "-f", "-c", command], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        rows = cx_zmx.snapshot(bind_threads=False)["sessions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["backend"], "zmx")
        actual = [line for line in self.capture(rows[0]["session"]) if line.startswith("ARGV=")]
        self.assertEqual(actual, ["ARGV=wrapper-literal"])


if __name__ == "__main__":
    unittest.main()
