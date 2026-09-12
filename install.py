#!/usr/bin/env python3
"""Back up and install CX Deck without changing running sessions."""
import datetime
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from cx_version import VERSION

SOURCE_LINE = 'source "$HOME/.cxdeck.zsh"'
OLD_SOURCE_LINE = 'source "$HOME/.codex-tmux.zsh"'
SHIM = 'source "$HOME/.local/share/cxdeck/cxdeck.zsh"\n'
OLD_SHIM = 'source "$HOME/.local/share/codex-tmux/codex-tmux.zsh"\n'
INSTALL_MARKER = '.cxdeck-owned'
MARKER = b'CX Deck installation v1\n'
FILES = ('cxdeck.zsh', 'cx_version.py', 'cx_paths.py', 'cx_zmx.py', 'cx_upgrade.py', 'agent_console.py',
         'console_entry.py', 'codex_resume.py', 'cx_store.py', 'cx_iterm.py',
         'workbench.py')


def atomic_write(dest, data, mode=0o600):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.cx-', dir=dest.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, dest)
        directory = os.open(dest.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _owned_directory(path, label):
    if path.is_symlink():
        raise RuntimeError(f'{label} is a symlink; refusing to modify it.')
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f'{label} is not an owned directory.')


def _owned_regular(path, label, *, symlink_ok=False):
    target = path
    if path.is_symlink():
        if not symlink_ok:
            raise RuntimeError(f'{label} is a symlink; refusing to modify it.')
        target = path.resolve(strict=True)
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f'{label} is not an owned regular file.')
    return target


def _validate_install_directory(module, *, marker_required):
    _owned_directory(module, 'CX Deck install directory')
    if not module.exists():
        return
    marker = module / INSTALL_MARKER
    if marker.exists() or marker.is_symlink():
        _owned_regular(marker, 'CX Deck install marker')
        if marker.read_bytes() != MARKER:
            raise RuntimeError('CX Deck install marker is invalid; refusing to modify this directory.')
        return
    if marker_required:
        raise RuntimeError('CX Deck install directory has no ownership marker; refusing automatic removal.')
    # The unmerged v0.7 candidate predates the marker. Accept it for an
    # in-place installer repair only when both identifying payloads are present.
    for name in ('cxdeck.zsh', 'cx_version.py'):
        candidate = module / name
        _owned_regular(candidate, f'Existing CX Deck {name}')
        if not candidate.exists():
            raise RuntimeError('Unmarked install directory is not a recognized CX Deck candidate.')


def _zshrc(home):
    path = home / '.zshrc'
    target = _owned_regular(path, '.zshrc', symlink_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    return path, target, mode


def install(home, source, configure_iterm=None):
    """Paths injected by tests. Install never runs Codex, brew, or zmx."""
    home, source = Path(home), Path(source)
    from cx_paths import LEGACY_STATE_RELATIVE, state_home, upgrade_state_path, validate_state_upgrade
    from cx_store import Store
    import cx_iterm

    module = home / '.local/share/cxdeck'
    old_module = home / '.local/share/codex-tmux'
    shim = home / '.cxdeck.zsh'
    old_shim = home / '.codex-tmux.zsh'
    validate_state_upgrade(home)
    for parent in (home / '.local', home / '.local/share'):
        _owned_directory(parent, 'Install path')
    _validate_install_directory(module, marker_required=False)
    _owned_directory(old_module, 'Historical CX install directory')
    _owned_regular(shim, 'CX Deck shell shim')
    if shim.exists() and shim.read_text() != SHIM:
        raise RuntimeError('Existing .cxdeck.zsh is not owned by this installation.')
    _owned_regular(old_shim, 'Historical CX shell shim')
    zshrc, zshrc_target, zshrc_mode = _zshrc(home)
    payloads = {name: (source / name).read_bytes() for name in FILES}
    old_store = home / LEGACY_STATE_RELATIVE / 'workbench'
    active_store = old_store if old_store.exists() else state_home(home) / 'workbench'
    timestamps = Store(active_store).preference('timestamps', True)
    configure_iterm = sys.platform == 'darwin' if configure_iterm is None else configure_iterm
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backups = state_home(home) / 'backups'
    if backups.is_symlink():
        raise RuntimeError('Backup directory is a symlink; refusing to write through it.')
    backups.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not backups.is_dir() or backups.stat().st_uid != os.getuid():
        raise RuntimeError('Backup directory is not an owned directory.')
    os.chmod(backups.parent, 0o700)
    os.chmod(backups, 0o700)
    backup = backups / stamp
    backup.mkdir(parents=True, mode=0o700)
    for name in ('.zshrc', '.cxdeck.zsh', '.codex-tmux.zsh'):
        path = home / name
        if path.exists():
            shutil.copy2(path, backup / name)
    if module.exists():
        shutil.copytree(module, backup / 'installed', symlinks=True)
    if old_module.exists():
        shutil.copytree(old_module, backup / 'old-installed', symlinks=True)
    if configure_iterm:
        cx_iterm.ensure_profile(timestamps, home)
    for name, data in payloads.items():
        atomic_write(module / name, data)
    atomic_write(module / INSTALL_MARKER, MARKER)
    atomic_write(shim, SHIM.encode())
    existing = zshrc_target.read_text() if zshrc_target.exists() else ''
    owned_lines = (SOURCE_LINE, OLD_SOURCE_LINE, '# CX Deck: persistent Codex sessions',
                   '# Persistent Codex sessions / workbench')
    lines = [line for line in existing.splitlines() if line.strip() not in owned_lines]
    lines += ['', '# CX Deck: persistent Codex sessions', SOURCE_LINE]
    zshrc_data = ('\n'.join(lines).rstrip() + '\n').encode()
    atomic_write(zshrc_target, zshrc_data, mode=zshrc_mode)
    upgrade_state_path(home)
    for child in module.iterdir():
        if child.name not in (*FILES, INSTALL_MARKER):
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    if old_module.exists():
        shutil.rmtree(old_module)
    if old_shim.exists() and old_shim.read_text() == OLD_SHIM:
        old_shim.unlink()
    return backup


def uninstall(home):
    """Remove CX Deck code/config while preserving state and Codex history."""
    import cx_iterm
    home = Path(home)
    module = home / '.local/share/cxdeck'
    shim = home / '.cxdeck.zsh'
    zshrc = home / '.zshrc'
    _validate_install_directory(module, marker_required=True)
    _owned_regular(shim, 'CX Deck shell shim')
    if shim.exists() and shim.read_text() != SHIM:
        raise RuntimeError('Existing .cxdeck.zsh is not owned by this installation.')
    zshrc, zshrc_target, zshrc_mode = _zshrc(home)
    cx_iterm.validate_profile_removal(home)
    if zshrc_target.exists():
        lines = [line for line in zshrc_target.read_text().splitlines()
                 if line.strip() not in (SOURCE_LINE, '# CX Deck: persistent Codex sessions')]
        data = ('\n'.join(lines).rstrip() + '\n').encode()
        atomic_write(zshrc_target, data, mode=zshrc_mode)
    if module.exists():
        shutil.rmtree(module)
    if shim.exists():
        shim.unlink()
    cx_iterm.remove_profile(home)


def main():
    if sys.version_info < (3, 9):
        raise RuntimeError('Python 3.9+ is required.')
    missing = [x for x in ('zmx', 'zsh', 'git') if not shutil.which(x)]
    if missing:
        detail = 'Missing prerequisites: ' + ', '.join(missing)
        if 'zmx' in missing:
            detail += '. Install zmx explicitly with: brew install neurosnap/tap/zmx'
        raise RuntimeError(detail)
    import cx_zmx
    try:
        zmx_info = cx_zmx.preflight()
    except cx_zmx.Error as exc:
        raise RuntimeError(str(exc)) from exc
    backup = install(Path.home(), Path(__file__).resolve().parent)
    minimum = '.'.join(map(str, cx_zmx.MIN_VERSION))
    print(f'Installed CX Deck {VERSION}. Backups: {backup}')
    print(f"zmx {zmx_info['version']}: compatible (minimum {minimum})")
    print('Load at a shell prompt: source "$HOME/.cxdeck.zsh"')
    print('Then: cx doctor; cx dashboard; cx new --split; cx resume')
    print('Plain cx still starts here; workspace and GUI actions are explicit.')
    print('Existing Codex aliases/functions are preserved. CX_WRAP_CODEX=0 bypasses wrapping.')
    print('Existing zmx sessions were left untouched.')
    print('Check: cx doctor; cx upgrade status')
    print('zmx was not installed or upgraded automatically.')
    if not shutil.which('codex'):
        print('WARNING: codex is not on PATH; starting agents and listing history require Codex CLI.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(f'Install failed: {exc}', file=sys.stderr)
        sys.exit(1)
