#!/usr/bin/env python3
"""Narrow zmx persistence provider for cx.

Only version/preflight, list, attach/create, detach, labels, and explicit kill
are exposed. Commands are argv arrays and terminal output is never scraped.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
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
from collections import Counter

from cx_version import VERSION
MIN_VERSION = (0, 8, 1)
MANAGED_PREFIXES = ("cx-agent-", "cx-chat-")
SAFE_SESSION = re.compile(r"cx-(?:agent|chat)-[A-Za-z0-9_-]{1,72}\Z")
SAFE_LABEL = re.compile(r"[A-Za-z0-9_.-]*\Z")


class Error(RuntimeError):
    pass


def clean(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


def validate(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Error("Values must be nonempty and cannot contain terminal control characters.")
    return value


def optional(call, default=None):
    try:
        return call()
    except Error:
        return default


def run(argv, timeout=5, env=None, cwd=None):
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) for x in argv):
        raise Error("Subprocess commands must be nonempty argv arrays.")
    try:
        result = subprocess.run(list(argv), text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            env=dict(os.environ) if env is None else env, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(str(exc)) from exc
    if result.returncode:
        raise Error(result.stderr.strip() or result.stdout.strip() or
                    f"{Path(argv[0]).name}: exit {result.returncode}")
    return result.stdout


def git(root, *args):
    return run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-C", str(root), *args])


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _client_env(extra=None, *, preserve_session=False):
    env = dict(os.environ)
    if not preserve_session:
        env.pop("ZMX_SESSION", None)
    env["ZMX_NO_DETACH_KEY"] = "1"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def zmx_path():
    return shutil.which("zmx")


def parse_version(text):
    fields = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            fields[parts[0].strip()] = parts[-1].strip()
    value = fields.get("zmx")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value or "")
    if not match or not fields.get("socket_dir"):
        raise Error("Unrecognized 'zmx version' response.")
    fields["version"] = value.removeprefix("v")
    fields["version_tuple"] = tuple(map(int, match.groups()))
    fields["socket_dir"] = os.path.realpath(fields["socket_dir"])
    return fields


def version(binary=None, env=None):
    binary = binary or zmx_path()
    if not binary:
        raise Error("zmx is not installed. Install explicitly with: brew install neurosnap/tap/zmx")
    return parse_version(run([binary, "version"], env=_client_env(env)))


def preflight(binary=None, env=None):
    info = version(binary, env)
    if info["version_tuple"] < MIN_VERSION:
        raise Error(f"zmx >= {'.'.join(map(str, MIN_VERSION))} is required; found {info['version']}.")
    info["path"] = str(Path(binary or zmx_path()).resolve())
    return info


def runtime_dir(binary=None, env=None):
    return preflight(binary, env)["socket_dir"]


def encode_path(path):
    raw = os.path.realpath(os.path.expanduser(path)).encode("utf-8")
    return "b64_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_path(value):
    if not isinstance(value, str) or not value.startswith("b64_") or not SAFE_LABEL.fullmatch(value):
        raise Error("Malformed encoded cx path label.")
    try:
        raw = base64.urlsafe_b64decode(value[4:] + "=" * (-len(value[4:]) % 4)).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise Error("Malformed encoded cx path label.") from exc
    if not os.path.isabs(raw):
        raise Error("Encoded cx path label is not absolute.")
    return os.path.realpath(raw)


def serialize_labels(labels):
    parts = []
    for key in sorted(labels):
        value = str(labels[key])
        if not key or not SAFE_LABEL.fullmatch(key) or not SAFE_LABEL.fullmatch(value):
            raise Error(f"Label {clean(key)!r} cannot be represented by zmx 0.8.1.")
        parts.append(key + "=" + value)
    return " ".join(parts)


def parse_labels(text):
    labels = {}
    for item in text.split():
        if item.count("=") != 1:
            raise Error("Malformed zmx label response.")
        key, value = item.split("=", 1)
        if not key or not SAFE_LABEL.fullmatch(key) or not SAFE_LABEL.fullmatch(value) or key in labels:
            raise Error("Malformed zmx label response.")
        labels[key] = value
    return labels


def _zmx(binary, *args, timeout=5, env=None, cwd=None):
    executable = binary or zmx_path()
    if not executable:
        raise Error("zmx is not installed. Install explicitly with: brew install neurosnap/tap/zmx")
    return run([executable, *args], timeout=timeout, env=_client_env(env), cwd=cwd)


def get_labels(name, binary=None, env=None):
    validate_session_name(name)
    return parse_labels(_zmx(binary, "get", name, env=env))


def set_labels(name, labels, binary=None, env=None):
    validate_session_name(name)
    if labels:
        serialize_labels(labels)
        _zmx(binary, "set", name,
             *[key + "=" + str(value) for key, value in sorted(labels.items())], env=env)


def validate_session_name(name):
    if not SAFE_SESSION.fullmatch(str(name)):
        raise Error("Invalid cx zmx session name.")
    return str(name)


def parse_list(text, runtime, host=None):
    host = host or socket.gethostname()
    rows = []
    for raw in text.splitlines():
        fields = raw.removeprefix("→ ").lstrip().split("\t")
        if not fields or not fields[0].startswith("name="):
            continue
        name = fields[0][5:]
        if not any(name.startswith(prefix) for prefix in MANAGED_PREFIXES):
            continue
        validate_session_name(name)
        values = {"name": name}
        for field in fields[1:]:
            if "=" not in field:
                raise Error(f"Unrecognized zmx session record for {clean(name)}.")
            key, value = field.split("=", 1)
            if key in values:
                raise Error(f"Duplicate zmx field {clean(key)} for {clean(name)}.")
            values[key] = value
        try:
            pid, clients, created = int(values["pid"]), int(values["clients"]), int(values["created"])
        except (KeyError, ValueError) as exc:
            raise Error(f"Incomplete zmx generation record for {clean(name)}.") from exc
        if pid <= 0 or clients < 0 or created <= 0:
            raise Error(f"Invalid zmx generation record for {clean(name)}.")
        labels = {key: value for key, value in values.items() if key.startswith("cx_")}
        if labels.get("cx_managed") != "1":
            continue
        try:
            home = decode_path(labels["cx_codex_home"]) if labels.get("cx_codex_home") else ""
            launch_cwd = decode_path(labels["cx_launch_cwd"]) if labels.get("cx_launch_cwd") else ""
        except Error as exc:
            raise Error(f"Malformed cx labels on {clean(name)}: {exc}") from exc
        tid = labels.get("cx_thread_id") or ""
        if tid:
            import uuid
            try:
                tid = str(uuid.UUID(tid))
            except (ValueError, AttributeError) as exc:
                raise Error(f"Malformed cx_thread_id on {clean(name)}.") from exc
        generation = dict(host=host, runtime_dir=os.path.realpath(runtime), session=name,
                          daemon_pid=pid, created=created)
        rows.append(dict(backend="zmx", runtime_version=labels.get("cx_zmx_version", "UNKNOWN"),
            session=name, sid=name, daemon_pid=pid, created=created, attached=clients,
            generation=generation, labels=labels, thread_id=tid, codex_home=home,
            cx_version=labels.get("cx_version", "UNKNOWN"),
            launch_policy=labels.get("cx_launch_policy", "UNKNOWN"),
            launch_mode=labels.get("cx_launch_mode", "UNKNOWN"), launch_cwd=launch_cwd,
            root="", task=name, repo="?", cwd=values.get("cwd"), command=values.get("cmd")))
    return sorted(rows, key=lambda row: row["session"])


def parse_session_names(text):
    """Return every exact zmx name so unmanaged collisions also fail closed."""
    names = set()
    for raw in text.splitlines():
        first = raw.removeprefix("→ ").lstrip().split("\t", 1)[0]
        if not first.startswith("name="):
            continue
        name = first[5:]
        if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise Error("Malformed zmx session name in list response.")
        names.add(name)
    return names


def list_sessions(binary=None, env=None, info=None):
    info = info or preflight(binary, env)
    rows = parse_list(_zmx(info["path"], "list", env=env), info["socket_dir"])
    for row in rows:
        row["runtime_version"] = info["version"]
    return rows


def sessions():
    return list_sessions()


def normalize_generation(row):
    generation = row.get("generation") or {}
    required = ("host", "runtime_dir", "session", "daemon_pid", "created")
    if any(key not in generation for key in required):
        raise Error("Incomplete zmx live-generation identity.")
    return (generation["host"], os.path.realpath(generation["runtime_dir"]),
            generation["session"], int(generation["daemon_pid"]), int(generation["created"]))


def generation_token(row):
    payload = json.dumps(normalize_generation(row), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def exact_session(name, generation=None, binary=None, env=None):
    name = validate_session_name(name)
    matches = [row for row in list_sessions(binary, env) if row["session"] == name]
    if not matches:
        raise Error("No managed zmx session with this exact name: " + clean(name))
    row = matches[0]
    if generation is not None and normalize_generation(row) != tuple(generation):
        raise Error("The zmx session generation changed; refusing to reuse its name.")
    return row


def _wait_created(name, binary, env, timeout=8):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            return exact_session(name, binary=binary, env=env)
        except Error as exc:
            last = exc
            time.sleep(0.05)
    raise Error(f"zmx session {clean(name)} was not observable after creation: {last}")


def create(name, command, cwd, labels, *, detached=False, binary=None, env=None):
    """Create a direct persistent PTY without zmx's shell-command runner."""
    name = validate_session_name(name)
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(x, str) for x in command):
        raise Error("The zmx child command must be a nonempty argv array.")
    cwd = os.path.realpath(cwd)
    if not os.path.isdir(cwd):
        raise Error("Launch directory does not exist: " + clean(cwd))
    info = preflight(binary, env)
    if name in parse_session_names(_zmx(info["path"], "list", env=env)):
        raise Error("A zmx session with that exact name already exists; no command was started.")
    values = dict(labels)
    values.setdefault("cx_managed", "1")
    values.setdefault("cx_version", VERSION)
    values.setdefault("cx_zmx_version", info["version"])
    values.setdefault("cx_launch_cwd", encode_path(cwd))
    argv = [info["path"], "attach", "--labels", serialize_labels(values), name, *command]
    child_env = _client_env(env)
    if detached:
        try:
            result = subprocess.run(argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            result = None
        except OSError as exc:
            raise Error(str(exc)) from exc
        if result is not None and result.returncode:
            raise Error(result.stderr.strip() or f"zmx attach: exit {result.returncode}")
        return _wait_created(name, info["path"], env)
    try:
        code = subprocess.call(argv, cwd=cwd, env=child_env)
    except OSError as exc:
        raise Error(str(exc)) from exc
    if code:
        raise Error(f"zmx attach failed ({code}); no session was killed.")
    return optional(lambda: exact_session(name, binary=info["path"], env=env))


def attach_argv(row, binary=None, env=None):
    info = preflight(binary, env)
    expected = normalize_generation(row)
    current = exact_session(row["session"], binary=info["path"], env=env)
    if normalize_generation(current) != expected:
        raise Error("The zmx generation changed; refusing to attach by name alone.")
    return [info["path"], "attach", row["session"]]


def attach(row):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Error("Attach requires an interactive terminal. Use cxl for status.")
    code = subprocess.call(attach_argv(row), env=_client_env({"CX_ZMX_GENERATION": generation_token(row)}))
    if code:
        raise Error(f"zmx attach failed ({code}); the session was not killed.")


def detach():
    if not os.environ.get("ZMX_SESSION"):
        raise Error("Not inside a zmx session.")
    executable = zmx_path()
    if not executable:
        raise Error("zmx is not on PATH.")
    code = subprocess.call([executable, "detach"], env=_client_env(preserve_session=True))
    if code:
        raise Error(f"zmx detach failed ({code}).")


def kill_session(row):
    if not sys.stdin.isatty():
        raise Error("Refusing non-interactive kill; use an interactive terminal.")
    expected = normalize_generation(row)
    print("This terminates the zmx session and its Codex process: " + row["session"])
    if input("Type the full session name to confirm: ") != row["session"]:
        print("Cancelled.")
        return
    current = exact_session(row["session"], generation=expected)
    _zmx(None, "kill", current["session"])
    print("Killed: " + current["session"])


def parse_ps(text):
    result = {}
    for line in text.splitlines():
        fields = line.strip().split(None, 7)
        if len(fields) != 8:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
            result[pid] = dict(pid=pid, ppid=ppid, cpu=float(fields[2]), rss_kib=int(fields[3]),
                elapsed=fields[4], stat=fields[5], tty=fields[6], executable=fields[7])
        except ValueError:
            continue
    return result


def processes():
    parsed = parse_ps(run(["ps", "-axo", "pid=,ppid=,pcpu=,rss=,etime=,stat=,tty=,comm="]))
    if not parsed:
        raise Error("ps returned no parseable processes; liveness is unknown.")
    return parsed


def descendants(roots, procs):
    children = {}
    for pid, process in procs.items():
        children.setdefault(process["ppid"], []).append(pid)
    found, pending = set(), list(roots)
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, ()))
    return found.intersection(procs)


def is_codex(process):
    if process["stat"].startswith("Z"):
        return False
    name = Path(process["executable"]).name
    pattern = r"codex(?:-(?:aarch64|x86_64)-(?:apple-darwin|unknown-linux-(?:musl|gnu)))?"
    if re.fullmatch(pattern, name):
        return True
    if sys.platform.startswith("linux") and name.startswith("codex-"):
        try:
            actual = Path(os.readlink(f"/proc/{process['pid']}/exe")).name.removesuffix(" (deleted)")
            return bool(re.fullmatch(pattern, actual))
        except OSError:
            return False
    return False


def git_state(root):
    output = dict(branch=None, dirty=None, commit=None, git_error=None)
    if not root:
        output["git_error"] = "missing repository metadata"
        return output
    try:
        git(root, "rev-parse", "--show-toplevel")
        branch = optional(lambda: git(root, "symbolic-ref", "--quiet", "--short", "HEAD").strip())
        sha = optional(lambda: git(root, "rev-parse", "--short", "HEAD").strip())
        output["branch"] = branch or (f"DETACHED@{sha}" if sha else "unborn")
        output["dirty"] = bool(git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal"))
        last = optional(lambda: git(root, "log", "-1", "--format=%h%x00%cs%x00%s").rstrip("\n"))
        if last:
            commit, date, subject = last.split("\0", 2)
            output["commit"] = dict(sha=commit, date=date, subject=subject)
    except (Error, ValueError) as exc:
        output["git_error"] = str(exc)
    return output


def gpu_state():
    output = dict(status="unavailable", scope="local NVIDIA host only", devices=[], jobs=[], error=None)
    if not shutil.which("nvidia-smi"):
        output["error"] = "nvidia-smi not installed; Apple/remote GPUs are not monitored"
        return output
    try:
        rows = run(["nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits"], timeout=3)
        for row in csv.reader(io.StringIO(rows)):
            if len(row) == 5:
                ident, name, util, used, total = [value.strip() for value in row]
                output["devices"].append(dict(uuid=ident, name=name, utilization=number(util),
                    memory_used_mib=number(used), memory_total_mib=number(total)))
        rows = run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits"], timeout=3)
        for row in csv.reader(io.StringIO(rows)):
            if len(row) == 4 and row[1].strip().isdigit():
                ident, pid, name, memory = [value.strip() for value in row]
                output["jobs"].append(dict(gpu_uuid=ident, pid=int(pid), name=name,
                    memory_mib=number(memory), sessions=[]))
        output["status"] = "available"
    except Error as exc:
        output["error"] = str(exc)
    return output


def _bind_threads(records, procs, warnings):
    try:
        from codex_resume import open_thread_files
    except ImportError:
        return
    claims = {(row["codex_home"], row["thread_id"]): row["session"] for row in records
              if row["codex_home"] and row["thread_id"]}
    for row in records:
        if row["thread_id"] or not row["codex_home"] or not row.get("codex_pids"):
            continue
        observed = open_thread_files(row["codex_pids"], row["codex_home"])
        tids = {tid for values in observed.values() for tid in values}
        if len(tids) != 1:
            continue
        tid = tids.pop()
        owner = claims.get((row["codex_home"], tid))
        if owner and owner != row["session"]:
            warnings.append(f"Refused to bind {row['session']}: conversation {tid} is already live in {owner}.")
            continue
        try:
            set_labels(row["session"], {"cx_thread_id": tid})
        except Error as exc:
            warnings.append(f"Could not bind discovered conversation ID for {row['session']}: {exc}")
            continue
        row["thread_id"] = tid
        row["labels"]["cx_thread_id"] = tid
        claims[(row["codex_home"], tid)] = row["session"]


def snapshot(bind_threads=True):
    info = preflight()
    records = list_sessions(info["path"], info=info)
    warnings = []
    try:
        procs = processes()
    except Error as exc:
        procs = None
        warnings.append(str(exc))
    gpu, git_cache = gpu_state(), {}
    for row in records:
        tree = descendants([row["daemon_pid"]], procs) if procs is not None else set()
        living = {pid for pid in tree if not procs[pid]["stat"].startswith("Z")} if procs is not None else set()
        codex = sorted(pid for pid in living if is_codex(procs[pid])) if procs is not None else []
        state = "ALIVE" if codex else "NO_CODEX"
        if codex and all(procs[pid]["stat"].startswith("T") for pid in codex):
            state = "STOPPED"
        if procs is None or row["daemon_pid"] not in procs:
            state = "UNKNOWN"
        row.update(state=state, codex_pids=codex, process_pids=sorted(living),
            cpu_tree_pct=round(sum(procs[pid]["cpu"] for pid in living), 1) if procs is not None else None,
            rss_tree_mib=round(sum(procs[pid]["rss_kib"] for pid in living) / 1024, 1) if procs is not None else None,
            elapsed=procs[codex[0]]["elapsed"] if codex and procs is not None else None)
        root = row.get("root", "")
        if root not in git_cache:
            git_cache[root] = git_state(root)
        row.update(git_cache[root])
        row["gpu_jobs"] = []
        for job in gpu["jobs"]:
            if job["pid"] in living:
                job["sessions"].append(row["session"])
                row["gpu_jobs"].append({key: value for key, value in job.items() if key != "sessions"})
    if bind_threads and procs is not None:
        _bind_threads(records, procs, warnings)
    counts = Counter(os.path.realpath(row["root"]) for row in records if row["root"])
    for row in records:
        row["shared_worktree"] = bool(row["root"] and counts[os.path.realpath(row["root"])] > 1)
    owned = {pid for row in records for pid in row["process_pids"]}
    outside = sorted(pid for pid, process in (procs or {}).items()
                     if is_codex(process) and pid not in owned) if procs is not None else None
    return dict(version=VERSION, backend="zmx", runtime_version=info["version"],
        host=socket.gethostname(), timestamp=time.time(), sessions=records, gpu=gpu,
        outside_managed_codex_pids=outside, warnings=warnings,
        context=dict(backend="zmx", host=socket.gethostname(), runtime_dir=info["socket_dir"],
                     runtime_path=info["path"], runtime_version=info["version"]))


def client_ttys(row, procs=None):
    """Verify attach clients by exact-generation token and controlling TTY."""
    procs = procs or processes()
    candidates = [process for process in procs.values()
                  if Path(process["executable"]).name == "zmx" and process["tty"] not in ("?", "??", "-")]
    token, matched = generation_token(row), set()
    if sys.platform.startswith("linux"):
        for process in candidates:
            try:
                environment = Path(f"/proc/{process['pid']}/environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if ("CX_ZMX_GENERATION=" + token).encode() in environment:
                matched.add("/dev/" + process["tty"].removeprefix("/dev/"))
        return matched
    marker = "CX_ZMX_GENERATION=" + token
    for process in candidates:
        try:
            output = run(["ps", "eww", "-p", str(process["pid"]), "-o", "command="], timeout=3)
        except Error:
            continue
        if re.search(r"(?:^|\s)" + re.escape(marker) + r"(?:\s|$)", output):
            matched.add("/dev/" + process["tty"].removeprefix("/dev/"))
    return matched


def _attach_verified(argv):
    parser = argparse.ArgumentParser(prog="cx_zmx.py attach-verified")
    parser.add_argument("binary")
    parser.add_argument("runtime_dir")
    parser.add_argument("session")
    parser.add_argument("daemon_pid", type=int)
    parser.add_argument("created", type=int)
    args = parser.parse_args(argv)
    requested_runtime = os.path.realpath(args.runtime_dir)
    env = _client_env({"ZMX_DIR": requested_runtime})
    info = preflight(binary=args.binary, env=env)
    expected = (socket.gethostname(), os.path.realpath(args.runtime_dir), args.session,
                args.daemon_pid, args.created)
    if info["socket_dir"] != expected[1]:
        raise Error("Configured zmx runtime differs from the verified target; refusing to attach.")
    row = exact_session(args.session, generation=expected, binary=info["path"], env=env)
    env["CX_ZMX_GENERATION"] = generation_token(row)
    os.execve(info["path"], [info["path"], "attach", row["session"]], env)
    return 127


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["attach-verified"]:
        return _attach_verified(argv[1:])
    raise Error("cx_zmx.py is an internal provider; use cx.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Error, OSError) as exc:
        print("cx: " + clean(exc), file=sys.stderr)
        raise SystemExit(1)
