#!/usr/bin/env python3
"""Dependency-free privacy and secret check for tracked public source files."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


MAX_TEXT_BYTES = 5 * 1024 * 1024
ALLOWED_EMAIL_DOMAINS = {"example.com", "users.noreply.github.com"}
ALLOWED_USER_PATHS = {"example", "runner", "shared", "test"}


def tracked_files(root: Path):
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True
    )
    return [root / os.fsdecode(value) for value in result.stdout.split(b"\0") if value]


def _patterns():
    private_key = "BEGIN " + "PRIVATE KEY"
    archive_name = "cxdeck-" + "private-archive"
    users_path = re.escape("/" + "Users" + "/")
    return (
        ("PRIVATE_KEY", re.compile(re.escape(private_key), re.I)),
        ("GITHUB_TOKEN", re.compile(r"(?:ghp|github_pat)_[A-Za-z0-9_]{20,}")),
        ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
        ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
        ("SLACK_TOKEN", re.compile(r"\bxox[a-z]-[0-9A-Za-z-]{20,}\b")),
        ("AUTH_HEADER", re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)),
        ("SECRET_ASSIGNMENT", re.compile(
            r"(?i)\b(?:password|passwd|token|secret|api_key)\s*=\s*['\"][^'\"]{8,}['\"]"
        )),
        ("PRIVATE_ARCHIVE", re.compile(re.escape(archive_name), re.I)),
        ("PERSONAL_USER_PATH", re.compile(users_path + r"([^/\s'\"]+)")),
    )


EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")


def scan(root: Path, files, extra_forbidden=()):
    root = root.resolve()
    patterns = _patterns()
    # FQDN resolution can block for a minute on otherwise healthy macOS runners.
    # The kernel hostname plus an explicit environment override cover local
    # identity without introducing a network lookup into this deterministic gate.
    dynamic = {getpass.getuser(), socket.gethostname(), *extra_forbidden}
    dynamic = {value for value in dynamic if value and len(value) >= 4 and value not in {"root", "runner"}}
    findings = []
    for path in files:
        path = Path(path)
        relative = path.resolve().relative_to(root)
        data = path.read_bytes()
        if len(data) > MAX_TEXT_BYTES:
            findings.append(("OVERSIZED_FILE", str(relative), 0))
            continue
        if b"\0" in data:
            findings.append(("BINARY_FILE", str(relative), 0))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(("NON_UTF8_FILE", str(relative), 0))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for category, pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                if category == "PERSONAL_USER_PATH" and match.group(1).lower() in ALLOWED_USER_PATHS:
                    continue
                findings.append((category, str(relative), number))
            for match in EMAIL.finditer(line):
                if match.group(2).lower() not in ALLOWED_EMAIL_DOMAINS:
                    findings.append(("NONPUBLIC_EMAIL", str(relative), number))
            lowered = line.casefold()
            if any(value.casefold() in lowered for value in dynamic):
                findings.append(("LOCAL_IDENTITY", str(relative), number))
    return sorted(set(findings))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    extra = [value.strip() for value in os.environ.get("CX_PUBLIC_AUDIT_FORBIDDEN", "").split(",")]
    files = tracked_files(root)
    findings = scan(root, files, extra)
    if findings:
        for category, path, line in findings:
            print(f"{category}: {path}:{line}")
        print(f"Public-source audit failed with {len(findings)} finding(s).", file=sys.stderr)
        return 1
    print(f"Public-source audit passed: {len(files)} source files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
