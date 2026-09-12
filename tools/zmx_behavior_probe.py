#!/usr/bin/env python3
"""Disposable direct-PTY versus zmx-PTY terminal behavior probe.

The probe creates only private runtime state and a dummy full-screen TUI. It
never discovers or attaches to the user's normal zmx sessions.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cx_zmx
from cx_version import VERSION

DUMMY = r'''#!/usr/bin/env python3
import os
import shutil
import signal
import sys
import termios
import tty

fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)

def emit(value):
    os.write(sys.stdout.fileno(), value)

def size(*_):
    columns, rows = shutil.get_terminal_size((80, 24))
    emit(("\r\nRESIZE:%dx%d\r\n" % (columns, rows)).encode())

try:
    tty.setraw(fd)
    signal.signal(signal.SIGWINCH, size)
    emit(b"\x1b[?1049h")
    emit(b"\x1b[31mANSI_RED\x1b[0m\r\n")
    emit(b"\x1b]0;CX Z0 TITLE\x07")
    emit(b"\x1b]8;;https://example.com/cx-z0\x1b\\OSC8_LINK\x1b]8;;\x1b\\\r\n")
    for index in range(128):
        emit(("LINE-%04d abcdefghijklmnopqrstuvwxyz\r\n" % index).encode())
    size()
    emit(b"READY\r\n")
    while True:
        value = os.read(fd, 4096)
        if not value:
            break
        emit(("INPUT:" + value.hex() + "\r\n").encode())
        if b"q" in value:
            emit(b"EXIT\r\n\x1b[?1049l")
            break
finally:
    termios.tcsetattr(fd, termios.TCSANOW, original)
'''


class ProbeFailure(RuntimeError):
    pass


def read_until(master, marker, timeout=8):
    marker = marker if isinstance(marker, bytes) else marker.encode()
    data, deadline = bytearray(), time.monotonic() + timeout
    while marker not in data and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], min(0.1, max(0, deadline - time.monotonic())))
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
    if marker not in data:
        raise ProbeFailure("Timed out waiting for terminal marker " + repr(marker))
    return bytes(data)


def wait_child(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result, status = os.waitpid(pid, os.WNOHANG)
        if result:
            return status
        time.sleep(0.05)
    raise ProbeFailure("PTY client did not exit after terminal closure")


def spawn(argv, env, cwd):
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    return pid, master


def resize(master, columns=111, rows=37):
    fcntl.ioctl(master, termios_tiocswinsz(), struct.pack("HHHH", rows, columns, 0, 0))


def termios_tiocswinsz():
    import termios
    return termios.TIOCSWINSZ


def exercise_live(master):
    initial = read_until(master, b"READY")
    checks = {
        "alternate_screen": b"\x1b[?1049h" in initial,
        "ansi_colors": b"\x1b[31mANSI_RED\x1b[0m" in initial,
        "osc_title": b"\x1b]0;CX Z0 TITLE\x07" in initial,
        "osc8_hyperlink": b"https://example.com/cx-z0" in initial and b"OSC8_LINK" in initial,
        "long_output": b"LINE-0000" in initial and b"LINE-0127" in initial,
    }
    inputs = {
        "keyboard": b"A",
        "ctrl_backslash": b"\x1c",
        "paste": b"\x1b[200~paste text\n\x1b[201~",
        "mouse_wheel": b"\x1b[<64;10;5M",
        "keyboard_escape": b"\x1b[A",
    }
    for key, value in inputs.items():
        os.write(master, value)
        checks[key] = ("INPUT:" + value.hex()).encode() in read_until(
            master, ("INPUT:" + value.hex()).encode())
    resize(master)
    checks["resize"] = b"RESIZE:111x37" in read_until(master, b"RESIZE:111x37")
    return checks


def exact_row(binary, env, name):
    rows = cx_zmx.list_sessions(binary=binary, env=env)
    return next((row for row in rows if row["session"] == name), None)


def run_probe(binary):
    if not binary or not Path(binary).is_file():
        raise ProbeFailure("Pass an existing zmx 0.8.1+ binary with --zmx")
    temp = tempfile.TemporaryDirectory(prefix="cx-z0-", dir="/tmp")
    home = Path(temp.name)
    runtime = "/tmp/cxz0-" + uuid.uuid4().hex[:10]
    tui = home / "dummy_tui.py"
    tui.write_text(DUMMY)
    tui.chmod(0o700)
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / "codex-home"),
               ZMX_DIR=runtime, ZMX_DIR_MODE="0700", ZMX_LOG_MODE="0600",
               ZMX_NO_DETACH_KEY="1", TERM="xterm-256color")
    for key in ("ZMX_SESSION", "ZMX_SESSION_PREFIX"):
        env.pop(key, None)
    name = "cx-agent-z0-" + uuid.uuid4().hex[:10]
    direct_pid = direct_master = zmx_pid = zmx_master = None
    report = {"zmx_binary": os.path.realpath(binary), "zmx_version": None,
              "runtime_dir": runtime, "direct": {}, "zmx": {}, "comparisons": {},
              "ownership": {"selection": "iTerm/native terminal",
                            "copy": "iTerm/native terminal", "scrollback": "iTerm/native terminal"}}
    try:
        info = cx_zmx.preflight(binary=binary, env=env)
        report["zmx_version"] = info["version"]
        report["runtime_dir"] = info["socket_dir"]

        direct_pid, direct_master = spawn([sys.executable, str(tui)], env, str(home))
        report["direct"] = exercise_live(direct_master)
        os.close(direct_master)
        direct_master = None
        wait_child(direct_pid)
        direct_pid = None
        report["direct"]["terminal_close_ends_process"] = True

        labels = cx_zmx.serialize_labels({"cx_managed": "1", "cx_version": VERSION,
            "cx_codex_home": cx_zmx.encode_path(str(home / "codex-home")),
            "cx_launch_policy": "safe", "cx_launch_mode": "new"})
        zmx_pid, zmx_master = spawn([binary, "attach", "--labels", labels, name,
                                      sys.executable, str(tui)], env, str(home))
        report["zmx"] = exercise_live(zmx_master)
        before = exact_row(binary, env, name)
        if not before:
            raise ProbeFailure("Disposable zmx session was not listed")
        os.close(zmx_master)
        zmx_master = None
        wait_child(zmx_pid)
        zmx_pid = None
        after = exact_row(binary, env, name)
        report["zmx"]["terminal_close_preserves_process"] = bool(
            after and cx_zmx.normalize_generation(after) == cx_zmx.normalize_generation(before))

        zmx_pid, zmx_master = spawn([binary, "attach", name], env, str(home))
        replay = read_until(zmx_master, b"READY")
        report["zmx"]["reattach_screen"] = b"READY" in replay
        os.write(zmx_master, b"R")
        report["zmx"]["reattach_keyboard"] = b"INPUT:52" in read_until(zmx_master, b"INPUT:52")
        os.write(zmx_master, b"q")
        exit_data = read_until(zmx_master, b"EXIT")
        report["zmx"]["alternate_screen_exit"] = b"\x1b[?1049l" in exit_data
        os.close(zmx_master)
        zmx_master = None
        wait_child(zmx_pid)
        zmx_pid = None
        deadline = time.monotonic() + 5
        while exact_row(binary, env, name) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        report["zmx"]["session_cleanup_after_tui_exit"] = exact_row(binary, env, name) is None

        common = sorted(set(report["direct"]) & set(report["zmx"]))
        report["comparisons"] = {key: report["direct"][key] == report["zmx"][key] is True
                                 for key in common}
        required = [value for section in (report["direct"], report["zmx"], report["comparisons"])
                    for value in section.values()]
        report["passed"] = all(required)
        return report
    finally:
        for pid, master in ((direct_pid, direct_master), (zmx_pid, zmx_master)):
            if master is not None:
                try:
                    os.close(master)
                except OSError:
                    pass
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGHUP)
                    os.waitpid(pid, 0)
                except (OSError, ChildProcessError):
                    pass
        try:
            row = exact_row(binary, env, name)
            if row:
                subprocess.run([binary, "kill", name, "--force"], env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
        temp.cleanup()
        shutil.rmtree(runtime, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--zmx", default=os.environ.get("ZMX_TEST_BINARY") or shutil.which("zmx"))
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    report = run_probe(args.zmx)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(output)
    print(output, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProbeFailure, cx_zmx.Error, OSError, subprocess.SubprocessError) as exc:
        print("Z0 probe failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
