# v0.8 T0: iTerm topology probe

## Verdict

**EXACT_TREE_AVAILABLE_WITH_LIMITATIONS**

The supported iTerm2 Python API directly exposes the native split tree. Capture
and reconstruction can reach **L3 — exact windows/tabs/split tree with
approximate split ratios**. The result is structural; it does not infer parentage
from rectangles. Exact, portable presentation geometry (L4) is not available.

This result unblocks a read-only T1 capture contract. It does not change CX
Deck's persistence or conversation identity model and does not implement
workspace restoration.

## Environment

The live experiment ran on 2026-09-10 with:

| Component | Version |
| --- | --- |
| macOS | 15.7.7 |
| iTerm2 | 3.7.0 |
| `iterm2` Python distribution | 2.23 |
| Python interface | iTerm2 Python API |
| Secondary interface inspected | Installed iTerm2 AppleScript dictionary |

The Python package was temporary and is not a CX Deck runtime dependency:

```bash
uv run --isolated --with 'iterm2==2.23' \
  python tools/iterm_topology_probe.py run
```

The experiment used only owned disposable windows running `/bin/sleep 600`.
Every window, tab, and session was tagged with a unique `user.cxdeck_t0_run`
value. Cleanup rechecked that value before closing an object. The run reported
zero cleanup errors.

## Supported API findings

The decisive interface is the documented [iTerm2 Python API](https://iterm2.com/python-api/):

```text
App.windows
  Window.window_id
  Window.tabs
    Tab.tab_id
    Tab.root
      Splitter(vertical, children[])
        Splitter | Session
      Session.session_id
      Session.frame
      Session.grid_size
```

[`Tab.root`](https://iterm2.com/python-api/tab.html) is documented as a tree:
interior nodes are `Splitter` objects and leaves are `Session` objects.
[`Splitter`](https://iterm2.com/python-api/session.html) directly exposes its
divider orientation and child list. A vertical divider has left-to-right
children; a horizontal divider has top-to-bottom children. The probe serializes
these as `axis: columns` and `axis: rows` to avoid the ambiguous phrase
"horizontal split." The observed child sequence was stable, and iTerm flattened
three same-orientation panes into one splitter with three ordered children.

The installed Python client receives this structure from the supported
`ListSessions` RPC as ordered `SplitTreeNode.links`; it does not derive it from
`Session.frame`. Supported creation uses
[`Session.async_split_pane`](https://iterm2.com/python-api/session.html), and tab
size adjustment uses `Session.preferred_size` plus `Tab.async_update_layout()`.

The AppleScript dictionary exposes windows, tabs, flat session collections,
GUIDs, TTYs, rows, columns, and horizontal/vertical split commands. It does not
expose splitter nodes or parent-child relationships. AppleScript alone would be
geometry/order only and is not the selected topology interface.

### Live identifiers

| Node | Supported live identifier | Durable workspace identity? |
| --- | --- | --- |
| Window | `Window.window_id` | No; capture verification only |
| Tab | `Tab.tab_id` | No; capture verification only |
| Splitter | None | No; represented by tree position |
| Session | `Session.session_id` (GUID) and TTY variable | No; live view verification only |

Runtime IDs remained stable across refreshed reads and changed when the layout
was reconstructed, as expected. Durable leaves must ultimately contain CX
Deck's exact conversation identity `(host, realpath(CODEX_HOME), UUID)`. T0 did
not implement that mapping.

## Fixtures and structural observations

The expected structures were declared before creation and labeled with synthetic
markers. `columns` means left-to-right and `rows` means top-to-bottom.

| Fixture | Expected structure | Windows | Tabs | Sessions | Direct capture | Round trip |
| --- | --- | ---: | ---: | ---: | --- | --- |
| T0-A | `columns(A, B)` | 1 | 1 | 2 | exact | exact |
| T0-B | `rows(A, B)` | 1 | 1 | 2 | exact | exact |
| T0-C | `columns(A, rows(B, C))` | 1 | 1 | 3 | exact | exact |
| T0-D | `rows(columns(A, B), C)` | 1 | 1 | 3 | exact | exact |
| T0-E | two windows, four tabs, four mixed trees | 2 | 4 | 11 | exact | exact |

T0-E also proved that a splitter can have more than two children. Its second tab
captured `columns(D, E, F)` as one three-child splitter.

## Stability

Each unchanged fixture was captured ten times. All 50 normalized JSON snapshots
were byte-identical. Normalization removed runtime window/tab/session IDs, TTYs,
and geometry while retaining window membership, tab order, split axis, child
order, nesting, and leaf markers.

For T0-A through T0-D, window width was changed toward 160, 120, 100, 80, and 60
total columns. Actual grid spans were within four columns of each target because
window decorations and dividers consume pixels. Window heights were also changed
to 900, 700, and 500 pixels. Every capture retained the exact same split tree.
Changing individual pane preferred sizes altered geometry while preserving the
tree in all four fixtures.

Capture performs two fresh, exact-ID reads and requires the full hierarchy and
geometry to agree. These deliberate changes between reads all failed closed:

| Race | Result |
| --- | --- |
| Pane created | hierarchy change detected |
| Pane closed | hierarchy change detected |
| Tab closed | exact expected tab set changed |
| Window closed | exact expected window disappeared |
| Session moved between windows | hierarchy change detected |
| Window resized | geometry change detected |

No race silently omitted a pane or reassigned a leaf. A production capture can
retry a bounded number of times, but it must return an explicit presentation
error if two complete reads do not converge.

## Reconstruction round trip

For each fixture, the probe:

1. captured the supported structural tree;
2. closed only the tagged disposable source windows;
3. created new disposable windows, tabs, and splits through the Python API;
4. applied captured cell sizes as preferred-size hints;
5. captured the result using fresh runtime IDs; and
6. compared normalized trees.

All five round trips were structurally equal, including the mixed opposite
nestings and the two-window/four-tab fixture. Comparison required the same window
and tab membership, exact split axes, ordered children, nesting, and leaf
markers. Merely producing the same pane count would have failed.

## Ratios and fidelity

`Splitter` has no supported ratio property. `Session.frame` supplies current
pixel geometry and `Session.grid_size` supplies current cell dimensions. The
probe derives per-split ratio hints from child subtree spans and uses
`preferred_size` during reconstruction.

Those hints are useful but approximate. Divider pixels, cell rounding, title and
status UI, profile/font changes, minimum pane dimensions, window size, and screen
configuration affect the realized geometry. In T0-C, for example, the split
tree and cell ratio round-tripped while a nested pane's pixel height changed by
25 pixels. T0-E produced small one-cell ratio differences in several panes.

The achieved fidelity is therefore:

```text
L0 membership                         PASS
L1 windows/tabs/session membership    PASS
L2 exact ordered split tree           PASS
L3 tree + approximate ratios          PASS
L4 exact presentation geometry        NOT PROVEN / NOT RECOMMENDED
```

## Failure modes and limits

- Window stacking order is not documented as durable. A serialized window list
  can express desired reconstruction order, while live window IDs and macOS
  front-to-back order remain transient.
- Exact window placement across monitors, Spaces, DPI changes, and different
  display configurations was not proven. A window frame may be stored only as a
  presentation hint.
- Splitters have no stable IDs. Their durable representation is the ordered tree
  itself.
- A single-session tab arrives as a one-child root splitter whose orientation is
  meaningless; normalization collapses it to the leaf.
- Maximized panes appear through the API's separate minimized-session model and
  were not tested. T1 should reject capture when `Tab.minimized_sessions` is
  nonempty until that state has an explicit contract.
- Exact ratio restoration is impossible to promise through the exposed API.
  Dense layouts may be clamped by iTerm and should produce a presentation
  limitation, never a runtime action.
- The Python API connection refreshes the application hierarchy internally.
  Production capture must select only verified CX Deck view IDs and must not
  serialize unrelated iTerm sessions.

## Candidate durable representation

The evidence supports this minimal provider-neutral shape:

```json
{
  "schema": "cxdeck.workspace-layout/v1",
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
                "identity": {"host": "host", "codex_home": "/path", "thread_id": "UUID-A"}
              },
              {
                "type": "split",
                "axis": "rows",
                "ratio_hints": [0.5, 0.5],
                "children": [
                  {"type": "conversation", "identity": {"host": "host", "codex_home": "/path", "thread_id": "UUID-B"}},
                  {"type": "conversation", "identity": {"host": "host", "codex_home": "/path", "thread_id": "UUID-C"}}
                ]
              }
            ]
          }
        }
      ]
    }
  ]
}
```

`ratio_hints` and `frame_hint` are explicitly best effort. iTerm window, tab,
splitter, session, TTY, and process identifiers do not belong in durable leaves.
Capture-time IDs may exist in an ephemeral receipt solely to prove that both
reads observed the same live objects.

## Recommendation

Proceed with T1 using `Tab.root` as the structural source. Define a versioned
read-only capture contract, require two consistent fresh-ID snapshots, map only
verified CX Deck views to exact conversation leaves, and reject partial,
maximized, moved, or otherwise changing layouts. Preserve the current adaptive
workspace representation and treat ratios/window frames as optional hints.

No change is required to `cx_zmx.py`, zmx labels, Codex UUID handling, resume
policy, or runtime generation identity.
