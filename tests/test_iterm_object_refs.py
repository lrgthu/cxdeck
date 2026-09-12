import unittest

import cx_iterm


class ITermObjectReferenceTests(unittest.TestCase):
    def test_real_invalid_index_shape_uses_materialized_indexed_collections(self):
        script = cx_iterm.APPLESCRIPT
        for unstable in ('repeat with w in windows', 'repeat with t in tabs of w',
                         'repeat with s in sessions of t', 'every session',
                         'every tab', 'every window'):
            self.assertNotIn(unstable, script)
        for materialized in ('set ws to get windows', 'set ts to get tabs of w',
                             'set ss to get sessions of t',
                             'repeat with wi from 1 to count of ws',
                             'repeat with ti from 1 to count of ts',
                             'repeat with si from 1 to count of ss'):
            self.assertIn(materialized, script)
        self.assertIn('set targetWindow to w', script)
        self.assertIn('set splitPane to s', script)

    def test_layout_uses_scalar_ids_across_collection_mutations(self):
        script = cx_iterm.APPLESCRIPT
        # Never retain application-object specifiers in the adaptive candidate list.
        self.assertIn('set paneGuids to {}', script)
        self.assertIn('set paneGuids to {anchorGuid}', script)
        self.assertIn('repeat with paneGuidRef in paneGuids', script)
        self.assertNotIn('repeat with s in paneList', script)
        self.assertNotIn('set end of paneList to childPane', script)
        self.assertNotIn('set paneList to {childPane}', script)

        # Durable matching uses scalar IDs. Fresh objects are resolved from a
        # materialized collection and used only after traversal has ended.
        self.assertIn('set targetWindowID to (id of w) as text', script)
        self.assertIn('set bestGuid to paneGuid', script)
        self.assertIn('if ((unique id of s) as text) is bestGuid then', script)
        self.assertNotIn('set bestPane to s', script)
        self.assertNotIn('set anchorPane to s', script)

    def test_inventory_materializes_guid_and_tty_as_scalar_text(self):
        script = cx_iterm.APPLESCRIPT
        self.assertIn('set sessionGuid to (get unique id of s) as text', script)
        self.assertIn('set sessionTTY to (get tty of s) as text', script)
        self.assertIn('set output to output & sessionGuid & (ASCII character 9) & sessionTTY',
                      script)
        self.assertNotIn('set output to output & (unique id of s)', script)

    def test_layout_still_uses_returned_objects_only_immediately(self):
        script = cx_iterm.APPLESCRIPT
        # Fresh objects returned by iTerm are read immediately, then converted to IDs.
        self.assertIn('set newWindow to (create window with profile cxProfile command paneCommand)', script)
        self.assertIn('set childGuid to (unique id of childPane) as text', script)
        self.assertIn('set childTTY to (tty of childPane) as text', script)
        self.assertIn('set end of paneGuids to childGuid', script)
        self.assertIn('set variable childPane named "user.cxdeck_name" to paneName', script)


if __name__ == '__main__':
    unittest.main()
