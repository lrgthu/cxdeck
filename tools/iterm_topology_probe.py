#!/usr/bin/env python3
"""Disposable iTerm2 split-topology experiment for CX Deck v0.8 T0.

This tool never reads terminal contents or talks to Codex/zmx.  Its live mode
creates windows whose only process is ``/bin/sleep``, tags every created object
with a per-run iTerm user variable, and closes only objects whose tag matches.

Run on macOS with iTerm2's Python API enabled:

    uv run --isolated --with 'iterm2==2.23' \
      python tools/iterm_topology_probe.py run
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


RUN_VARIABLE = "user.cxdeck_t0_run"
MARKER_VARIABLE = "user.cxdeck_t0_marker"
SLEEP_COMMAND = "/bin/sleep 600"


class ProbeError(RuntimeError):
    """Base error for a failed or inconclusive topology observation."""


class CaptureChanged(ProbeError):
    """The observed hierarchy changed across the two capture reads."""


def leaf(marker: str) -> Dict[str, Any]:
    return {"type": "session", "marker": marker}


def split(axis: str, *children: Dict[str, Any]) -> Dict[str, Any]:
    if axis not in ("columns", "rows"):
        raise ValueError("split axis must be columns or rows")
    if len(children) < 2:
        raise ValueError("a split needs at least two children")
    return {"type": "split", "axis": axis, "children": list(children)}


FIXTURES: Dict[str, List[List[Dict[str, Any]]]] = {
    # windows -> tabs -> root.  "columns" means left-to-right children and a
    # vertical divider; "rows" means top-to-bottom and a horizontal divider.
    "T0-A": [[split("columns", leaf("A"), leaf("B"))]],
    "T0-B": [[split("rows", leaf("A"), leaf("B"))]],
    "T0-C": [[split("columns", leaf("A"), split("rows", leaf("B"), leaf("C"))) ]],
    "T0-D": [[split("rows", split("columns", leaf("A"), leaf("B")), leaf("C"))]],
    "T0-E": [
        [
            split("columns", leaf("A"), split("rows", leaf("B"), leaf("C"))),
            split("columns", leaf("D"), leaf("E"), leaf("F")),
        ],
        [
            split("rows", split("columns", leaf("G"), leaf("H")), leaf("I")),
            split("rows", leaf("J"), leaf("K")),
        ],
    ],
}


def _frame_dict(frame: Any) -> Dict[str, int]:
    return {
        "x": int(frame.origin.x),
        "y": int(frame.origin.y),
        "width": int(frame.size.width),
        "height": int(frame.size.height),
    }


def _grid_dict(grid: Any) -> Dict[str, int]:
    return {"columns": int(grid.width), "rows": int(grid.height)}


def _axis_span(node: Dict[str, Any]) -> Tuple[int, int]:
    """Return the approximate (columns, rows) bounding span of a captured tree."""
    if node["type"] == "session":
        grid = node.get("grid", {})
        return int(grid.get("columns", 0)), int(grid.get("rows", 0))
    sizes = [_axis_span(child) for child in node["children"]]
    if node["axis"] == "columns":
        return sum(width for width, _ in sizes), max((height for _, height in sizes), default=0)
    return max((width for width, _ in sizes), default=0), sum(height for _, height in sizes)


def add_approximate_ratios(node: Dict[str, Any]) -> Dict[str, Any]:
    """Add ratios derived from leaf grid sizes; the API exposes no splitter ratio."""
    copied = dict(node)
    if node["type"] == "session":
        return copied
    copied["children"] = [add_approximate_ratios(child) for child in node["children"]]
    axis_index = 0 if node["axis"] == "columns" else 1
    spans = [_axis_span(child)[axis_index] for child in node["children"]]
    total = sum(spans)
    copied["approximate_ratios"] = [round(value / total, 6) if total else None for value in spans]
    return copied


def normalize_tree(node: Dict[str, Any], *, geometry: bool = False) -> Dict[str, Any]:
    """Remove live IDs; optionally retain dimensions and approximate ratios."""
    if node["type"] == "session":
        result: Dict[str, Any] = {"type": "session", "marker": node.get("marker")}
        if geometry:
            result["grid"] = node.get("grid")
            result["frame_pixels"] = node.get("frame_pixels")
        return result
    children = [normalize_tree(child, geometry=geometry) for child in node["children"]]
    # iTerm represents a one-pane tab as a Splitter with one leaf. Its
    # orientation has no structural meaning, so durable normalization removes it.
    if len(children) == 1:
        return children[0]
    result = {"type": "split", "axis": node["axis"], "children": children}
    if geometry:
        result["approximate_ratios"] = node.get("approximate_ratios")
    return result


def normalize_snapshot(snapshot: Dict[str, Any], *, geometry: bool = False) -> Dict[str, Any]:
    return {
        "windows": [
            {
                "tabs": [
                    {"root": normalize_tree(tab["root"], geometry=geometry)}
                    for tab in window["tabs"]
                ]
            }
            for window in snapshot["windows"]
        ]
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def structural_equal(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return canonical_json(normalize_snapshot(left)) == canonical_json(normalize_snapshot(right))


def count_leaves(node: Dict[str, Any]) -> int:
    if node["type"] == "session":
        return 1
    return sum(count_leaves(child) for child in node["children"])


def fixture_leaf_count(spec: Sequence[Sequence[Dict[str, Any]]]) -> int:
    return sum(count_leaves(root) for window in spec for root in window)


def first_leaf_record(node: Dict[str, Any]) -> Dict[str, Any]:
    if node["type"] == "session":
        return node
    if not node.get("children"):
        raise ProbeError("split node has no children")
    return first_leaf_record(node["children"][0])


@dataclass
class LiveFixture:
    name: str
    run_id: str
    window_ids: List[str]
    tab_ids_by_window: List[List[str]]


class LiveProbe:
    def __init__(self, iterm2_module: Any, connection: Any, app: Any, run_id: str):
        self.iterm2 = iterm2_module
        self.connection = connection
        self.app = app
        self.run_id = run_id
        self.created_window_ids: List[str] = []

    def _sleep_profile(self) -> Any:
        profile = self.iterm2.LocalWriteOnlyProfile()
        profile.set_use_custom_command(self.iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
        profile.set_command(SLEEP_COMMAND)
        return profile

    async def _refresh(self) -> None:
        await self.app.async_refresh()
        await asyncio.sleep(0.05)

    async def _fresh_window(self, window_id: str) -> Any:
        await self._refresh()
        window = self.app.get_window_by_id(window_id)
        if window is None:
            raise ProbeError(f"window disappeared: {window_id}")
        return window

    async def _owned_window(self, window_id: str) -> Any:
        window = await self._fresh_window(window_id)
        owner = await window.async_get_variable(RUN_VARIABLE)
        if owner != self.run_id:
            raise ProbeError(f"refusing to mutate unowned window {window_id}")
        return window

    async def _owned_session(self, session_id: str) -> Any:
        await self._refresh()
        session = self.app.get_session_by_id(session_id)
        if session is None:
            raise ProbeError(f"session disappeared: {session_id}")
        owner = await session.async_get_variable(RUN_VARIABLE)
        if owner != self.run_id:
            raise ProbeError(f"refusing to mutate unowned session {session_id}")
        return session

    async def _mark_leaf(self, session: Any, marker: str) -> None:
        await session.async_set_variable(RUN_VARIABLE, self.run_id)
        await session.async_set_variable(MARKER_VARIABLE, marker)

    async def _realize_tree(self, anchor: Any, node: Dict[str, Any]) -> None:
        if node["type"] == "session":
            await self._mark_leaf(anchor, node["marker"])
            return
        sessions = [anchor]
        cursor = anchor
        for _ in node["children"][1:]:
            cursor = await cursor.async_split_pane(
                vertical=node["axis"] == "columns",
                before=False,
                profile_customizations=self._sleep_profile(),
            )
            sessions.append(cursor)
        for session, child in zip(sessions, node["children"]):
            await self._realize_tree(session, child)

    async def _new_window(self, root: Dict[str, Any]) -> Tuple[Any, str]:
        window = await self.iterm2.Window.async_create(self.connection, command=SLEEP_COMMAND)
        if window is None:
            raise ProbeError("new disposable window exited immediately")
        self.created_window_ids.append(window.window_id)
        await window.async_set_variable(RUN_VARIABLE, self.run_id)
        window = await self._fresh_window(window.window_id)
        if not window.tabs or not window.tabs[0].sessions:
            raise ProbeError("new disposable window did not expose its first session")
        tab = window.tabs[0]
        await tab.async_set_variable(RUN_VARIABLE, self.run_id)
        await self._realize_tree(tab.sessions[0], root)
        return window, tab.tab_id

    async def _new_tab(self, window_id: str, root: Dict[str, Any]) -> str:
        window = await self._owned_window(window_id)
        tab = await window.async_create_tab(command=SLEEP_COMMAND, select=False)
        if tab is None or not tab.sessions:
            raise ProbeError("new disposable tab exited immediately")
        await tab.async_set_variable(RUN_VARIABLE, self.run_id)
        await self._realize_tree(tab.sessions[0], root)
        return tab.tab_id

    async def create_fixture(self, name: str, spec: Sequence[Sequence[Dict[str, Any]]]) -> LiveFixture:
        window_ids: List[str] = []
        tabs_by_window: List[List[str]] = []
        for tab_roots in spec:
            window, first_tab_id = await self._new_window(tab_roots[0])
            window_ids.append(window.window_id)
            tab_ids = [first_tab_id]
            for root in tab_roots[1:]:
                tab_ids.append(await self._new_tab(window.window_id, root))
            tabs_by_window.append(tab_ids)
        await self._refresh()
        return LiveFixture(name, self.run_id, window_ids, tabs_by_window)

    async def _serialize_node(self, node: Any) -> Dict[str, Any]:
        if isinstance(node, self.iterm2.Session):
            return {
                "type": "session",
                "marker": await node.async_get_variable(MARKER_VARIABLE),
                "session_id": node.session_id,
                "tty": await node.async_get_variable("tty"),
                "grid": _grid_dict(node.grid_size),
                "frame_pixels": _frame_dict(node.frame),
            }
        result = {
            "type": "split",
            "axis": "columns" if node.vertical else "rows",
            "children": [await self._serialize_node(child) for child in node.children],
        }
        return add_approximate_ratios(result)

    async def capture_once(self, fixture: LiveFixture) -> Dict[str, Any]:
        await self._refresh()
        windows = []
        for window_id, expected_tabs in zip(fixture.window_ids, fixture.tab_ids_by_window):
            window = self.app.get_window_by_id(window_id)
            if window is None:
                raise ProbeError(f"window disappeared during capture: {window_id}")
            if await window.async_get_variable(RUN_VARIABLE) != self.run_id:
                raise ProbeError(f"window ownership changed during capture: {window_id}")
            actual_tabs = [tab for tab in window.tabs if tab.tab_id in expected_tabs]
            actual_ids = [tab.tab_id for tab in actual_tabs]
            if actual_ids != expected_tabs:
                raise ProbeError(
                    f"tab set/order changed during capture: expected {expected_tabs}, got {actual_ids}")
            tabs = []
            for tab in actual_tabs:
                if await tab.async_get_variable(RUN_VARIABLE) != self.run_id:
                    raise ProbeError(f"tab ownership changed during capture: {tab.tab_id}")
                tabs.append({"tab_id": tab.tab_id, "root": await self._serialize_node(tab.root)})
            windows.append({"window_id": window_id, "tabs": tabs})
        return {"windows": windows}

    async def capture_consistent(
        self,
        fixture: LiveFixture,
        *,
        between_reads: Optional[Callable[[], Awaitable[None]]] = None,
        attempts: int = 3,
    ) -> Dict[str, Any]:
        last_detail = ""
        for attempt in range(attempts):
            try:
                first = await self.capture_once(fixture)
                if between_reads is not None and attempt == 0:
                    await between_reads()
                second = await self.capture_once(fixture)
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                if attempt + 1 == attempts:
                    raise CaptureChanged(last_detail) from exc
                continue
            if canonical_json(first) == canonical_json(second):
                return second
            last_detail = "hierarchy or geometry changed between capture reads"
            if attempt + 1 == attempts:
                break
        raise CaptureChanged(last_detail)

    async def close_window(self, window_id: str) -> None:
        try:
            window = await self._owned_window(window_id)
        except ProbeError as exc:
            if "window disappeared" in str(exc):
                return
            raise
        await window.async_close(force=True)
        await asyncio.sleep(0.1)

    async def close_fixture(self, fixture: LiveFixture) -> None:
        for window_id in list(fixture.window_ids):
            await self.close_window(window_id)

    async def cleanup_all(self) -> List[str]:
        errors = []
        for window_id in list(dict.fromkeys(self.created_window_ids)):
            try:
                await self.close_window(window_id)
            except Exception as exc:  # Keep trying only other known disposable IDs.
                errors.append(f"{window_id}: {type(exc).__name__}: {exc}")
        return errors

    async def resize_fixture(self, fixture: LiveFixture, expected: Dict[str, Any]) -> List[Dict[str, Any]]:
        results = []
        window_id = fixture.window_ids[0]
        for target_columns in (160, 120, 100, 80, 60):
            window = await self._owned_window(window_id)
            before = await self.capture_consistent(fixture)
            width_now, _ = _axis_span(before["windows"][0]["tabs"][0]["root"])
            frame = await window.async_get_frame()
            new_width = max(360, int(frame.size.width * target_columns / max(width_now, 1)))
            new_frame = self.iterm2.Frame(
                frame.origin,
                self.iterm2.Size(new_width, int(frame.size.height)),
            )
            await window.async_set_frame(new_frame)
            await asyncio.sleep(0.2)
            after = await self.capture_consistent(fixture)
            achieved, _ = _axis_span(after["windows"][0]["tabs"][0]["root"])
            results.append({
                "requested_columns": target_columns,
                "achieved_columns": achieved,
                "tree_unchanged": normalize_tree(after["windows"][0]["tabs"][0]["root"]) == expected,
            })
        for target_height in (900, 700, 500):
            window = await self._owned_window(window_id)
            frame = await window.async_get_frame()
            await window.async_set_frame(self.iterm2.Frame(
                frame.origin,
                self.iterm2.Size(int(frame.size.width), target_height),
            ))
            await asyncio.sleep(0.2)
            after = await self.capture_consistent(fixture)
            results.append({
                "requested_window_height_pixels": target_height,
                "tree_unchanged": normalize_tree(after["windows"][0]["tabs"][0]["root"]) == expected,
            })
        return results

    async def adjust_split_boundary(self, fixture: LiveFixture) -> Dict[str, Any]:
        snapshot = await self.capture_consistent(fixture)
        leaves = []
        def collect(node: Dict[str, Any]) -> None:
            if node["type"] == "session":
                leaves.append(node)
            else:
                for child in node["children"]:
                    collect(child)
        collect(snapshot["windows"][0]["tabs"][0]["root"])
        if len(leaves) < 2:
            return {"attempted": False}
        session = await self._owned_session(leaves[0]["session_id"])
        tab = self.app.get_tab_by_id(fixture.tab_ids_by_window[0][0])
        if tab is None:
            raise ProbeError("tab disappeared before boundary adjustment")
        grid = leaves[0]["grid"]
        session.preferred_size = self.iterm2.Size(
            max(20, grid["columns"] + 7), max(5, grid["rows"] + 3))
        await tab.async_update_layout()
        await asyncio.sleep(0.2)
        after = await self.capture_consistent(fixture)
        return {
            "attempted": True,
            "tree_unchanged": structural_equal(snapshot, after),
            "geometry_changed": canonical_json(normalize_snapshot(snapshot, geometry=True)) != canonical_json(normalize_snapshot(after, geometry=True)),
        }

    async def _apply_captured_sizes(self, fixture: LiveFixture, source: Dict[str, Any]) -> None:
        desired: Dict[str, Dict[str, int]] = {}
        def collect(node: Dict[str, Any]) -> None:
            if node["type"] == "session":
                desired[node["marker"]] = node["grid"]
            else:
                for child in node["children"]:
                    collect(child)
        for window in source["windows"]:
            for tab in window["tabs"]:
                collect(tab["root"])
        current = await self.capture_once(fixture)
        for window in current["windows"]:
            for tab_record in window["tabs"]:
                def sessions(node: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
                    if node["type"] == "session":
                        yield node
                    else:
                        for child in node["children"]:
                            yield from sessions(child)
                for leaf_record in sessions(tab_record["root"]):
                    size = desired.get(leaf_record["marker"])
                    session = self.app.get_session_by_id(leaf_record["session_id"])
                    if size and session:
                        session.preferred_size = self.iterm2.Size(size["columns"], size["rows"])
                tab = self.app.get_tab_by_id(tab_record["tab_id"])
                if tab:
                    await tab.async_update_layout()
        await asyncio.sleep(0.2)

    async def reconstruct(self, name: str, captured: Dict[str, Any]) -> LiveFixture:
        spec = [[normalize_tree(tab["root"]) for tab in window["tabs"]] for window in captured["windows"]]
        fixture = await self.create_fixture(name, spec)
        await self._apply_captured_sizes(fixture, captured)
        return fixture


def _macos_version() -> str:
    try:
        return subprocess.check_output(["sw_vers", "-productVersion"], text=True).strip()
    except Exception:
        return platform.mac_ver()[0]


def _iterm_app_version() -> str:
    try:
        return subprocess.check_output(
            ["defaults", "read", "/Applications/iTerm.app/Contents/Info", "CFBundleShortVersionString"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "UNKNOWN"


async def _run_live(connection: Any, iterm2_module: Any) -> Dict[str, Any]:
    app = await iterm2_module.async_get_app(connection)
    run_id = "cxdeck-t0-" + uuid.uuid4().hex
    probe = LiveProbe(iterm2_module, connection, app, run_id)
    output: Dict[str, Any] = {
        "schema": 1,
        "run_id": run_id,
        "environment": {
            "macos": _macos_version(),
            "iterm2": _iterm_app_version(),
            "iterm2_python_package": metadata.version("iterm2"),
            "interfaces": ["iTerm2 Python API"],
        },
        "api": {
            "structural_tree": "Tab.root -> Splitter(vertical, ordered children) -> Session",
            "window_id": "Window.window_id",
            "tab_id": "Tab.tab_id",
            "session_id": "Session.session_id",
            "session_frame": "Session.frame (pixels, relative to containing splitter)",
            "session_grid": "Session.grid_size (cells)",
            "split_ratio_property": False,
        },
        "fixtures": {},
        "races": {},
    }
    try:
        for name in ("T0-A", "T0-B", "T0-C", "T0-D", "T0-E"):
            spec = FIXTURES[name]
            fixture = await probe.create_fixture(name, spec)
            original = await probe.capture_consistent(fixture)
            normalized = normalize_snapshot(original)
            expected = {"windows": [{"tabs": [{"root": root} for root in tabs]} for tabs in spec]}
            repeated = []
            for _ in range(10):
                repeated.append(canonical_json(normalize_snapshot(await probe.capture_consistent(fixture))))
            resize = []
            boundary = {"attempted": False}
            if name != "T0-E":
                expected_root = expected["windows"][0]["tabs"][0]["root"]
                resize = await probe.resize_fixture(fixture, expected_root)
                boundary = await probe.adjust_split_boundary(fixture)
            original_for_roundtrip = await probe.capture_consistent(fixture)
            await probe.close_fixture(fixture)
            rebuilt = await probe.reconstruct(name + "-roundtrip", original_for_roundtrip)
            reconstructed = await probe.capture_consistent(rebuilt)
            output["fixtures"][name] = {
                "windows": len(spec),
                "tabs": sum(len(window) for window in spec),
                "sessions": fixture_leaf_count(spec),
                "observed_matches_expected": normalized == expected,
                "repeat_captures": len(repeated),
                "repeat_normalized_byte_identical": len(set(repeated)) == 1,
                "resize": resize,
                "split_boundary": boundary,
                "roundtrip_structurally_equal": structural_equal(original_for_roundtrip, reconstructed),
                "original": normalize_snapshot(original_for_roundtrip, geometry=True),
                "reconstructed": normalize_snapshot(reconstructed, geometry=True),
            }
            await probe.close_fixture(rebuilt)

        # Creation during capture.
        fixture = await probe.create_fixture("race-create", [[split("columns", leaf("A"), leaf("B"))]])
        baseline = await probe.capture_once(fixture)
        target_id = baseline["windows"][0]["tabs"][0]["root"]["children"][-1]["session_id"]
        async def create_pane() -> None:
            target = await probe._owned_session(target_id)
            child = await target.async_split_pane(vertical=False, profile_customizations=probe._sleep_profile())
            await probe._mark_leaf(child, "C")
        try:
            await probe.capture_consistent(fixture, between_reads=create_pane, attempts=1)
            output["races"]["pane_created"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["pane_created"] = f"FAIL_CLOSED: {exc}"
        await probe.close_fixture(fixture)

        # Pane disappearance during capture.
        fixture = await probe.create_fixture("race-pane-close", [[split("columns", leaf("A"), leaf("B"))]])
        baseline = await probe.capture_once(fixture)
        closing_id = baseline["windows"][0]["tabs"][0]["root"]["children"][-1]["session_id"]
        async def close_pane() -> None:
            session = await probe._owned_session(closing_id)
            await session.async_close(force=True)
        try:
            await probe.capture_consistent(fixture, between_reads=close_pane, attempts=1)
            output["races"]["pane_disappeared"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["pane_disappeared"] = f"FAIL_CLOSED: {exc}"
        await probe.close_fixture(fixture)

        # Tab disappearance while another tab keeps the disposable window alive.
        fixture = await probe.create_fixture("race-tab-close", [[leaf("A"), leaf("B")]])
        closing_tab_id = fixture.tab_ids_by_window[0][1]
        async def close_tab() -> None:
            await probe._refresh()
            tab = probe.app.get_tab_by_id(closing_tab_id)
            if tab is None or await tab.async_get_variable(RUN_VARIABLE) != run_id:
                raise ProbeError("refusing to close unowned tab")
            await tab.async_close(force=True)
        try:
            await probe.capture_consistent(fixture, between_reads=close_tab, attempts=1)
            output["races"]["tab_disappeared"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["tab_disappeared"] = f"FAIL_CLOSED: {exc}"
        await probe.close_fixture(fixture)

        # Window disappearance.
        fixture = await probe.create_fixture("race-window-close", [[leaf("A")]])
        async def close_window() -> None:
            await probe.close_window(fixture.window_ids[0])
        try:
            await probe.capture_consistent(fixture, between_reads=close_window, attempts=1)
            output["races"]["window_disappeared"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["window_disappeared"] = f"FAIL_CLOSED: {exc}"

        # A resize is a race for the ratio/geometry snapshot and must retry/fail.
        fixture = await probe.create_fixture("race-resize", [[split("columns", leaf("A"), leaf("B"))]])
        async def resize_window() -> None:
            window = await probe._owned_window(fixture.window_ids[0])
            frame = await window.async_get_frame()
            await window.async_set_frame(iterm2_module.Frame(
                frame.origin, iterm2_module.Size(int(frame.size.width) + 137, int(frame.size.height) + 53)))
            await asyncio.sleep(0.15)
        try:
            await probe.capture_consistent(fixture, between_reads=resize_window, attempts=1)
            output["races"]["rapid_resize"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["rapid_resize"] = f"FAIL_CLOSED: {exc}"
        await probe.close_fixture(fixture)

        # Supported API move operation during capture.
        fixture = await probe.create_fixture("race-move", [[split("columns", leaf("A"), leaf("B"))], [leaf("C")]])
        baseline = await probe.capture_once(fixture)
        source_id = baseline["windows"][0]["tabs"][0]["root"]["children"][-1]["session_id"]
        destination_id = first_leaf_record(baseline["windows"][1]["tabs"][0]["root"])["session_id"]
        async def move_pane() -> None:
            source = await probe._owned_session(source_id)
            destination = await probe._owned_session(destination_id)
            await probe.app.async_move_session(source, destination, split_vertically=True, before=False)
            await asyncio.sleep(0.15)
        try:
            await probe.capture_consistent(fixture, between_reads=move_pane, attempts=1)
            output["races"]["session_moved"] = "MISSED"
        except CaptureChanged as exc:
            output["races"]["session_moved"] = f"FAIL_CLOSED: {exc}"
        await probe.close_fixture(fixture)

        fixtures_ok = all(
            result["observed_matches_expected"]
            and result["repeat_normalized_byte_identical"]
            and result["roundtrip_structurally_equal"]
            and all(item["tree_unchanged"] for item in result["resize"])
            and result["split_boundary"].get("tree_unchanged", True)
            for result in output["fixtures"].values()
        )
        races_ok = all(str(value).startswith("FAIL_CLOSED") for value in output["races"].values())
        output["result"] = {
            "structural_tests_passed": fixtures_ok,
            "race_tests_failed_closed": races_ok,
            "highest_fidelity": "L3",
            "ratio_fidelity": "approximate; derived from per-session grid/frame geometry",
            "verdict": "EXACT_TREE_AVAILABLE_WITH_LIMITATIONS" if fixtures_ok and races_ok else "NOT_RELIABLE",
        }
    finally:
        output["cleanup_errors"] = await probe.cleanup_all()
        output["finished_at_unix"] = int(time.time())
    return output


def run_live() -> int:
    try:
        import iterm2  # type: ignore
    except ImportError:
        print(json.dumps({
            "error": "missing iterm2 Python package",
            "run": "uv run --isolated --with 'iterm2==2.23' python tools/iterm_topology_probe.py run",
        }, indent=2))
        return 2

    holder: Dict[str, Any] = {}
    async def main(connection: Any) -> None:
        holder["result"] = await _run_live(connection, iterm2)
    try:
        incidental_stdout = io.StringIO()
        with contextlib.redirect_stdout(incidental_stdout):
            iterm2.run_until_complete(main)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    noise = incidental_stdout.getvalue().strip()
    if noise:
        holder["result"]["iterm2_incidental_stdout"] = noise
    print(json.dumps(holder["result"], indent=2, sort_keys=True))
    result = holder["result"].get("result", {})
    return 0 if (
        result.get("structural_tests_passed")
        and result.get("race_tests_failed_closed")
        and not holder["result"].get("cleanup_errors")
    ) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "fixtures"), nargs="?", default="fixtures")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_live()
    print(json.dumps(FIXTURES, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
