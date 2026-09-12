#!/usr/bin/env python3
"""Compatibility reporting for cx-created zmx runtime generations."""
from __future__ import annotations

import argparse
from functools import total_ordering
import json
import re

from cx_version import VERSION
MIN_RUNTIME_CX_VERSION = "0.6.0"

# Each entry is (controller version introducing a runtime requirement,
# minimum cx version that may have created the session). There are no runtime
# regeneration boundaries through v0.7; add one only when a future release truly
# requires a controlled replacement generation.
RUNTIME_REQUIREMENTS = ()
STATES = ("CURRENT", "UPGRADE_AVAILABLE", "UPGRADE_REQUIRED", "INCOMPATIBLE")
SEMVER = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)


class UpgradeError(RuntimeError):
    pass


@total_ordering
class SemanticVersion:
    def __init__(self, value):
        match = SEMVER.fullmatch(str(value))
        if not match:
            raise ValueError("invalid semantic version")
        self.text = str(value).removeprefix("v")
        self.core = tuple(map(int, match.group(1, 2, 3)))
        prerelease = match.group(4)
        self.prerelease = tuple(prerelease.split(".")) if prerelease else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0")
               for part in self.prerelease):
            raise ValueError("invalid semantic version")

    def __eq__(self, other):
        return self.core == other.core and self.prerelease == other.prerelease

    def __lt__(self, other):
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_number, right_number = left.isdigit(), right.isdigit()
            if left_number and right_number:
                return int(left) < int(right)
            if left_number != right_number:
                return left_number
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def parse_version(value):
    """Parse SemVer for ordering; malformed or missing labels fail closed."""
    return SemanticVersion(value)


def runtime_compatibility(session_version, installed_version=VERSION, requirements=None):
    """Return compatibility without changing a running session or its labels."""
    try:
        session = parse_version(session_version)
        installed = parse_version(installed_version)
        minimum = parse_version(MIN_RUNTIME_CX_VERSION)
    except (TypeError, ValueError):
        return "INCOMPATIBLE"
    if session < minimum or installed < session:
        return "INCOMPATIBLE"
    rules = RUNTIME_REQUIREMENTS if requirements is None else requirements
    try:
        for controller_since, minimum_session in rules:
            if installed >= parse_version(controller_since) and session < parse_version(minimum_session):
                return "UPGRADE_REQUIRED"
    except (TypeError, ValueError):
        raise UpgradeError("Invalid code-defined runtime compatibility boundary.")
    return "CURRENT" if session == installed else "UPGRADE_AVAILABLE"


def _failure(installed, minimum, version, error):
    return dict(installed_cx_version=installed,
                zmx=dict(installed_version=version, minimum_version=minimum,
                         compatible=False, error=error),
                live_sessions=0, counts={state: 0 for state in STATES},
                status="INCOMPATIBLE", sessions=[])


def _read_only_snapshot(backend):
    """Inspect runtime labels without invoking cx's automatic UUID binder."""
    raw_snapshot = getattr(type(backend), "raw_snapshot", None)
    if callable(raw_snapshot):
        return raw_snapshot(backend, bind_threads=False)
    return backend.snapshot(bind_threads=False)


def status_data(backend=None, installed_version=None):
    if backend is None:
        import cx_zmx as backend
    installed = installed_version or getattr(backend, "VERSION", VERSION)
    minimum = ".".join(map(str, backend.MIN_VERSION))
    try:
        info = backend.version()
    except (backend.Error, OSError) as exc:
        return _failure(installed, minimum, "UNKNOWN", backend.clean(exc))
    if tuple(info["version_tuple"]) < tuple(backend.MIN_VERSION):
        error = f"zmx >= {minimum} is required; found {info['version']}."
        return _failure(installed, minimum, info["version"], error)
    try:
        snapshot = _read_only_snapshot(backend)
    except (backend.Error, OSError) as exc:
        data = _failure(installed, minimum, info["version"], backend.clean(exc))
        data["zmx"]["compatible"] = True
        return data
    sessions = []
    counts = {state: 0 for state in STATES}
    for row in snapshot.get("sessions", []):
        labels = row.get("labels") or {}
        launched = row.get("cx_version") or labels.get("cx_version") or "UNKNOWN"
        state = runtime_compatibility(launched, installed)
        counts[state] += 1
        sessions.append(dict(session=row.get("session") or "UNKNOWN",
                             cx_version=launched, upgrade_state=state,
                             state=row.get("state") or "UNKNOWN"))
    if counts["INCOMPATIBLE"]:
        overall = "INCOMPATIBLE"
    elif counts["UPGRADE_REQUIRED"]:
        overall = "REQUIRED"
    elif counts["UPGRADE_AVAILABLE"]:
        overall = "AVAILABLE"
    else:
        overall = "CURRENT"
    return dict(installed_cx_version=installed,
                zmx=dict(installed_version=info["version"], minimum_version=minimum,
                         compatible=True, error=None),
                live_sessions=len(sessions), counts=counts, status=overall,
                sessions=sessions)


def render_status(data):
    zmx = data["zmx"]
    verdict = "PASS" if zmx["compatible"] else "FAIL"
    print(f"cx installed: {data['installed_cx_version']}")
    print(f"zmx: {zmx['installed_version']} (minimum {zmx['minimum_version']}: {verdict})")
    print(f"\nLive sessions: {data['live_sessions']}\n")
    for state in STATES:
        print(f"{state + ':':20}{data['counts'][state]}")
    print(f"\nUpgrade status: {data['status']}")
    if zmx.get("error"):
        print("zmx error: " + zmx["error"])


def apply_current_upgrade(data):
    if data["status"] == "CURRENT":
        print("All live sessions are current.")
        return 0
    if data["status"] == "AVAILABLE":
        print("All live sessions are compatible. No restart is required and no runtime metadata was changed.")
        return 0
    if data["status"] == "REQUIRED":
        names = [row["session"] for row in data["sessions"]
                 if row["upgrade_state"] == "UPGRADE_REQUIRED"]
        print("Runtime regeneration is required by an explicit compatibility boundary: " + ", ".join(names))
        print("This cx release does not restart sessions automatically.")
        return 1
    print("One or more runtimes are incompatible with this cx version. No sessions were changed.")
    return 1


def main(argv=None, backend=None):
    parser = argparse.ArgumentParser(prog="cx upgrade")
    parser.add_argument("command", nargs="?", choices=("status",))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = status_data(backend)
    if args.command == "status" or args.json:
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            render_status(data)
        return 0
    return apply_current_upgrade(data)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UpgradeError, OSError) as exc:
        print("cx upgrade: " + str(exc))
        raise SystemExit(1)
