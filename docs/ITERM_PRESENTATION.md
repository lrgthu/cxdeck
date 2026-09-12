# Native iTerm2 presentation

iTerm2 owns windows, tabs, splits, scrolling, selection, copy/paste, mouse input,
terminal sizing, colors, links, titles, and full-screen redraw. CX Deck does not
introduce another terminal UI layer.

CX Deck owns one iTerm
[dynamic profile](https://iterm2.com/documentation-dynamic-profiles.html) named
`CX Deck`. iTerm merges its omitted
settings from the user's current default profile. The file overrides only:

```text
Badge Text = \(user.cxdeck_name)
Timestamps Visible = true|false
Timestamps Style = overlap
```

Views are created explicitly with that profile, so unrelated iTerm sessions and
the Default Profile are unchanged. The badge is iTerm's native overlay and does
not consume terminal rows. CX Deck also sets native session-name metadata, while
the attached program remains free to update its ordinary terminal title. The
badge and session-name metadata use the complete Store display name; iTerm may
visually shorten them when physical space is insufficient.

For `cx` and `cx new` in the caller's current pane, CX Deck selects its profile
and sets the same user variable with iTerm's documented OSC controls immediately
before the first zmx attach. Those controls go to the caller-owned iTerm session,
before the Codex PTY is connected, and produce no terminal rows.

Once a pane is deliberately used as a CX Deck view, it may remain on the CX Deck
profile after zmx detaches. This keeps its presentation role visible and avoids
fragile tracking or restoration of an arbitrary previous profile. The pane can be
repurposed normally by selecting another iTerm profile.

`cx rename` saves the display name first, then updates the verified view's title
and `user.cxdeck_name` variable by GUID and TTY. `cx views refresh` applies names
to all verified existing views. Neither operation writes to the PTY, sends input,
attaches, detaches, or changes the zmx generation.

iTerm's `Timestamps Visible` session-profile property supplies per-line scrollback
times. `cx config timestamps on|off` updates only the CX Deck dynamic profile;
iTerm applies that profile update to existing views that use it as well as future
views. No prefix is inserted into terminal output, no scrollback database is
created, and no agent is reattached.

These mechanisms follow iTerm2's supported
[badge](https://iterm2.com/documentation-badges.html) and
[session timestamp](https://iterm2.com/documentation-preferences-profiles-session.html)
features. CX Deck uses AppleScript only for supported session creation,
GUID/TTY inventory, focus, title, and user-variable operations; it does not use
Accessibility UI clicking.

The AppleScript bridge materializes each window, tab, and session collection and
then traverses it by index. It never uses nested `every session of every tab of
every window` object specifiers. Candidate sessions and target windows are
matched by scalar GUID, TTY, and window ID; collection mutation begins only after
the lookup traversal has ended. This avoids iTerm error `-1719` when multiple
windows, tabs, or splits are present.

A view is reusable only when its current TTY belongs to a zmx client carrying the
exact live-generation token. Stale cached views, unknown attached clients,
multiple clients, and changed generations fail closed. `cx views rebuild`
opens all missing verified live generations in one adaptive native layout
request and never starts Codex. `cx views status [--workspace NAME]` reports
presentation health independently from process health. A workspace-scoped
rebuild is adaptive presentation repair; exact saved topology remains the job of
`cx workspace open NAME`. `cx focus --next|--previous` focuses only an already
verified GUID/TTY and opens no attach client.

The v0.8 read-only capture path uses the separately installed iTerm2 Python API
to read `Tab.root` as an ordered `Splitter`/`Session` tree. It double-reads the
hierarchy and exact GUID/TTY-to-zmx bindings before atomically storing an
optional layout; it never creates or changes presentation. See
[the T1 capture contract](V08_T1_CAPTURE_CONTRACT.md).

The v0.8 exact restore path uses the same supported hierarchy plus documented
tab/session move APIs. Verified existing Sessions are moved in place, preserving
their GUID, TTY, attach client, zmx generation, and Codex PID. Missing views use
generation-pinned `attach-verified`; a final structural recapture must equal the
saved tree. Frames and ratios remain hints, and window stacking order is outside
the exact contract. See [the T2 reconstruction contract](V08_T2_RECONSTRUCTION.md).

The disposable terminal comparison on macOS arm64 with zmx 0.8.1 verifies
keyboard and escape input, Ctrl-\\, bracketed paste, mouse-wheel sequences,
alternate-screen enter/exit, resize, long output, ANSI colors, OSC titles, OSC-8
links, terminal close, reattach, and child-exit cleanup. Common direct and zmx
behavior is equivalent; closing the zmx client preserves the child.

The disposable real-iTerm harness verifies native window, tab, and split
creation, unique badges/titles across five panes, narrow-pane resize, timestamps,
inventory, focus, preferred-view reuse, pane disappearance, workspace reopen,
process preservation, and detach-key disabling.
