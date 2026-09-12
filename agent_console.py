#!/usr/bin/env python3
"""Agent-first cx entry point backed only by zmx persistent PTYs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

from cx_version import VERSION
VALUE_FLAGS = {"-c", "--config", "-C", "--cd", "-m", "--model", "-p", "--profile",
               "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
               "--add-dir", "--enable", "--disable", "--local-provider"}
UTILITY_COMMANDS = {"exec", "e", "review", "login", "logout", "mcp", "mcp-server",
                    "app-server", "app", "completion", "sandbox", "debug", "apply",
                    "a", "cloud", "features", "help", "update", "serve", "agents",
                    "plugin", "remote-control", "queue", "archive", "delete",
                    "migrate-rollouts", "unarchive", "exec-server", "doctor"}


class Error(RuntimeError):
    pass


def backend():
    import cx_zmx
    cx_zmx.VERSION = VERSION
    return cx_zmx


def invocation(argv):
    """Return (utility, first positional token) without interpreting prompts."""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return False, None
        if arg in ("-h", "--help", "-V", "--version"):
            return True, arg
        if arg in VALUE_FLAGS:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if arg in ("resume", "fork"):
            tail, pos = argv[index + 1:], 0
            while pos < len(tail) and tail[pos].startswith("-") and tail[pos] != "--":
                if tail[pos] in ("-h", "--help"):
                    return True, arg
                pos += 2 if tail[pos] in VALUE_FLAGS else 1
        return arg in UTILITY_COMMANDS, arg
    return False, None


def codex_home():
    return os.path.realpath(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))


def identity(label=None):
    """Create a provisional runtime name; it is never conversation identity."""
    if label is None:
        label = "agent-" + uuid.uuid4().hex[:12]
    backend().validate(label)
    if len(label) > 128:
        raise Error("Agent names must be at most 128 characters.")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "agent"
    digest = hashlib.sha256((codex_home() + "\0" + label).encode()).hexdigest()[:12]
    return label, f"cx-agent-{slug[:24]}-{digest}"


def _thread_from_argv(argv):
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in VALUE_FLAGS:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if arg != "resume":
            return ""
        for candidate in argv[index + 1:]:
            if candidate.startswith("-"):
                continue
            try:
                return str(uuid.UUID(candidate))
            except ValueError:
                return ""
        return ""
    return ""


def _launch_policy(argv):
    return "yolo" if "--yolo" in argv else "safe"


def option(session, name):
    key = name.removeprefix("@cx_")
    field = {"thread_id": "thread_id", "codex_home": "codex_home",
             "launch_policy": "launch_policy", "launch_mode": "launch_mode",
             "launch_cwd": "launch_cwd"}.get(key)
    if field:
        return session.get(field, "")
    return session.get("labels", {}).get("cx_version", "") if key == "version" else ""


def _enriched_rows():
    from cx_store import Store, live_key, thread_key
    data, state = backend().snapshot(), Store().read()
    rows = []
    for row in data["sessions"]:
        key = live_key(row, data["context"])
        thread = thread_key(row.get("codex_home"), row.get("thread_id"), data["context"]["host"])
        metadata = dict(state["agents"].get(key, {}))
        if thread:
            metadata.update(state["agents"].get(thread, {}))
        item = dict(row)
        item["_key"], item["_thread_key"] = key, thread
        item["task"] = metadata.get("name") or row["session"]
        item["root"] = metadata.get("root", row.get("root", ""))
        item["launch_cwd"] = metadata.get("launch_cwd", row.get("launch_cwd", ""))
        rows.append(item)
    return data, rows


def by_label(label):
    _, rows = _enriched_rows()
    matches = [row for row in rows if row["task"] == label and row.get("codex_home") == codex_home()]
    if len(matches) > 1:
        raise Error("More than one agent has that display name. Use cxa with the full zmx session name from cxl.")
    return matches[0] if matches else None


def resolve(label):
    exact = [row for row in backend().sessions() if row["session"] == label]
    if exact:
        return exact[0]
    result = by_label(label)
    if result is None:
        raise Error("No agent with this exact runtime name or display name: " + backend().clean(label))
    return result


def _annotate_created(row, label, cwd):
    from cx_store import Store, live_key, thread_key
    data = backend().snapshot(bind_threads=False)
    current = next((item for item in data["sessions"]
                    if backend().normalize_generation(item) == backend().normalize_generation(row)), row)
    keys = [live_key(current, data["context"])]
    thread = thread_key(current.get("codex_home"), current.get("thread_id"), data["context"]["host"])
    if thread:
        keys.append(thread)
    values = {"launch_cwd": cwd}
    if label is not None:
        values["name"] = label
    Store().annotate(keys, **values)


def _prepare_current_view(display_name):
    """Best-effort presentation for a caller-owned iTerm pane."""
    from cx_store import Store
    import cx_iterm
    try:
        cx_iterm.prepare_current_view(
            display_name, Store().preference('timestamps', True))
    except (RuntimeError, OSError) as exc:
        print('cx: presentation warning: ' + str(exc) +
              '. The Codex process and zmx generation are unchanged.', file=sys.stderr)


def _attach_or_focus(row, display_name, b=None):
    """Use the caller pane only when no client exists; otherwise focus one verified view."""
    b = b or backend()
    data = b.snapshot()
    expected = b.normalize_generation(row)
    matches = [item for item in data['sessions']
               if b.normalize_generation(item) == expected]
    if len(matches) != 1:
        raise Error('The zmx generation changed or became ambiguous; no view was attached.')
    current = matches[0]
    attached = current.get('attached', 0)
    if attached > 1:
        raise Error('Multiple zmx clients already view this conversation. Close extras before choosing a preferred view.')
    if attached == 0:
        _prepare_current_view(display_name)
        b.attach(current)
        return
    # A client already exists. The iTerm layer verifies GUID + TTY against the
    # exact zmx client generation before focusing it and never opens a duplicate.
    import console_entry
    import workbench
    adapter = console_entry.ResumeBackend(sys.modules[__name__])
    target = workbench.resolve(current['session'], adapter)
    workbench.focus_rows([target], adapter)


def start_agent(codex_args, label=None, detached=False):
    b, cwd = backend(), os.path.realpath(os.getcwd())
    b.validate(cwd)
    if label is not None:
        found = by_label(label)
        if found:
            if codex_args:
                raise Error("Agent already exists; new Codex arguments were not executed. Attach or choose another label.")
            print("Existing: " + found["session"])
            if not detached:
                _attach_or_focus(found, found.get('task') or label, b)
            return found
    requested_label = label
    label, name = identity(label)
    if not detached and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Error("Interactive launch requires a terminal. Use 'cx start --detach [label]' explicitly.")
    executable = shutil.which("codex")
    if not executable:
        raise Error("codex is not on PATH.")
    info = b.preflight()
    if any(row["session"] == name for row in b.list_sessions(info["path"], info=info)):
        raise Error("Agent runtime-name collision; no existing session was reused.")
    mode = invocation(codex_args)[1]
    mode = mode if mode in ("resume", "fork") else "new"
    tid = _thread_from_argv(codex_args)
    labels = {"cx_managed": "1", "cx_version": VERSION,
              "cx_codex_home": b.encode_path(codex_home()),
              "cx_launch_policy": _launch_policy(codex_args), "cx_launch_mode": mode}
    if tid:
        labels["cx_thread_id"] = tid
    env = {"PATH": os.environ.get("PATH", ""), "CODEX_HOME": codex_home(),
           "CX_MANAGED": "1", "CX_AGENT_NAME": name,
           "CX_CONSOLE": str(Path(__file__).resolve())}
    print(f"Managed zmx session: {name} | launch cwd: {b.clean(cwd)}")
    row = b.create(name, [executable, *codex_args], cwd, labels, detached=True,
                   binary=info["path"], env=env)
    _annotate_created(row, requested_label, cwd)
    if not detached:
        _prepare_current_view(requested_label or 'New Codex session')
        b.attach(row)
    return row


def run_codex(argv, detached=False):
    utility, _ = invocation(argv)
    if utility:
        executable = shutil.which("codex")
        if not executable:
            raise Error("codex is not on PATH.")
        return subprocess.call([executable, *argv])
    start_agent(argv, detached=detached)
    return 0


def bind_project(path, session=None):
    from cx_store import Store, live_key, thread_key
    b, target = backend(), session or os.environ.get("CX_AGENT_NAME")
    if not target:
        raise Error("Run inside the managed agent, or pass --session with an exact name/display name.")
    row = resolve(target)
    root = b.git(os.path.realpath(os.path.expanduser(path)), "rev-parse", "--show-toplevel").rstrip("\n")
    b.validate(root)
    data = b.snapshot()
    current = next(item for item in data["sessions"] if item["session"] == row["session"])
    keys = [live_key(current, data["context"]),
            thread_key(current.get("codex_home"), current.get("thread_id"), data["context"]["host"])]
    Store().annotate(keys, root=root)
    print("Project metadata bound: " + b.clean(root) + ". Conversation and zmx generation unchanged.")


def snapshot():
    data, rows = _enriched_rows()
    data["sessions"] = rows
    for row in rows:
        row["scope"] = "agent"
        row["project_assigned"] = bool(row.get("root"))
        if row["project_assigned"]:
            row.update(backend().git_state(row["root"]))
            row["repo"] = Path(row["root"]).name
    return data


def render(data):
    b = backend()
    lines = [f"CX Deck {VERSION} | zmx {data.get('runtime_version', 'UNKNOWN')} | "
             f"{b.clean(data['host'])} | {len(data['sessions'])} agents",
             "AGENT | STATE | VIEWS | PID | AGE | POLICY | PROJECT"]
    for row in data["sessions"]:
        project = row.get("repo", "?") if row.get("project_assigned") else "unassigned (normal)"
        if row.get("project_assigned"):
            project += "@" + (row.get("branch") or "?")
            if row.get("dirty"):
                project += " DIRTY"
        pids = ",".join(map(str, row["codex_pids"])) or "-"
        lines += [f"{row['task']} | {row['state']} | {row['attached']} | {pids} | "
                  f"{row['elapsed'] or '-'} | {row['launch_policy'].upper()} | {project}",
                  f"  {row['session']}  [entry: {row['launch_mode']}; backend: zmx]"]
    if not data["sessions"]:
        lines.append("No managed zmx agents. Run codex, codex resume --all, or cx.")
    lines += ["ALIVE means a Codex process exists; it does not describe task progress.",
              "cxa <full runtime name or display name> attaches through zmx."]
    if data.get("outside_managed_codex_pids"):
        lines.append("Unidentified external Codex PIDs: " + str(data["outside_managed_codex_pids"]))
    lines += ["WARNING: " + warning for warning in data.get("warnings", [])]
    return [b.clean(line) for line in lines]


def dashboard(interval=3, once=False, as_json=False):
    if not math.isfinite(interval) or interval < 1:
        raise Error("Refresh interval must be finite and at least one second.")
    watch = sys.stdout.isatty() and not once and not as_json
    try:
        if watch:
            print("\033[?1049h\033[?25l", end="", flush=True)
        while True:
            data = snapshot()
            if as_json:
                print(json.dumps(data, indent=2, ensure_ascii=True))
            elif watch:
                width, height = shutil.get_terminal_size((100, 30))
                lines, budget = render(data), max(1, height - 2)
                if len(lines) > budget:
                    lines = lines[:max(0, budget - 1)] + ["More rows: cxl or cx status --json"]
                print("\033[H\033[2J" + "\n".join(line[:max(1, width - 1)] for line in lines)
                      + "\nCtrl-C: close only this dashboard", end="", flush=True)
            else:
                print("\n".join(render(data)))
            if not watch:
                return
            time.sleep(interval)
    finally:
        if watch:
            print("\033[?25h\033[?1049l", end="", flush=True)


def doctor():
    b = backend()
    minimum = ".".join(map(str, b.MIN_VERSION))
    print(f"CX Deck {VERSION} | Python {sys.version.split()[0]} | {socket.gethostname()}")
    codex, zmx = shutil.which("codex"), shutil.which("zmx")
    print("codex path: " + (codex or "not found"))
    if codex:
        print("codex version: " + b.optional(lambda: b.run([codex, "--version"]).strip(), "unavailable"))
    print("zmx path: " + (zmx or "not found"))
    try:
        info = b.preflight()
        print(f"zmx version: {info['version']} (minimum {minimum}: PASS)")
        print("zmx runtime/socket directory: " + info["socket_dir"])
        data = snapshot()
        print(f"managed zmx sessions: {len(data['sessions'])}")
        counts = {value: sum(row["launch_policy"] == value for row in data["sessions"])
                  for value in ("yolo", "safe", "UNKNOWN")}
        print(f"launch policy: YOLO={counts['yolo']} SAFE={counts['safe']} UNKNOWN={counts['UNKNOWN']}")
        print("unidentified external Codex PIDs: " + str(data["outside_managed_codex_pids"]))
    except b.Error as exc:
        print(f"zmx >= {minimum}: FAIL — " + b.clean(exc))
    if sys.platform == "darwin" and shutil.which("osascript"):
        try:
            from cx_iterm import ITerm
            ITerm().preflight()
            print("iTerm2 automation: PASS")
        except (RuntimeError, OSError) as exc:
            print("iTerm2 automation: FAIL — " + b.clean(exc))
    else:
        print("iTerm2 automation: N/A (non-macOS or osascript unavailable)")
    try:
        import cx_upgrade
        upgrade = cx_upgrade.status_data(b, VERSION)
        print("upgrade status: " + upgrade["status"])
    except (RuntimeError, OSError) as exc:
        print("upgrade status: INCOMPATIBLE — " + b.clean(exc))


HELP = f"""CX Deck {VERSION}: persistent Codex sessions with a native terminal experience
  codex / cx / cx new        new persistent Codex session here
  cx LABEL                   create or attach a named session
  cx new --split|--tab|--window  open native iTerm2 views
  cx resume                  saved/live picker; cold resume defaults to YOLO
  cx status --json           normalized zmx status
  cx views rebuild|refresh   create missing views or refresh presentation only
  cx config timestamps on|off  native iTerm scrollback timestamps
  cx upgrade [status]        report code/runtime compatibility; never restart implicitly
  cx doctor                  zmx/Codex/iTerm/upgrade diagnostics
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print(VERSION)
        return 0
    if argv and argv[0] in ("help", "--help", "-h"):
        print(HELP)
        return 0
    if not argv:
        start_agent([])
        return 0
    command, rest = argv[0], argv[1:]
    if command in ("resume", "fork"):
        return run_codex([command, *rest])
    if command == "run":
        parser = argparse.ArgumentParser(prog="cx run")
        parser.add_argument("--detach", action="store_true")
        parser.add_argument("args", nargs=argparse.REMAINDER)
        args = parser.parse_args(rest)
        return run_codex(args.args[1:] if args.args[:1] == ["--"] else args.args, args.detach)
    if command in ("dashboard", "status"):
        parser = argparse.ArgumentParser(prog="cx " + command)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--interval", type=float, default=3)
        args = parser.parse_args(rest)
        dashboard(args.interval, args.once or command == "status", args.json)
        return 0
    if command == "project":
        parser = argparse.ArgumentParser(prog="cx project")
        parser.add_argument("--session")
        parser.add_argument("path")
        args = parser.parse_args(rest)
        bind_project(args.path, args.session)
        return 0
    if command in ("attach", "task", "info", "kill", "kill-task"):
        parser = argparse.ArgumentParser(prog="cx " + command)
        parser.add_argument("name")
        row = resolve(parser.parse_args(rest).name)
        if command in ("attach", "task"):
            _attach_or_focus(row, row.get('task') or row['session'])
        elif command == "info":
            print(json.dumps(next(item for item in snapshot()["sessions"]
                                  if item["session"] == row["session"]), indent=2))
        else:
            backend().kill_session(row)
        return 0
    if command == "doctor":
        doctor()
        return 0
    if command == "gpu":
        print(json.dumps(snapshot()["gpu"], indent=2))
        return 0
    if command == "detach":
        backend().detach()
        return 0
    if command not in ("new", "start"):
        rest = argv
    parser = argparse.ArgumentParser(prog="cx start")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("label", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(rest)
    native = args.args[1:] if args.args[:1] == ["--"] else args.args
    start_agent(native, args.label, args.detach)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError) as exc:
        print("cx: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
