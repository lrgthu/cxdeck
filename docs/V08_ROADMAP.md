# CX Deck v0.8 roadmap

## Problem

Persistence is solved, but daily use with many conversations still requires manual work in two places. Workspaces remember members and adaptive layout settings rather than an exact native iTerm arrangement, and saved/live conversations and their current presentation health are not visible in one concise navigation surface.

## User-facing goals

1. **Faithful native workspace restoration.** Capture a workspace's CX Deck-owned iTerm windows, tabs, and exact ordered split tree, then reconstruct that structure with approximate size ratios while moving verified existing views in place.
2. **Faster conversation navigation.** Present saved, managed-live, and known-external conversations with clear states, pinned/recent ordering, and exact metadata search across display name, group, repository, cwd, and UUID.
3. **Actionable view health.** Report whether each live conversation has one verified preferred view, no view, an unverifiable client, or multiple clients; support workspace-scoped view rebuild and next/previous focus without changing a runtime generation.

## Non-goals

- Changes to zmx persistence, Codex UUID identity, resume policy, or process ownership.
- Terminal output parsing, transcript search, prompt injection, cross-agent input, approvals, Git actions, or task orchestration.
- A terminal multiplexer, terminal emulator, or replacement for iTerm presentation.
- Remote-machine execution, a distribution pipeline, or automatic pane closing in v0.8.

## Architecture invariants

- Conversation identity remains `(host, realpath(CODEX_HOME), exact Codex UUID)`.
- Live identity remains `(host, realpath(zmx socket directory), exact session name, daemon PID, creation time)`.
- Layouts, display names, activity hints, iTerm GUIDs, TTYs, groups, and workspaces are presentation or organization metadata only.
- A known duplicate UUID, changed zmx generation, unverifiable attached client, or multiple clients fails closed.
- GUI failure never starts, stops, restarts, detaches, relabels, or replaces Codex or zmx.
- iTerm continues to own mouse, selection, copy/paste, scrolling, sizing, and rendering.

## Proposed UX

```text
cx workspace capture NAME [--replace]
cx workspace open NAME
cx dashboard
cx resume --list|--json
cx views status [--json]
cx views rebuild --workspace NAME
cx focus --next|--previous
```

`workspace capture` is read-only with respect to Codex and zmx. `workspace open` resolves the complete exact runtime set before moving verified existing views or creating missing presentation. Search and recency use metadata only and label each source and timestamp explicitly. View-health commands diagnose or reconstruct presentation; they never regenerate a session.

## Implementation phases

1. **Disposable topology probe — complete.** The supported iTerm2 Python API exposes `Tab.root` as an ordered `Splitter`/`Session` tree. Five disposable fixtures round-tripped exactly across mixed splits, multiple windows and tabs; resize left the tree invariant, and mutation races failed closed. The result is `EXACT_TREE_AVAILABLE_WITH_LIMITATIONS`, at L3: exact tree plus approximate ratios. See [the T0 report](V08_T0_ITERM_TOPOLOGY_PROBE.md).
2. **Capture contract — complete.** `cx workspace capture NAME [--replace]` now writes a strictly validated `cxdeck.workspace-layout/v1` object sourced from `Tab.root`. It double-reads iTerm structure, geometry, exact GUID/TTY-to-zmx bindings, generations, and view receipts before one atomic Store update. Durable leaves contain only exact conversation identity; mixed unmanaged panes and unsupported minimized state fail closed. Existing membership-only workspaces are unchanged. See [the T1 contract](V08_T1_CAPTURE_CONTRACT.md).
3. **Safe exact reconstruction — complete.** `cx workspace open NAME` now validates layout/v1, resolves and duplicate-checks the complete runtime set, cold-resumes saved-only identities exact UUIDs through the existing launch path, then applies the empirically proven `MOVE_REUSE` policy. Supported iTerm APIs preserve Session GUID/TTY and zmx/Codex processes while moving tabs and split Sessions. Final structural recapture is mandatory; ratio/frame fields remain hints. A deliberately interrupted disposable restore converged on rerun, and a second complete restore performed zero moves, clients, or runtime changes. See [the T2 reconstruction contract](V08_T2_RECONSTRUCTION.md).
4. **Unified navigation and view health — complete.** `cxdeck.inventory/v1`
   composes saved history, managed zmx generations, known external processes,
   organization metadata, and fresh GUID/TTY presentation facts while keeping
   conversation, runtime, and view state separate. `cx views status`, local
   metadata search, verified next/previous focus, and workspace-scoped adaptive
   rebuild now share this model. Provider failure degrades only view health;
   status/navigation creates no clients or runtimes. See
   [the T3 contract](V08_T3_NAVIGATION_HEALTH.md).

The bounded v0.8 feature scope is frozen: T0, T1, T2, and T3 are complete, and
there is no T4. The integrated release contract and command mutation audit are
recorded in [the v0.8 release review](V08_RELEASE_REVIEW.md).

## Acceptance tests

- Capture and restore the exact ordered split tree for at least five disposable sessions across multiple windows, tabs, horizontal splits, vertical splits, and mixed trees. Ratio and window-frame restoration is best effort and must be reported as such.
- Restoring twice opens zero duplicate clients and preserves every zmx generation and Codex PID.
- Missing saved conversations use exact UUID resume rules; known external duplicates and unidentified processes retain current fail-closed behavior.
- A pane or window disappearing during capture or restore produces an explicit presentation error and no runtime mutation.
- Search never fuzzy-resumes and never reads transcript bodies; identical display names remain distinguishable by exact identity and source state.
- Recency reports its metadata source and never presents process existence as task activity.
- View health correctly distinguishes one verified view, missing view, unverifiable client, multiple clients, and changed generation.
- Workspace-scoped rebuild affects only selected verified generations; next/previous focus opens no client.
- Direct-vs-zmx terminal behavior remains equivalent and `ZMX_NO_DETACH_KEY=1` remains mandatory.

## Risks

- The split tree is directly available, but split ratios are derived from session geometry and exact cross-display window placement is not proven. Window stacking order has no supported setter. Treat these as presentation hints; adaptive reconstruction remains an explicit `--adaptive` choice rather than a silent fallback.
- Saved-history and runtime timestamps describe different events. The UI must label their source and avoid implying agent progress.
- Cached GUID/TTY receipts can become stale. Every presentation action must re-inventory iTerm and revalidate the exact zmx client generation.
- Dense layouts may not fit current screen dimensions. Reconstruction should report a presentation limitation and keep all agents alive.

## Explicit deferred ideas

- Remote `local terminal -> remote zmx -> remote Codex` support.
- Homebrew, signed binaries, one-line installation, and automatic update checks.
- Automatic closing of stale panes or windows.
- Transcript-content search or indexing.
- Multi-view conversations, cross-agent messaging, and orchestration.
- Any replacement or abstraction of the zmx provider.
