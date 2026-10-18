"""Regression tests for slim-dashboards rollback with a root-owned backup.

Production backups are root:root 0750. An ordinary operator's unprivileged
`[ -e backup/slim-overview.json ]` is false for a file that exists, because the
directory cannot be searched, so an existence check before escalating would pick the
deletion branch. These tests run the real script as an unprivileged user against a
root-owned backup, with a sudo stand-in that does NOT elevate, and require it to abort
without touching the live dashboards. Tests that need real root run only as root.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'usr' / 'local' / 'bin' / 'slim-dashboards'
IS_ROOT = os.geteuid() == 0
NOBODY = 65534


def drop_privileges():
    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o755)
        self.live = self.tmp / 'live'
        self.backup = self.tmp / 'backup'
        self.live.mkdir()
        self.backup.mkdir()
        # Live state after deploying the overview and new versions of the others.
        self.write(self.live / 'pihole.json', 'pihole-NEW')
        self.write(self.live / 'router.json', 'router-NEW')
        self.write(self.live / 'slim-overview.json', 'overview-NEW')
        # Backup taken before deployment. By default the overview did not exist.
        self.write(self.backup / 'pihole.json', 'pihole-OLD')
        self.write(self.backup / 'router.json', 'router-OLD')
        # A sudo stand-in that runs the command as the caller: no privilege change.
        self.fake_sudo = self.tmp / 'fake-sudo'
        self.fake_sudo.write_text('#!/bin/sh\nexec "$@"\n')
        self.fake_sudo.chmod(0o755)

    @staticmethod
    def write(path, text):
        path.write_text(text)
        path.chmod(0o644)

    def lock_backup(self):
        """Make the backup unreadable to an ordinary operator, like root:root 0750."""
        if IS_ROOT:
            os.chmod(self.backup, 0o750)  # owned by root: nobody gets no access
            for f in self.backup.iterdir():
                os.chmod(f, 0o640)
        else:
            os.chmod(self.backup, 0o000)
            self.addCleanup(os.chmod, self.backup, 0o755)

    def run_script(self, *args, unprivileged=False, sudo=None):
        env = {'PATH': os.environ['PATH'], 'SLIM_DASHBOARD_DIR': str(self.live),
               'SLIM_BACKUP_ROOT': str(self.tmp), 'SLIM_SUDO': str(sudo or self.fake_sudo)}
        kwargs = {'preexec_fn': drop_privileges} if (unprivileged and IS_ROOT) else {}
        return subprocess.run(['sh', str(SCRIPT), *map(str, args)], env=env,
                              capture_output=True, text=True, **kwargs)

    def live_text(self, name):
        return (self.live / f'{name}.json').read_text()

    def assert_live_untouched(self):
        self.assertEqual(self.live_text('pihole'), 'pihole-NEW')
        self.assertEqual(self.live_text('router'), 'router-NEW')
        self.assertEqual(self.live_text('slim-overview'), 'overview-NEW')

    # ---- the regression
    def test_unprivileged_existence_check_cannot_distinguish_absent_from_denied(self):
        """Documents the hazard: the old unprivileged test claims an existing file is absent."""
        self.write(self.backup / 'slim-overview.json', 'overview-OLD')
        self.lock_backup()
        kwargs = {'preexec_fn': drop_privileges} if IS_ROOT else {}
        probe = subprocess.run(['sh', '-c', '[ -e "$1" ] && echo present || echo absent', 'sh',
                                str(self.backup / 'slim-overview.json')],
                               capture_output=True, text=True, **kwargs)
        self.assertEqual(probe.stdout.strip(), 'absent')

    def test_ordinary_operator_with_root_owned_backup_aborts_instead_of_deleting(self):
        self.lock_backup()  # the overview is in neither case deleted by mistake
        for names in (['slim-overview'], ['slim-overview', 'pihole', 'router']):
            with self.subTest(names=names):
                result = self.run_script('rollback', self.backup, *names, unprivileged=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertRegex(result.stderr, 'not readable|could not elevate')
                self.assert_live_untouched()

    def test_ordinary_operator_with_overview_in_backup_is_not_told_it_was_absent(self):
        self.write(self.backup / 'slim-overview.json', 'overview-OLD')
        self.lock_backup()
        result = self.run_script('rollback', self.backup, 'slim-overview', unprivileged=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('removed', result.stdout)
        self.assert_live_untouched()

    # ---- privileged behavior (the elevated process is root)
    @unittest.skipUnless(IS_ROOT, 'needs real root')
    def test_privileged_rollback_removes_overview_only_when_absent_from_backup(self):
        self.lock_backup()
        result = self.run_script('rollback', self.backup, 'slim-overview', 'pihole', 'router')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.live / 'slim-overview.json').exists())
        self.assertEqual(self.live_text('pihole'), 'pihole-OLD')
        self.assertEqual(self.live_text('router'), 'router-OLD')

    @unittest.skipUnless(IS_ROOT, 'needs real root')
    def test_privileged_rollback_restores_overview_when_it_existed(self):
        self.write(self.backup / 'slim-overview.json', 'overview-OLD')
        self.lock_backup()
        result = self.run_script('rollback', self.backup, 'slim-overview')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.live_text('slim-overview'), 'overview-OLD')
        self.assertEqual(self.live_text('pihole'), 'pihole-NEW')  # not requested, untouched

    @unittest.skipUnless(IS_ROOT, 'needs real root')
    def test_overview_only_rollback_leaves_other_dashboards_alone(self):
        result = self.run_script('rollback', self.backup, 'slim-overview')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.live / 'slim-overview.json').exists())
        self.assertEqual(self.live_text('pihole'), 'pihole-NEW')
        self.assertEqual(self.live_text('router'), 'router-NEW')

    # ---- aborts that must not depend on privilege
    def test_missing_backup_aborts(self):
        result = self.run_script('rollback', self.tmp / 'nope', 'slim-overview')
        self.assertNotEqual(result.returncode, 0)
        self.assert_live_untouched()

    @unittest.skipUnless(IS_ROOT, 'needs real root to be past the privilege check')
    def test_invalid_backups_abort_before_any_change(self):
        (self.backup / 'router.json').unlink()  # not a valid backup any more
        result = self.run_script('rollback', self.backup, 'slim-overview', 'pihole')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a valid backup', result.stderr)
        self.assert_live_untouched()

    @unittest.skipUnless(IS_ROOT, 'needs real root to be past the privilege check')
    def test_empty_backup_aborts(self):
        for f in self.backup.iterdir():
            f.unlink()
        result = self.run_script('rollback', self.backup, 'slim-overview')
        self.assertNotEqual(result.returncode, 0)
        self.assert_live_untouched()

    @unittest.skipUnless(IS_ROOT, 'needs real root to be past the privilege check')
    def test_unknown_name_aborts_before_any_change(self):
        result = self.run_script('rollback', self.backup, 'slim-overview', 'node-exporter-full')
        self.assertNotEqual(result.returncode, 0)
        self.assert_live_untouched()

    @unittest.skipUnless(IS_ROOT, 'needs real root to be past the privilege check')
    def test_required_dashboard_missing_from_backup_aborts_not_deletes(self):
        (self.backup / 'pihole.json').unlink()
        result = self.run_script('rollback', self.backup, 'pihole')
        self.assertNotEqual(result.returncode, 0)
        self.assert_live_untouched()

    @unittest.skipUnless(IS_ROOT, 'needs real root')
    def test_backup_command_creates_a_complete_private_backup(self):
        result = self.run_script('backup')
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(dest.parent, self.tmp)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), '0o750')
        for name in ('pihole', 'router', 'slim-overview'):
            self.assertEqual((dest / f'{name}.json').read_text(), self.live_text(name))


if __name__ == '__main__':
    unittest.main()
