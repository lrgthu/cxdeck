"""CX Deck paths and the install-time v0.6 -> v0.7 state-directory upgrade."""
from __future__ import annotations

import os
from pathlib import Path
import stat


STATE_RELATIVE = Path(".local/state/cxdeck")
LEGACY_STATE_RELATIVE = Path(".local/state/codex-tmux")


class PathUpgradeError(RuntimeError):
    pass


def state_home(home=None):
    return Path(home or Path.home()) / STATE_RELATIVE


def _owned_directory(path, *, create=False):
    if path.is_symlink():
        raise PathUpgradeError(f"State path is a symlink: {path}")
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PathUpgradeError(f"State path is not an owned directory: {path}")


def validate_state_upgrade(home=None):
    """Validate the state transition without creating, moving, or rewriting data."""
    home = Path(home or Path.home())
    old_base = home / LEGACY_STATE_RELATIVE
    new_base = home / STATE_RELATIVE
    old = old_base / "workbench"
    new = new_base / "workbench"
    for parent in (home / ".local", home / ".local/state", old_base, new_base):
        if parent.exists() or parent.is_symlink():
            _owned_directory(parent)
    if old.exists() or old.is_symlink():
        _owned_directory(old)
        if new.exists() or new.is_symlink():
            _owned_directory(new)
            raise PathUpgradeError(
                "Both old and new CX Deck workbench directories exist; refusing to merge or discard either.")
        return "UPGRADE_AVAILABLE"
    if new.exists() or new.is_symlink():
        _owned_directory(new)
    return "CURRENT"


def upgrade_state_path(home=None):
    """Atomically move durable v0.6 workbench data to the CX Deck state path.

    Only the organization store moves. Historical files beside it remain inert in
    the old directory. The operation never inspects or changes Codex or zmx.
    """
    home = Path(home or Path.home())
    old_base = home / LEGACY_STATE_RELATIVE
    new_base = home / STATE_RELATIVE
    old = old_base / "workbench"
    new = new_base / "workbench"
    transition = validate_state_upgrade(home)
    if not old.exists() and not old.is_symlink():
        # A read-only CX Deck command must not create private state merely by
        # constructing the Store. The first actual Store write creates it.
        return "CURRENT"
    _owned_directory(new_base, create=True)
    os.chmod(new_base, 0o700)
    if transition != "UPGRADE_AVAILABLE":
        raise PathUpgradeError("State upgrade preflight changed unexpectedly; nothing moved.")
    os.replace(old, new)
    os.chmod(new, 0o700)
    for parent in (old_base, new_base):
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return "UPGRADED"
