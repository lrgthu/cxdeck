"""Installer unit tests using only temporary home directories."""
from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install as installer
import cx_iterm


class InstallerTests(unittest.TestCase):
    def test_idempotent_install_synchronizes_owned_module_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".zshrc").write_text("# personal settings\n")
            (home / ".codex-tmux.zsh").write_text("# old launcher\n")
            old_module = home / ".local/share/codex-tmux"
            old_module.mkdir(parents=True)
            (old_module / "obsolete.py").write_text("old\n")
            old_store = home / '.local/state/codex-tmux/workbench'
            old_store.mkdir(parents=True)
            (old_store / 'state.json').write_text(
                '{"version":1,"agents":{"a":{"name":"Study"}},"views":{},"workspaces":{},"groups":{}}\n')
            source = Path(__file__).resolve().parents[1]
            first = installer.install(home, source, configure_iterm=True)
            installer.install(home, source, configure_iterm=True)
            self.assertEqual((home / ".zshrc").read_text().count(installer.SOURCE_LINE), 1)
            self.assertEqual((first / ".codex-tmux.zsh").read_text(), "# old launcher\n")
            module = home / ".local/share/cxdeck"
            self.assertTrue((module / "cx_zmx.py").exists())
            self.assertTrue((module / "cx_upgrade.py").exists())
            self.assertFalse((module / "obsolete.py").exists())
            self.assertFalse(old_module.exists())
            self.assertTrue((module / installer.INSTALL_MARKER).exists())
            self.assertTrue((home / '.codex-tmux.zsh').exists())
            self.assertEqual((home / '.local/state/cxdeck/workbench/state.json').read_text(),
                             '{"version":1,"agents":{"a":{"name":"Study"}},"views":{},"workspaces":{},"groups":{}}\n')
            self.assertTrue(cx_iterm._profile_path(home).exists())

    def test_preserves_zshrc_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / "dotfile"
            target.write_text("# keep\n")
            (home / ".zshrc").symlink_to(target)
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            self.assertTrue((home / ".zshrc").is_symlink())
            self.assertIn(installer.SOURCE_LINE, target.read_text())

    def test_preserves_zshrc_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            zshrc = home / '.zshrc'
            zshrc.write_text('# keep\n')
            zshrc.chmod(0o640)
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            self.assertEqual(zshrc.stat().st_mode & 0o777, 0o640)
            installer.uninstall(home)
            self.assertEqual(zshrc.stat().st_mode & 0o777, 0o640)

    def test_exact_v060_shell_hook_is_replaced_but_unrelated_hook_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.zshrc').write_text(
                '# personal\n# Persistent Codex sessions / workbench\n' +
                installer.OLD_SOURCE_LINE + '\n')
            (home / '.codex-tmux.zsh').write_text(installer.OLD_SHIM)
            old_module = home / '.local/share/codex-tmux'
            old_module.mkdir(parents=True)
            (old_module / 'codex-tmux.zsh').write_text('# old\n')
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            self.assertFalse((home / '.codex-tmux.zsh').exists())
            self.assertFalse(old_module.exists())
            zshrc = (home / '.zshrc').read_text()
            self.assertIn('# personal', zshrc)
            self.assertNotIn(installer.OLD_SOURCE_LINE, zshrc)
            self.assertEqual(zshrc.count(installer.SOURCE_LINE), 1)

    def test_uninstall_preserves_state_and_codex_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.zshrc').write_text('# settings\n')
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            state = home / '.local/state/cxdeck/workbench/state.json'
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text('{"version":1,"agents":{},"views":{},"workspaces":{},"groups":{}}')
            codex = home / '.codex/history.jsonl'
            codex.parent.mkdir()
            codex.write_text('private-history\n')
            installer.uninstall(home)
            self.assertTrue(state.exists())
            self.assertEqual(codex.read_text(), 'private-history\n')
            self.assertFalse((home / '.local/share/cxdeck').exists())
            self.assertFalse(cx_iterm._profile_path(home).exists())

    def test_installer_never_runs_package_or_runtime_commands(self):
        source = Path(installer.__file__).read_text()
        self.assertNotIn("subprocess", source)
        self.assertIn("Install zmx explicitly", source)
        self.assertIn("Existing zmx sessions were left untouched", source)

    def test_profile_preflight_failure_leaves_install_and_old_state_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.zshrc').write_text('# keep\n')
            old = home / '.local/state/codex-tmux/workbench'
            old.mkdir(parents=True)
            old_data = '{"version":1,"agents":{},"views":{},"workspaces":{},"groups":{}}\n'
            (old / 'state.json').write_text(old_data)
            profile = cx_iterm._profile_path(home)
            profile.parent.mkdir(parents=True)
            profile.write_text('{"Profiles":[{"Guid":"someone-else"}]}')
            with self.assertRaisesRegex(RuntimeError, 'not owned'):
                installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            self.assertEqual((home / '.zshrc').read_text(), '# keep\n')
            self.assertFalse((home / '.local/share/cxdeck').exists())
            self.assertEqual((old / 'state.json').read_text(), old_data)

    def test_uninstall_preflight_failure_removes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.zshrc').write_text('# keep\n')
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            profile = cx_iterm._profile_path(home)
            profile.write_text('{"Profiles":[{"Guid":"someone-else"}]}')
            before = (home / '.zshrc').read_text()
            with self.assertRaisesRegex(RuntimeError, 'not owned'):
                installer.uninstall(home)
            self.assertTrue((home / '.local/share/cxdeck').exists())
            self.assertTrue((home / '.cxdeck.zsh').exists())
            self.assertEqual((home / '.zshrc').read_text(), before)

    def test_uninstall_requires_install_ownership_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            module = home / '.local/share/cxdeck'
            module.mkdir(parents=True)
            (module / 'unrelated').write_text('keep\n')
            with self.assertRaisesRegex(RuntimeError, 'ownership marker'):
                installer.uninstall(home)
            self.assertTrue((module / 'unrelated').exists())

    def test_uninstall_does_not_touch_running_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / '.zshrc').write_text('# keep\n')
            installer.install(home, Path(__file__).resolve().parents[1], configure_iterm=True)
            process = subprocess.Popen(['/bin/sleep', '30'])
            def cleanup():
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            self.addCleanup(cleanup)
            installer.uninstall(home)
            self.assertIsNone(process.poll())

    def test_shell_wrapper_preserves_existing_codex_and_resourcing_is_idempotent(self):
        wrapper = shlex.quote(str(Path(__file__).resolve().parents[1] / 'cxdeck.zsh'))
        alias = subprocess.run(
            ['zsh', '-f', '-c', f"alias codex='print -- preserved-alias'; source {wrapper}; eval codex"],
            text=True, capture_output=True, check=True)
        self.assertEqual(alias.stdout.strip(), 'preserved-alias')
        function = subprocess.run(
            ['zsh', '-f', '-c', f"codex() {{ print -- preserved-function }}; source {wrapper}; codex"],
            text=True, capture_output=True, check=True)
        self.assertEqual(function.stdout.strip(), 'preserved-function')
        own = subprocess.run(
            ['zsh', '-f', '-c', f"source {wrapper}; first=${{functions[codex]}}; "
             f"source {wrapper}; [[ \"$first\" == \"${{functions[codex]}}\" ]]"],
            text=True, capture_output=True)
        self.assertEqual(own.returncode, 0, own.stderr)

    def test_shell_wrapper_bypass_and_managed_calls_route_to_codex_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'codex'
            binary.write_text('#!/bin/sh\nprintf "direct:%s\\n" "$*"\n')
            binary.chmod(0o755)
            wrapper = shlex.quote(str(Path(__file__).resolve().parents[1] / 'cxdeck.zsh'))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            script = (f'source {wrapper}; CX_WRAP_CODEX=0 codex bypass; '
                      'CX_MANAGED=1 codex managed')
            result = subprocess.run(['zsh', '-f', '-c', script], env=env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ['direct:bypass', 'direct:managed'])


if __name__ == "__main__":
    unittest.main()
