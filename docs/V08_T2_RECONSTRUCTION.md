# v0.8 T2: safe exact reconstruction

T2 consumes only a validated `cxdeck.workspace-layout/v1` and restores its
native iTerm presentation. It does not change conversation identity or replace
a live runtime. Exactness covers window membership, tab membership and order,
and the ordered split tree. Ratios, frames, window stacking, monitor selection,
and physical placement remain best-effort hints.

## Runtime planning

`cx workspace open NAME` builds the complete plan before starting a saved
conversation or changing iTerm. Every leaf is resolved by exactly:

```text
(host, realpath(CODEX_HOME), exact Codex UUID)
```

Each leaf becomes `LIVE_MANAGED` or `SAVED_ONLY`. Missing history, another
host/home, an incomplete or changed zmx generation, two managed generations,
or a known external same-UUID process produces `RUNTIME_BLOCKED` before GUI
mutation. Unidentified Codex processes block a saved-only resume unless the
existing explicit `--allow-unverified-live` override is supplied. The override
never permits a known exact duplicate.

The existing exact-resume implementation performs saved-only launches under
the normal per-home launch lock, with the normal YOLO default and
`--safe`/`--no-yolo` behavior. CX Deck re-observes duplicate risk before each
subsequent cold launch and revalidates the complete runtime set afterward.
Successful runtimes survive `RUNTIME_PARTIAL`; presentation begins only when
every leaf has exactly one verified managed generation.

`cx workspace open NAME --no-iterm` stops at `RUNTIME_RESOLVED`.

## Existing-view policy: MOVE_REUSE

Disposable experiments with iTerm 3.7.0 and the supported `iterm2==2.23`
Python API proved these operations preserve the same iTerm Session GUID, TTY,
zmx attach-client PID, zmx generation, and dummy Codex PID:

- `Window.async_set_tabs` for tab ordering and cross-window tab moves;
- `Tab.async_move_to_window` for a tab moved to its own window;
- `Session.async_move_to_new_tab` and `async_move_to_new_window`; and
- `App.async_move_session` for ordered split insertion and arbitrary nesting.

CX Deck therefore uses `MOVE_REUSE`. A verified existing Session is reparented
in place. A missing view is created with the CX Deck profile and an
`attach-verified` command pinned to the already resolved generation. No old
attach client is closed merely to reshape presentation, and no temporary
second client is needed.

The reconstructor materializes fresh window/tab/session collections and
re-resolves each Session by scalar GUID after every mutation. It also rechecks
TTY, exact generation, Codex PID set, and the single zmx client before the next
operation. This handles iTerm's brief post-move period where parent links have
not yet refreshed without retaining unstable nested object references.

Session-level movement protects mixed iTerm content: target Sessions may leave
a tab containing ordinary shells, while the unrelated panes remain untouched.
CX Deck moves a whole tab only when that tab contains one target Session. It
never closes an unrelated tab or window. If the invoking pane is a target that
would need movement, restore returns `PRESENTATION_CONFLICT` before mutation;
run the command from another pane.

## Structural reconstruction

The production blueprint selects one durable conversation anchor per desired
tab and window. It first isolates and orders those anchors, then creates each
validated `columns` or `rows` splitter using ordered children. Same-axis
splitters remain N-ary. Parentage always comes from the saved structural tree,
never from rectangles.

After all moves, CX Deck makes a fresh supported-API topology read and binds
every leaf through:

```text
iTerm GUID/TTY
  -> exact zmx client
  -> exact zmx generation
  -> canonical CODEX_HOME + UUID
```

The final tree must match the saved window grouping, tab grouping/order, split
axes, nesting, child order, and durable leaves. iTerm has no supported window
stacking-order setter, so stacking/list order is outside the exact contract.
Window membership remains exact.

Preferred view receipts are committed together only after this independent
recapture succeeds. Receipts remain an ephemeral cache rather than identity.

## Geometry hints

Structure is created first. Valid `ratio_hints` are then translated to session
`preferred_size` values and one supported tab layout update. A `frame_hint` is
applied with the supported window-frame API when possible. A failed ratio or
frame hint reports `GEOMETRY_LIMITATION`; it does not undo a correct tree or
affect a runtime. T2 continues to claim L3, not exact physical geometry.

## Failure and recovery

The externally meaningful results are:

- `RUNTIME_BLOCKED`: no runtime or presentation action began.
- `RUNTIME_PARTIAL`: one or more exact cold runtimes landed, all are retained,
  and presentation did not begin.
- `PRESENTATION_CONFLICT`: current views cannot be changed safely.
- `PRESENTATION_PARTIAL`: runtime resolution succeeded, but iTerm stopped
  during application.
- `TOPOLOGY_MISMATCH`: final supported-API recapture was not structurally equal.
- `GEOMETRY_LIMITATION`: topology is exact and only hints were limited.
- `COMPLETE`: exact structure was independently verified.

There is no migration-style journal or presentation rollback. A rerun inspects
fresh runtime and iTerm reality, reuses every surviving exact generation and
client, and converges. It never kills a successfully created runtime because a
later GUI operation failed.

## Compatibility and dependency boundary

Membership-only v0.7 workspaces continue through adaptive `workspace open` and
do not import `iterm2`. A workspace with `exact_layout` restores exactly by
default; `--adaptive` deliberately requests the older adaptive presentation.
There is no silent adaptive fallback after exact reconstruction fails.

The optional Python dependency remains local to exact capture/restore:

```bash
python3 -m pip install --user 'iterm2==2.23'
```

CX Deck does not auto-install it. Normal lifecycle, status, resume, and adaptive
workspace operations remain independent of this package.
