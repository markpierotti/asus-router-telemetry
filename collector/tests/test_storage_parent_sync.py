"""Directory sync must target the directory whose entries were changed."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import harness  # noqa: F401
from rtax_telemetry import store


class ParentSyncTests(unittest.TestCase):
    def test_sample_rename_syncs_samples_directory_after_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = store.Store(tmp)
            events = []
            real_open, real_fsync, real_replace = os.open, os.fsync, os.replace
            opened = {}

            def tracked_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                opened[fd] = Path(path)
                return fd

            def tracked_fsync(fd):
                if fd in opened:
                    events.append(('sync', opened[fd]))
                return real_fsync(fd)

            def tracked_replace(source, destination):
                result = real_replace(source, destination)
                events.append(('replace', Path(destination).parent))
                return result

            with patch.object(store.os, 'open', side_effect=tracked_open), \
                    patch.object(store.os, 'fsync', side_effect=tracked_fsync), \
                    patch.object(store.os, 'replace', side_effect=tracked_replace):
                sample = data.save_sample({'synthetic': True}, when=1700000000)
            self.assertTrue(sample.exists())
            self.assertEqual(events, [('replace', data.samples_dir), ('sync', data.samples_dir)])

    def test_state_and_pruned_outcomes_sync_the_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = store.Store(tmp, retain_outcomes=1)
            with patch.object(data, '_fsync_dir', wraps=data._fsync_dir) as sync:
                data.write_state({'consecutive_failures': 0})
                data.append_outcome({'at': 'first'})
                data.append_outcome({'at': 'second'})
            self.assertEqual([call.args for call in sync.call_args_list], [(data.dir,), (data.dir,)])
            self.assertEqual(data.outcomes(), [{'at': 'second'}])
