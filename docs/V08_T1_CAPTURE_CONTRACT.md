# v0.8 T1: exact workspace capture contract

T1 adds a production, read-only contract for saving verified native iTerm2
topology. It does not reconstruct a layout and does not change Codex or zmx.
The durable schema is:

```text
cxdeck.workspace-layout/v1
```

Exact topology is structural. Geometry is best effort.

## Durable schema

```json
{
  "schema": "cxdeck.workspace-layout/v1",
  "capture": {
    "provider": "iterm2-python",
    "provider_version": "2.23",
    "fidelity": "L3",
    "captured_at": "2026-09-10T12:00:00Z"
  },
  "windows": [
    {
      "frame_hint": {"x": 0, "y": 0, "width": 1200, "height": 800},
      "tabs": [
        {
          "root": {
            "type": "split",
            "axis": "columns",
            "ratio_hints": [0.5, 0.5],
            "children": [
              {
                "type": "conversation",
                "identity": {
                  "host": "host",
                  "codex_home": "/absolute/canonical/path",
                  "thread_id": "00000000-0000-4000-8000-000000000001"
                }
              },
              {
                "type": "conversation",
                "identity": {
                  "host": "host",
                  "codex_home": "/absolute/canonical/path",
                  "thread_id": "00000000-0000-4000-8000-000000000002"
                }
              }
            ]
          }
        }
      ]
    }
  ]
}
```

`columns` means ordered left-to-right children. `rows` means ordered
top-to-bottom children. Splitters may have two or more children. A live
one-child root splitter is normalized to its child; a nested one-child
splitter is rejected because T0 did not establish its meaning.

The validator rejects unknown node types and durable fields, splits with fewer
than two children, invalid axes, duplicate conversation identities, malformed
UUIDs, noncanonical or relative `CODEX_HOME` paths, invalid ratios or frames,
and unsupported schema versions. It does not coerce corrupt saved data.

## Durable and ephemeral facts

The durable artifact contains only schema and capture versions, ordered
windows/tabs/tree nodes, exact conversation identities, approximate ratio
hints, and optional window-frame hints. `ratio_hints` come from current session
grid spans. They are finite, nonnegative, normalized hints rather than exact
splitter state. `frame_hint` is a finite current rectangle; it does not promise
the same display, Space, DPI, or physical placement.

The capture receipt is ephemeral. It holds iTerm window and tab IDs, session
GUIDs, TTYs, current session geometry, exact zmx generation tuples, client
bindings, and cached view receipts. It proves a capture attempt but is not
written into the workspace layout. In particular, no iTerm ID, TTY, PID, zmx
session name, or zmx daemon identity becomes conversation identity.

## Exact binding chain

Every leaf must pass this complete chain:

```text
iTerm Session GUID + TTY
  -> one zmx client carrying the exact CX Deck generation token
  -> one current zmx generation
  -> known canonical CODEX_HOME + exact UUID
  -> (host, realpath(CODEX_HOME), UUID)
```

The runtime snapshot is requested with thread-label binding disabled. Capture
therefore never discovers a UUID by relabeling a session. It accepts only an
already known UUID and exactly one attached, token-verified client. An existing
cached view receipt is optional, but when present its GUID and TTY must agree
with current reality.

Capture fails for a missing or multiple client, a changed or incomplete
generation, an unknown UUID/home, a stale cached view, two leaves using one
generation, or two managed generations claiming one durable conversation. It
never falls back to a badge, title, cwd, repository, display name, or terminal
contents.

## Scope and workspace membership

The selected workspace determines the conversation leaves. Any iTerm window
containing one selected conversation becomes part of the capture scope. Every
leaf in every tab of that selected window must be a verified member of the same
workspace. A selected window containing an ordinary shell, an unverified pane,
or another workspace's managed conversation is rejected. Unrelated windows
that contain no selected conversation are ignored.

The set of captured durable identities must exactly equal workspace membership.
Live-only members without UUIDs, members missing from iTerm, and unexpected
members fail explicitly. `capture` never adds, removes, or replaces workspace
members.

## Double-read consistency and commit

Each attempt performs:

1. a fresh supported iTerm hierarchy read;
2. a fresh nonmutating zmx/process/client/view observation and exact binding;
3. a second fresh iTerm hierarchy read;
4. a second fresh runtime observation and exact binding; and
5. byte-stable canonical comparison of the durable content and ephemeral
   receipt.

Window/tab order and IDs, split structure, GUID/TTY leaves, geometry, leaf
mapping, zmx clients and generations, and relevant cached view receipts must
agree. CX Deck makes at most three complete attempts. A pane/tab/window change,
move, resize, client transition, generation transition, or receipt change
causes a retry and then an explicit presentation error if the view does not
stabilize.

Only a fully validated result reaches the Store. Under the existing exclusive
Store lock, CX Deck rechecks that the workspace and cached views still equal
the observations used for capture, then atomically writes `exact_layout`.
Failure leaves the previous state byte-for-byte unchanged. A first capture adds
the optional field. Replacing an existing exact layout requires `--replace`;
all membership, adaptive settings, names, pins, groups, and other state remain
unchanged. Existing v0.7 membership-only workspaces remain valid.

## Command and dependency boundary

```text
cx workspace capture NAME
cx workspace capture NAME --replace
```

Ordinary CX Deck commands do not import the iTerm2 Python package. Exact capture
loads it only at this feature boundary. The interface validated by T0 and T1 is
`iterm2==2.23`; users install it explicitly for capture support. CX Deck does
not auto-install it. A missing package or unavailable iTerm hierarchy produces
a feature-specific error and no Store or runtime change.

The production adapter uses only supported, read-only hierarchy operations:
`App.async_refresh`, `App.windows`, `Window.tabs`, `Tab.root`, ordered
`Splitter.children`, `Splitter.vertical`, session GUID/TTY/grid/frame, and
window frame reads. It rejects nonempty `Tab.minimized_sessions` with guidance
to restore the normal split view. It does not create, close, focus, resize,
move, attach, detach, label, or write to any session.

## T2 boundary

T2 may consume only a validated `cxdeck.workspace-layout/v1` object. It must
resolve every durable leaf and duplicate-check the whole plan before creating
presentation. Ratio and frame fields remain hints. T2 is responsible for safe
exact reconstruction and verified view reuse; none of that behavior is part of
T1.
