#!/usr/bin/env python3
"""CX Deck entry point with exact Codex conversation recovery."""
from __future__ import annotations
import argparse
import json
import sys

from cx_version import VERSION
PICKER_OPTIONS = {'--list', '--json', '--limit', '--select', '--cwd', '--per-tab',
                  '--min-columns', '--min-rows', '--no-iterm', '--no-dashboard',
                  '--allow-unverified-live', '--group', '--yolo', '--safe', '--no-yolo', '--help', '-h'}
HELP = f'''CX Deck {VERSION} — persistent Codex sessions with a native terminal experience
  cx / cx new                 new agent HERE; no repo selection
  cx LABEL                    create/attach a globally named agent
  cx new --split              new agent in a new local iTerm2 split
  cx new --tab / --window     new tab / window
  cx new --count 3            create any positive count; adaptive split/tab layout
  cx dashboard               interactive workbench (n/new, Enter/focus, r/rename)
  cxl / cx status --json      one snapshot / structured diagnostics
  cx focus NAME              focus a verified existing pane, or open one
  cx rename NAME DISPLAY     update the native pane name; runtime identity unchanged
  cx pin NAME / cx unpin NAME  organize the workbench
  cx workspace save daily    save current sessions; --select NAME ... for a subset
  cx workspace open daily    review/select saved workspace members
  cx workspace open daily --all   deliberately open all; refuse incomplete groups
  cx workspace list          list saved collections
  cx group create/list/...   organize exact conversations in local groups
  cx resume                  multi-select saved/live conversations; reuse views
  cx resume --safe / --no-yolo  opt out of default YOLO for cold resumes only
  cx resume --yolo           explicitly select default YOLO cold resume policy
  cx resume --list / --json   inspect saved history without starting agents
  cx resume --no-iterm        terminal-only fallback
  cx resume --all             compatibility: native SINGLE-conversation picker
  cx run -- CODEX_ARGS...     literal native arguments; utilities pass through
  cxa NAME / cxt LABEL        attach through zmx; users do not type zmx
  cx project PATH            optional repository metadata after discovery
  cx doctor / cxinfo NAME     diagnostics / session details
  cxkill NAME                 explicit confirmed termination; never automatic
  cx views rebuild           batch missing verified zmx views into native iTerm
  cx views refresh           refresh names on verified existing iTerm views
  cx config timestamps on|off  native iTerm scrollback timestamps (default on)
  cx upgrade status          runtime compatibility for the installed cx version
  cx upgrade                 apply defined safe upgrades; never restart implicitly

Dashboard: arrows/j/k move, Space selects, / searches, Enter focuses/opens,
n creates one agent, N asks for a count, r renames, p pins/unpins,
s saves selected (or highlighted) sessions, w opens a workspace, q exits the view.
No fixed agent count. No prompts, approvals or automatic commits are sent.
GUI commands require local macOS/iTerm2 Automation permission. Plain cx is portable.
Workspace is a collection, NOT a Git worktree. Unknown IDs are live-only.
'''


class ResumeBackend:
    def __init__(self, console, store=None):
        from cx_store import Store
        self.console = console
        self.core = console.backend()
        self.VERSION = VERSION
        self.store = store or Store()

    def __getattr__(self, key):
        return getattr(self.core, key)

    def raw_snapshot(self, *, bind_threads=True):
        return self.core.snapshot(bind_threads=bind_threads)

    def snapshot(self):
        import workbench
        return workbench.enrich(self.raw_snapshot(), self, self.store)

    def dashboard(self):
        import workbench
        return workbench.dashboard(self)

def main(argv=None, console=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if console is None:
        import agent_console as console
    console.VERSION = VERSION
    if argv and argv[0] in ('help', '--help', '-h'):
        print(HELP)
        return 0
    if not argv:
        return console.main(argv)
    cmd, rest = argv[0], argv[1:]
    if cmd == 'resume' and (not rest or any(x.split('=', 1)[0] in PICKER_OPTIONS for x in rest)):
        import codex_resume
        parser = argparse.ArgumentParser(prog='cx')
        codex_resume.add_arguments(parser.add_subparsers(dest='command', required=True))
        args = parser.parse_args(argv)
        # The terminal-only path remains the original, independently tested API.
        # GUI operations use the workbench's verified existing-view reuse.
        b = ResumeBackend(console)
        if args.no_iterm or args.list or args.json:
            codex_resume.execute(args, b)
        else:
            import workbench
            workbench.resume(args, b)
        return 0
    if cmd == 'new':
        options = rest[:rest.index('--')] if '--' in rest else rest
        if any(x.split('=', 1)[0] in ('--split', '--tab', '--window', '--count') for x in options):
            import workbench
            workbench.new_agents(rest, ResumeBackend(console))
            return 0
    if cmd in ('dashboard', 'status'):
        import workbench
        p = argparse.ArgumentParser(prog='cx ' + cmd)
        p.add_argument('--once', action='store_true')
        p.add_argument('--json', action='store_true')
        p.add_argument('--interval', type=float, default=3)
        a = p.parse_args(rest)
        workbench.dashboard(ResumeBackend(console), a.interval, a.once or cmd == 'status', a.json)
        return 0
    if cmd in ('focus', 'rename', 'pin', 'unpin', 'info'):
        import workbench
        p = argparse.ArgumentParser(prog='cx ' + cmd)
        p.add_argument('name')
        if cmd == 'rename':
            p.add_argument('display')
        a = p.parse_args(rest)
        b = ResumeBackend(console)
        if cmd == 'focus':
            workbench.focus_rows([workbench.resolve(a.name, b)], b)
        elif cmd == 'info':
            print(json.dumps(workbench.resolve(a.name, b), indent=2))
        else:
            workbench.annotate(a.name, b, title=a.display if cmd == 'rename' else None,
                               pinned=(cmd == 'pin') if cmd != 'rename' else None)
        return 0
    if cmd == 'workspace':
        import workbench
        workbench.workspace_command(rest, ResumeBackend(console))
        return 0
    if cmd == 'group':
        import workbench
        workbench.group_command(rest, ResumeBackend(console))
        return 0
    if cmd == 'views':
        import workbench
        return workbench.views_command(rest, ResumeBackend(console))
    if cmd == 'config':
        import workbench
        return workbench.config_command(rest, ResumeBackend(console))
    if cmd == 'upgrade':
        import cx_upgrade
        return cx_upgrade.main(rest, ResumeBackend(console))
    return console.main(argv)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    except (RuntimeError, OSError) as exc:
        from cx_zmx import clean
        print('cx: ' + clean(exc), file=sys.stderr)
        sys.exit(1)
