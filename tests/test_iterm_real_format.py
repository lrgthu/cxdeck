"""Regression for real iTerm2 inventory formatting observed on macOS."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cx_iterm


class RealITermInventoryFormatTests(unittest.TestCase):
    def test_real_literal_tab_output_is_parsed_exactly(self):
        raw = (
            '4BF29193-6C39-46B8-BDFB-53805BECEBA2tab/dev/ttys009\n'
            'EABF488D-61D4-4AEA-833A-23A2872324F9tab/dev/ttys002\n'
            '96B55BF1-279E-49E8-8042-CD3C51EE5728tab/dev/ttys000'
        )
        self.assertEqual(cx_iterm.parse_views(raw), [
            {'guid': '4BF29193-6C39-46B8-BDFB-53805BECEBA2', 'tty': '/dev/ttys009'},
            {'guid': 'EABF488D-61D4-4AEA-833A-23A2872324F9', 'tty': '/dev/ttys002'},
            {'guid': '96B55BF1-279E-49E8-8042-CD3C51EE5728', 'tty': '/dev/ttys000'},
        ])

    def test_current_ascii_tab_output_is_parsed(self):
        raw = '4BF29193-6C39-46B8-BDFB-53805BECEBA2\t/dev/ttys009'
        self.assertEqual(cx_iterm.parse_views(raw), [
            {'guid': '4BF29193-6C39-46B8-BDFB-53805BECEBA2', 'tty': '/dev/ttys009'}
        ])

    def test_arbitrary_literal_tab_text_is_not_accepted(self):
        with self.assertRaises(cx_iterm.StateError):
            cx_iterm.parse_views('not-a-guidtab/dev/ttys009')


if __name__ == '__main__':
    unittest.main()
