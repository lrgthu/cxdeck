"""Reading bundle version does not prove iTerm2 accepts window events."""
import unittest
from unittest.mock import patch
from cx_iterm import ITerm
from cx_store import StateError


class PreflightTests(unittest.TestCase):
    def test_window_preflight_requires_actual_inventory(self):
        gui = ITerm()
        with patch.object(gui, 'call', return_value='3.6.11'), patch.object(gui, 'inventory', side_effect=StateError('first launch blocked')) as inventory:
            with self.assertRaises(StateError):
                gui.preflight('window')
        inventory.assert_called_once()

    def test_split_uses_the_verified_inventory(self):
        gui = ITerm()
        with patch.object(gui, 'call', return_value='3.6.11'), patch.object(gui, 'inventory', return_value=[dict(guid='g', tty='/dev/ttys1')]) as inventory:
            gui.preflight('split', '/dev/ttys1')
        inventory.assert_called_once()
