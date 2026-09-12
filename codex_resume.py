"""Conversation-first resume picker over saved history and live zmx sessions.

History uses the installed Codex app-server's documented thread/list protocol.
Only metadata is consumed; no SQL schema dependency or raw transcript parser.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import curses
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid

from cx_version import VERSION


class ResumeError(RuntimeError):
    pass


def thread_id(value):
    """Use the canonical ID, never a title, --last, or a fuzzy match."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as e:
        raise ResumeError("Invalid Codex conversation UUID.") from e


def codex_home():
    return str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())


def label(text):
    return "".join(c if c.isprintable() else " " for c in str(text)).replace(";", ",").replace("#{", "{").replace("#(", "(")[:128].strip() or "Untitled conversation"


def chat_name(home, tid):
    digest = hashlib.sha256((os.path.realpath(home) + "\0" + thread_id(tid)).encode()).hexdigest()[:20]
    return "cx-chat-" + digest


class HistoryClient:
    """Short-lived stdio JSON-RPC client; only initialize and thread/list are sent.

    No thread/start, thread/resume, turn/start, approval, or archive request.
    Codex itself may maintain its index or perform its normal startup activity.
    """
    def __init__(self, executable, home, timeout=20):
        self.executable, self.home, self.timeout = executable, home, timeout
        self.process = None
        self.selector = None
        self.buffer = b""
        self.next_id = 0

    def __enter__(self):
        try:
            self.process = subprocess.Popen([self.executable, "app-server"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=dict(os.environ, CODEX_HOME=self.home), start_new_session=True)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.request("initialize", {"clientInfo": {"name": "cx", "title": "CX Deck resume picker", "version": VERSION}})
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.close()
            raise

    def send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            raise ResumeError("Codex history service closed its input.") from e

    def request(self, method, params):
        if method not in ("initialize", "thread/list"):
            raise ResumeError("History client only allows metadata listing.")
        ident = self.next_id
        self.next_id += 1
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError) as e:
                    raise ResumeError("Invalid JSON from Codex app-server; no sessions started.") from e
                if not isinstance(message, dict):
                    raise ResumeError("Invalid Codex app-server response.")
                if message.get("id") != ident:
                    continue  # notifications are not interpreted or logged
                if "error" in message:
                    raise ResumeError("Codex history listing failed: " + label(message["error"]))
                if "result" not in message:
                    raise ResumeError("Codex history response has no result.")
                return message["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise ResumeError("Codex history listing timed out. No sessions started; check 'codex --version' and retry.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ResumeError("Codex app-server exited during history listing. This command requires thread/list support.")
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise ResumeError("Codex metadata response exceeds the 8 MiB safety limit.")

    def close(self):
        if self.selector is not None:
            self.selector.close()
        if self.process is not None:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)  # our own helper group only
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
            if self.process.stdout:
                self.process.stdout.close()

    def __exit__(self, *exc):
        self.close()


def list_history(home, limit=200, client_factory=HistoryClient):
    executable = shutil.which("codex")
    if not executable:
        raise ResumeError("Codex CLI is not on PATH.")
    items, seen, cursor, cursors = {}, 0, None, set()
    with client_factory(executable, home) as client:
        while seen < limit:
            params = {"limit": min(100, limit-seen), "sortKey": "updated_at", "sourceKinds": ["cli"], "archived": False}
            if cursor is not None:
                params["cursor"] = cursor
            result = client.request("thread/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ResumeError("Unsupported thread/list response. No sessions started.")
            for item in result["data"]:
                if not isinstance(item, dict):
                    raise ResumeError("Malformed conversation metadata.")
                tid = thread_id(item.get("id"))
                updated = item.get("updatedAt", item.get("createdAt", 0))
                if not isinstance(updated, (float, int)) or not math.isfinite(updated):
                    updated = 0
                cwd = item.get("cwd")
                items[tid] = dict(key=tid, thread_id=tid, title=label(item.get("name") or item.get("preview") or tid),
                    cwd=cwd if isinstance(cwd, str) else None, updated=updated,
                    home=home, managed=None, state="SAVED", external_pids=[])
            seen += len(result["data"])
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in cursors or not result["data"]:
                raise ResumeError("Invalid/repeated Codex history cursor; refusing an incomplete list.")
            cursors.add(cursor)
    return sorted(items.values(), key=lambda r: r["updated"], reverse=True), cursor is not None


def open_thread_files(pids, home):
    """Best-effort PID -> UUID via open filenames, not command arguments or contents."""
    result = {pid: set() for pid in pids}
    def add(pid, name):
        path = Path(name.removesuffix(" (deleted)"))
        try:
            path.resolve().relative_to(Path(home).resolve() / "sessions")
        except (ValueError, OSError):
            return
        match = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$", path.name)
        if match and pid in result:
            result[pid].add(thread_id(match.group(1)))
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    try:
                        add(pid, os.readlink(fd))
                    except OSError:
                        pass
            except OSError:
                pass
    elif pids and shutil.which("lsof"):
        try:
            p = subprocess.run(["lsof", "-n", "-P", "-a", "-p", ",".join(map(str, pids)), "-Fpn"],
                capture_output=True, text=True, errors="replace", timeout=5)
            pid = None
            for line in p.stdout.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    pid = int(line[1:])
                elif line.startswith("n"):
                    add(pid, line[1:])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def inventory(history, home, cx):
    data = cx.snapshot()
    rows = {r["key"]: dict(r) for r in history}
    all_pids = sorted({p for s in data["sessions"] for p in s["codex_pids"]} | set(data["outside_managed_codex_pids"] or []))
    opened = open_thread_files(all_pids, home)
    mapped = set()
    for s in data["sessions"]:
        tid, shome = s.get("thread_id"), s.get("codex_home")
        found = set().union(*(opened.get(p, set()) for p in s["codex_pids"]))
        if len(found) == 1:
            # An observed live ID supersedes stale metadata (manual /resume).
            tid, shome = next(iter(found)), home
        if tid and shome and os.path.realpath(shome) == home:
            tid = thread_id(tid)
            r = rows.setdefault(tid, dict(key=tid, thread_id=tid, title=s["task"], cwd=s.get("launch_cwd") or s["root"],
                updated=s["created"], home=home, managed=None, external_pids=[]))
            if r.get("managed"):
                raise ResumeError("Two managed sessions claim the same conversation. Inspect cxl; nothing was resumed.")
            r.update(managed=s, state=s["state"])
            mapped.update(s["codex_pids"])
        else:
            key = "zmx:" + s["session"]
            rows[key] = dict(key=key, thread_id=None, title=s["task"], cwd=s.get("launch_cwd") or s["root"], updated=s["created"],
                home=home, managed=s, state=s["state"], external_pids=[])
    for pid in data["outside_managed_codex_pids"] or []:
        for tid in opened.get(pid, set()):
            r = rows.setdefault(tid, dict(key=tid, thread_id=tid, title=tid, cwd=None,
                updated=0, home=home, managed=None, state="LIVE-OUTSIDE", external_pids=[]))
            r["external_pids"].append(pid)
            r["state"] = "LIVE-OUTSIDE"
        if opened.get(pid):
            mapped.add(pid)
    unknown = sorted(set(all_pids)-mapped)
    warnings = list(data["warnings"])
    if data["outside_managed_codex_pids"] is None:
        warnings.append("Process inspection failed; cold resume is blocked.")
    if unknown:
        warnings.append(f"Cannot associate Codex PIDs {unknown} with saved IDs; cold resume is blocked by default.")
    return sorted(rows.values(), key=lambda r: (not bool(r.get("managed")), -r["updated"])), unknown, warnings, data["outside_managed_codex_pids"] is None


def clip(text, width):
    out, used = "", 0
    for c in str(text):
        c = c if c.isprintable() else "?"
        size = 0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
        if used + size > width:
            break
        used += size
        out += c
    return out


def choose(rows, warnings=()):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ResumeError("The picker needs a terminal; use 'cx resume --list' or '--select <exact-id> ...'.")
    def screen(stdscr):
        selected, position, query, filtering = set(), 0, "", False
        stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            visible = [r for r in rows if query.casefold() in (r["title"] + " " + r["key"] + " " + str(r["cwd"])).casefold()]
            position = min(position, max(0, len(visible)-1))
            height, width = stdscr.getmaxyx()
            def put(y, text, attr=0):
                if 0 <= y < height and width > 1:
                    try:
                        stdscr.addstr(y, 0, clip(text, width-1), attr)
                    except curses.error:
                        pass
            stdscr.erase()
            put(0, f"cx resume | {len(rows)} conversations/sessions | {len(selected)} selected", curses.A_BOLD)
            put(1, "Up/Down or j/k: move | Space: select | /: search | a: select visible | Enter: open | q: cancel")
            put(2, "All launch directories. LIVE = reattach; SAVED = resume original ID. No automatic prompt is sent.")
            put(3, "Search: " + query + ("_" if filtering else ""))
            page = max(1, height-7)
            offset = (position//page)*page
            for index, r in enumerate(visible[offset:offset+page], offset):
                check = "[x]" if r["key"] in selected else "[ ]"
                ident = (r["thread_id"] or r["key"])[-12:]
                try:
                    when = time.strftime("%m-%d %H:%M", time.localtime(r["updated"]))
                except (OverflowError, OSError, ValueError):
                    when = "unknown date"
                text = f"{check} {r['state']:12} {when} {ident}  {r['title']}  [{r['cwd'] or 'cwd unknown'}]"
                put(4+index-offset, text, curses.A_REVERSE if index == position else 0)
            put(height-2, warnings[0] if warnings else "Enter with no selections does nothing. Live external sessions must be stopped before resuming.")
            put(height-1, "Search mode: type, Backspace; Enter/Esc finishes search" if filtering else "Selected conversations get persistent zmx PTYs and native iTerm2 splits; this pane becomes the dashboard.")
            stdscr.refresh()
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if filtering:
                if key in ("\n", "\r", "\x1b"):
                    filtering = False
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    query = query[:-1]
                    position = 0
                elif isinstance(key, str) and key.isprintable():
                    query += key
                    position = 0
                continue
            if key in ("q", "\x1b"):
                return []
            if key in ("\n", "\r", curses.KEY_ENTER):
                return [r for r in rows if r["key"] in selected]
            if key in (curses.KEY_DOWN, "j"):
                position = min(position+1, max(0, len(visible)-1))
            elif key in (curses.KEY_UP, "k"):
                position = max(0, position-1)
            elif key == " " and visible:
                k = visible[position]["key"]
                selected.symmetric_difference_update({k})
            elif key == "a":
                keys = {r["key"] for r in visible}
                selected = selected-keys if keys <= selected else selected | keys
            elif key == "/":
                filtering = True
    try:
        return curses.wrapper(screen)
    except curses.error as e:
        raise ResumeError("Cannot initialize the terminal picker; use cx resume --list or an interactive terminal.") from e


@contextmanager
def launch_lock(home):
    from cx_paths import state_home
    directory = state_home()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(home.encode()).hexdigest()[:20] + ".resume.lock"
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ResumeError("Another cx resume is opening sessions; no duplicate launcher was started.") from e
        yield
    finally:
        os.close(fd)


def planned_cwd(row, override=None):
    cwd = override if override is not None else row.get("cwd")
    if not cwd or not os.path.isabs(os.path.expanduser(cwd)):
        raise ResumeError("Saved launch directory is missing/relative; specify --cwd explicitly. No repo is guessed.")
    path = str(Path(cwd).expanduser().resolve())
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or ";" in path or "#{" in path or "#(" in path:
        raise ResumeError("Unsafe launch-directory characters; choose --cwd explicitly.")
    if not Path(path).is_dir():
        raise ResumeError(f"Saved launch directory no longer exists: {path}. Use --cwd explicitly.")
    return path


def ensure(row, cx, override=None, yolo=True):
    """Called under launch_lock after rechecking inventory. Never kill or send keys."""
    existing = row.get("managed")
    if existing and existing["state"] in ("ALIVE", "STOPPED"):
        return existing, "reuse live zmx session"
    if existing:
        raise ResumeError(f"Managed zmx session state is {existing['state']}; refusing to start a duplicate generation.")
    if not row["thread_id"]:
        raise ResumeError("No saved conversation ID to resume.")
    root = planned_cwd(row, override)
    tid, home = thread_id(row["thread_id"]), os.path.realpath(row["home"])
    cx.validate(home)
    name = existing["session"] if existing else chat_name(home, tid)
    if not existing and any(s["session"] == name for s in cx.sessions()):
        raise ResumeError("Conversation session name exists without matching metadata. Inspect cxl; nothing was replaced.")
    executable = shutil.which("codex")
    if not executable:
        raise ResumeError("codex is not on PATH.")
    info = cx.preflight()
    codex_argv = [executable]
    if yolo:
        codex_argv.append("--yolo")
    codex_argv += ["resume", tid, "--cd", root]
    labels = {"cx_managed": "1", "cx_version": cx.VERSION,
              "cx_thread_id": tid, "cx_codex_home": cx.encode_path(home),
              "cx_launch_policy": "yolo" if yolo else "safe", "cx_launch_mode": "resume"}
    environment = {"PATH": os.environ.get("PATH", ""), "CODEX_HOME": home,
                   "CX_MANAGED": "1", "CX_AGENT_NAME": name,
                   "CX_CONSOLE": str(Path(__file__).resolve().with_name("agent_console.py"))}
    try:
        created = cx.create(name, codex_argv, root, labels, detached=True,
                            binary=info["path"], env=environment)
    except cx.Error as e:
        raise ResumeError(f"Could not launch verified zmx resume for {tid}: {e}. Nothing was killed.") from e
    if getattr(cx, "store", None):
        from cx_store import live_key, thread_key
        context = cx.snapshot()["context"]
        cx.store.annotate([live_key(created, context), thread_key(home, tid, context["host"])],
                          name=label(row["title"]), launch_cwd=root)
    return created, "resume launched in zmx"


def add_launch_policy_arguments(parser):
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--yolo", dest="yolo", action="store_true",
                        help="cold resumes bypass approvals and sandboxing (default)")
    policy.add_argument("--safe", dest="yolo", action="store_false",
                        help="cold resumes use the configured Codex safety policy")
    policy.add_argument("--no-yolo", dest="yolo", action="store_false",
                        help="synonym for --safe")
    parser.set_defaults(yolo=True)


def add_arguments(sub):
    p = sub.add_parser("resume", help="multi-select saved conversations globally and open iTerm2 splits")
    p.add_argument("--list", action="store_true", help="list only; no picker or launches")
    p.add_argument("--json", action="store_true", help="list metadata as JSON; no launches")
    p.add_argument("--limit", type=int, default=200, help="maximum saved CLI conversations to load (default 200)")
    p.add_argument("--select", nargs="+", metavar="ID", help="exact UUIDs or zmx:<full-session-name>; skip picker")
    p.add_argument("--group", help="filter by an exact local group name/ID or @ungrouped")
    p.add_argument("--cwd", help="explicit override for saved launch directory; never auto-select a repo")
    p.add_argument("--per-tab", type=int, default=0, help="optional panes per tab; 0 = fit to pane dimensions (no total session cap)")
    p.add_argument("--min-columns", type=int, default=70, help="minimum pane width for automatic splits")
    p.add_argument("--min-rows", type=int, default=12, help="minimum pane height for automatic splits")
    p.add_argument("--no-iterm", action="store_true", help="start/keep detached zmx sessions only")
    p.add_argument("--no-dashboard", action="store_true", help="return after opening the selected sessions")
    p.add_argument("--allow-unverified-live", action="store_true", help="explicitly accept duplicate risk from unidentified running Codex PIDs")
    add_launch_policy_arguments(p)


def execute(args, cx):
    if not 1 <= args.limit <= 2000:
        raise ResumeError("--limit must be between 1 and 2000.")
    if args.per_tab < 0 or args.min_columns < 20 or args.min_rows < 5:
        raise ResumeError("Layout needs --per-tab >= 0, --min-columns >= 20, --min-rows >= 5.")
    home = codex_home()
    history_warning = None
    try:
        history, truncated = list_history(home, args.limit)
    except ResumeError as e:
        history, truncated, history_warning = [], False, str(e)
    rows, unknown, warnings, process_unknown = inventory(history, home, cx)
    if getattr(args, "group", None):
        from cx_store import thread_key
        if not getattr(cx, "store", None):
            raise ResumeError("Group filtering requires the cx workbench store.")
        group_id = None if args.group == "@ungrouped" else cx.store.resolve_group(args.group)[0]
        state = cx.store.read()
        rows = [row for row in rows if state["agents"].get(
            thread_key(row.get("home"), row.get("thread_id"), socket.gethostname()), {}).get("group_id") == group_id]
    if history_warning:
        if not rows:
            raise ResumeError(history_warning)
        warnings.insert(0, "Saved history unavailable; showing managed sessions only: " + history_warning)
    if truncated:
        warnings.append(f"History capped at {args.limit} entries; increase --limit to search older conversations.")
    if args.json:
        print(json.dumps(dict(codex_home=home, rows=rows, warnings=warnings, truncated=truncated), indent=2, ensure_ascii=True))
        return
    if args.list:
        for row in rows:
            print(f"{row['key']}  {row['state']:12}  {row['title']}  [{cx.clean(row['cwd'] or 'cwd unknown')}]")
        for w in warnings:
            print("WARNING: " + cx.clean(w))
        return
    if args.select:
        by_key = {r["key"]: r for r in rows}
        keys = list(dict.fromkeys(args.select))
        missing = [k for k in keys if k not in by_key]
        if missing:
            raise ResumeError("Exact selection not in loaded history/sessions: " + ", ".join(missing) + ". Use --list or increase --limit.")
        chosen = [by_key[k] for k in keys]
    else:
        chosen = choose(rows, warnings) if rows else []
    if not chosen:
        print("No sessions selected; nothing started.")
        return
    if not args.no_iterm:
        raise ResumeError("Native iTerm presentation must use the cx workbench route; no session started.")
    with launch_lock(home):
        # Refresh liveness after selection/permission prompts; never trust old PIDs.
        fresh, unknown, _, process_unknown = inventory(history, home, cx)
        index = {r["key"]: r for r in fresh}
        chosen = [index.get(r["key"]) for r in chosen]
        if any(r is None for r in chosen):
            raise ResumeError("A selected session disappeared. Retry; nothing started.")
        cold = [r for r in chosen if r["thread_id"] and (not r.get("managed") or r["managed"]["state"] not in ("ALIVE", "STOPPED"))]
        if any(r["external_pids"] for r in chosen):
            raise ResumeError("A selected conversation has a known external live Codex PID. Exit that exact copy before resuming it.")
        if cold and (unknown or process_unknown) and not args.allow_unverified_live:
            raise ResumeError(f"Unidentified live Codex PIDs {unknown or 'UNKNOWN'} could duplicate a cold resume because their UUIDs cannot be verified. Inspect cx doctor, stop them, or pass --allow-unverified-live to accept that specific uncertainty.")
        for row in cold:
            planned_cwd(row, args.cwd)  # validate the full batch before launching
        attached, failures = [], []
        for row in chosen:
            try:
                s, action = ensure(row, cx, args.cwd, yolo=args.yolo)
                attached.append(s)
                print(f"{action}: {cx.clean(row['title'])}\n  {s['session']}")
            except (ResumeError, cx.Error, OSError) as e:
                failures.append(str(e))
        for s in attached:
            print("Attach: cxa " + shlex.quote(s["session"]))
        if failures:
            raise ResumeError("Partial launch; successful sessions were retained. " + " | ".join(failures))
    if not args.no_dashboard:
        cx.dashboard()
