"""The ASUS writer must never fall back to an unbounded host filesystem."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('storage', str(ROOT / 'usr/local/bin/slim-syslog-storage'))
spec = importlib.util.spec_from_loader(loader.name, loader)
storage = importlib.util.module_from_spec(spec)
loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def test_image_rejects_wrong_type_owner_permissions_size_or_sparse_allocation(self):
        valid = dict(st_mode=0o100600, st_uid=0, st_size=4096, st_blocks=8)
        for override in ({}, {'st_mode': 0o120600}, {'st_uid': 1000},
                         {'st_mode': 0o100644}, {'st_size': 8192}, {'st_blocks': 1}):
            with self.subTest(override=override), patch.object(Path, 'lstat', return_value=SimpleNamespace(**(valid | override))):
                if override:
                    with self.assertRaises(ValueError):
                        storage.regular_image(Path('/image'), 4096)
                else:
                    storage.regular_image(Path('/image'), 4096)

    def run_check(self, row=None, backing_inode=1, capacity=4096):
        row = row or dict(source='/dev/loop12', target='/logs', fstype='ext4', options='rw,nodev,nosuid,noexec,relatime')
        with patch.object(storage, 'regular_image', return_value=SimpleNamespace(st_dev=2, st_ino=1)), \
             patch.object(storage.subprocess, 'check_output', return_value=json.dumps({'filesystems': [row]})), \
             patch.object(Path, 'read_text', return_value='/image\n'), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_dev=2, st_ino=backing_inode)), \
             patch.object(storage.os, 'statvfs', return_value=SimpleNamespace(f_blocks=capacity, f_frsize=1)):
            storage.check(Path('/image'), Path('/logs'), 4096)

    def test_expected_mount_is_accepted(self):
        self.run_check()

    def test_wrong_source_target_type_options_identity_or_capacity_is_rejected(self):
        original = dict(source='/dev/loop12', target='/logs', fstype='ext4', options='rw,nodev,nosuid,noexec')
        for change in ({'source': '/dev/sda2'}, {'target': '/'}, {'fstype': 'tmpfs'},
                       {'options': 'rw,nodev,nosuid'}, {'options': 'ro,nodev,nosuid,noexec'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_check(original | change)
        with self.assertRaises(ValueError):
            self.run_check(backing_inode=9)
        with self.assertRaises(ValueError):
            self.run_check(capacity=8192)

    def test_missing_mount_is_an_error(self):
        with patch.object(storage, 'regular_image'), \
             patch.object(storage.subprocess, 'check_output', side_effect=storage.subprocess.CalledProcessError(1, 'findmnt')):
            with self.assertRaises(storage.subprocess.CalledProcessError):
                storage.check(Path('/image'), Path('/logs'), 4096)

    def test_rsyslog_start_and_stop_are_bound_to_storage(self):
        unit = (ROOT / 'etc/systemd/system/rsyslog.service.d/asus-storage.conf').read_text()
        for line in ('Requires=var-log-asus.mount', 'After=var-log-asus.mount',
                     'BindsTo=var-log-asus.mount', 'ExecStartPre=/usr/local/bin/slim-syslog-storage check'):
            self.assertIn(line, unit)
        rotate = (ROOT / 'etc/logrotate.d/asus-remote').read_text()
        self.assertIn('/usr/local/bin/slim-syslog-storage check || exit 1', rotate)
        self.assertNotIn('copytruncate', rotate)


if __name__ == '__main__':
    unittest.main()
