"""Public-facing product and packaging invariants."""
from pathlib import Path
import unittest

import console_entry
import cx_version
import install


ROOT = Path(__file__).resolve().parents[1]


class PublicHygieneTests(unittest.TestCase):
    def test_product_name_version_and_tagline_are_current(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertEqual(cx_version.VERSION, '0.7.0')
        self.assertIn('# CX Deck', readme)
        self.assertIn('Persistent Codex sessions with a native terminal experience.', readme)
        self.assertIn('independent, unofficial project', readme)
        self.assertIn('CX Deck 0.7.0', console_entry.HELP)
        self.assertNotIn('codex-tmux', console_entry.HELP)

    def test_package_paths_and_install_files_use_cxdeck(self):
        self.assertIn('.local/share/cxdeck/cxdeck.zsh', install.SHIM)
        self.assertIn('cxdeck.zsh', install.FILES)
        self.assertNotIn('codex-tmux.zsh', install.FILES)
        for required in ('LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
                         'install.sh', 'uninstall.sh'):
            self.assertTrue((ROOT / required).is_file(), required)

    def test_runtime_source_has_no_retired_backend_or_transition_commands(self):
        source_files = [path for path in ROOT.glob('*.py') if path.name != 'install.py']
        source_files.append(ROOT / 'cxdeck.zsh')
        text = '\n'.join(path.read_text() for path in source_files)
        for forbidden in ('tmux list-', 'tmux attach', 'tmux send-keys',
                          'SHUTDOWN_ARMED', 'TARGET_LAUNCHED'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
