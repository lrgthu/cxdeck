import asyncio
import copy
import unittest

from tools.iterm_topology_probe import (
    CaptureChanged,
    FIXTURES,
    LiveProbe,
    _axis_span,
    add_approximate_ratios,
    canonical_json,
    fixture_leaf_count,
    leaf,
    normalize_snapshot,
    normalize_tree,
    split,
    structural_equal,
)


def snapshot(root, *, window_id="w1", tab_id="t1"):
    return {"windows": [{"window_id": window_id, "tabs": [{"tab_id": tab_id, "root": root}]}]}


def captured_leaf(marker, session_id, tty, columns=40, rows=20, x=0, y=0):
    return {
        "type": "session",
        "marker": marker,
        "session_id": session_id,
        "tty": tty,
        "grid": {"columns": columns, "rows": rows},
        "frame_pixels": {"x": x, "y": y, "width": columns * 10, "height": rows * 20},
    }


class TopologyNormalizationTests(unittest.TestCase):
    def test_fixture_matrix_has_required_shapes_and_large_fixture(self):
        self.assertEqual(fixture_leaf_count(FIXTURES["T0-A"]), 2)
        self.assertEqual(fixture_leaf_count(FIXTURES["T0-B"]), 2)
        self.assertEqual(fixture_leaf_count(FIXTURES["T0-C"]), 3)
        self.assertEqual(fixture_leaf_count(FIXTURES["T0-D"]), 3)
        self.assertGreaterEqual(fixture_leaf_count(FIXTURES["T0-E"]), 5)
        self.assertGreaterEqual(len(FIXTURES["T0-E"]), 2)
        self.assertGreaterEqual(sum(len(window) for window in FIXTURES["T0-E"]), 2)

    def test_split_requires_supported_axis_and_two_children(self):
        with self.assertRaises(ValueError):
            split("diagonal", leaf("A"), leaf("B"))
        with self.assertRaises(ValueError):
            split("columns", leaf("A"))

    def test_singleton_root_is_collapsed(self):
        root = {"type": "split", "axis": "rows", "children": [captured_leaf("A", "s1", "/dev/ttys1")]}
        self.assertEqual(normalize_tree(root), leaf("A"))

    def test_runtime_identifiers_are_not_durable_structure(self):
        left = snapshot({
            "type": "split",
            "axis": "columns",
            "children": [
                captured_leaf("A", "old-a", "/dev/ttys1"),
                captured_leaf("B", "old-b", "/dev/ttys2", x=401),
            ],
        }, window_id="old-window", tab_id="old-tab")
        right = copy.deepcopy(left)
        right["windows"][0]["window_id"] = "new-window"
        right["windows"][0]["tabs"][0]["tab_id"] = "new-tab"
        for index, node in enumerate(right["windows"][0]["tabs"][0]["root"]["children"]):
            node["session_id"] = f"new-{index}"
            node["tty"] = f"/dev/ttys{index + 8}"
        self.assertTrue(structural_equal(left, right))

    def test_order_and_nesting_are_structural(self):
        a = captured_leaf("A", "a", "/dev/a")
        b = captured_leaf("B", "b", "/dev/b")
        c = captured_leaf("C", "c", "/dev/c")
        first = snapshot({"type": "split", "axis": "columns", "children": [a, {"type": "split", "axis": "rows", "children": [b, c]}]})
        reordered = snapshot({"type": "split", "axis": "columns", "children": [a, {"type": "split", "axis": "rows", "children": [c, b]}]})
        opposite = snapshot({"type": "split", "axis": "rows", "children": [{"type": "split", "axis": "columns", "children": [a, b]}, c]})
        self.assertFalse(structural_equal(first, reordered))
        self.assertFalse(structural_equal(first, opposite))

    def test_geometry_can_be_retained_separately(self):
        item = snapshot(captured_leaf("A", "s1", "/dev/ttys1"))
        durable = normalize_snapshot(item)
        geometry = normalize_snapshot(item, geometry=True)
        self.assertNotIn("grid", durable["windows"][0]["tabs"][0]["root"])
        self.assertEqual(geometry["windows"][0]["tabs"][0]["root"]["grid"]["columns"], 40)

    def test_approximate_ratios_come_from_grid_spans(self):
        root = {
            "type": "split",
            "axis": "columns",
            "children": [
                captured_leaf("A", "a", "/dev/a", columns=30),
                captured_leaf("B", "b", "/dev/b", columns=70),
            ],
        }
        measured = add_approximate_ratios(root)
        self.assertEqual(measured["approximate_ratios"], [0.3, 0.7])
        self.assertEqual(_axis_span(measured), (100, 20))

    def test_multi_child_split_is_preserved(self):
        root = split("columns", leaf("A"), leaf("B"), leaf("C"))
        self.assertEqual([x["marker"] for x in normalize_tree(root)["children"]], ["A", "B", "C"])

    def test_canonical_json_is_byte_deterministic(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))


class ConsistentCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_consistent_double_read_returns_second_snapshot(self):
        record = snapshot(captured_leaf("A", "s1", "/dev/a"))
        probe = object.__new__(LiveProbe)
        reads = [record, copy.deepcopy(record)]

        async def capture_once(_fixture):
            return reads.pop(0)

        probe.capture_once = capture_once
        self.assertEqual(await probe.capture_consistent(object(), attempts=1), record)

    async def test_mutation_between_reads_fails_closed(self):
        first = snapshot(captured_leaf("A", "s1", "/dev/a"))
        second = snapshot(captured_leaf("B", "s2", "/dev/b"))
        probe = object.__new__(LiveProbe)
        reads = [first, second]

        async def capture_once(_fixture):
            return reads.pop(0)

        async def mutation():
            await asyncio.sleep(0)

        probe.capture_once = capture_once
        with self.assertRaises(CaptureChanged):
            await probe.capture_consistent(object(), between_reads=mutation, attempts=1)

    async def test_disappearance_exception_is_reported_as_changed(self):
        first = snapshot(captured_leaf("A", "s1", "/dev/a"))
        probe = object.__new__(LiveProbe)
        calls = 0

        async def capture_once(_fixture):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            raise RuntimeError("pane disappeared")

        probe.capture_once = capture_once
        with self.assertRaisesRegex(CaptureChanged, "pane disappeared"):
            await probe.capture_consistent(object(), attempts=1)


if __name__ == "__main__":
    unittest.main()
