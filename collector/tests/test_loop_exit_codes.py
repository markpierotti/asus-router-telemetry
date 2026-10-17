"""Keep loop identity stops distinct from failures a supervisor should retry."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import harness  # noqa: F401
from rtax_telemetry import cli
from rtax_telemetry.errors import ArtifactMismatch, StorageError, TransportHostKeyError
from rtax_telemetry.mock import ScenarioTransport


class LoopExitTests(unittest.TestCase):
    def test_both_identity_failures_stop_after_one_attempt_with_dedicated_code(self):
        for error, outcome in [(TransportHostKeyError, 'ssh_hostkey_mismatch'),
                               (ArtifactMismatch, 'artifact_mismatch')]:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                transport = ScenarioTransport('success', log=lambda *args: None)
                with patch.object(transport, 'start', side_effect=error('synthetic mismatch')) as start, \
                        patch.object(cli, 'build_transport', return_value=(transport, 'synthetic')):
                    rc = cli.main(['collect', '--loop', '--transport', 'mock', '--data-dir', tmp])
                self.assertEqual(rc, 4)
                self.assertEqual(rc, cli.EXIT_HARD_STOP)
                self.assertNotEqual(rc, cli.EXIT_COLLECTION_FAILURE)
                self.assertEqual(start.call_count, 1)
                rows = [json.loads(line) for line in (Path(tmp) / 'outcomes.jsonl').read_text().splitlines()]
                self.assertEqual([row['outcome'] for row in rows], [outcome])

    def test_once_identity_failure_keeps_collection_failure_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = harness.run_cli(['collect', '--once', '--transport', 'mock',
                                      '--mock-scenario', 'hostkey', '--data-dir', tmp])
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_other_cli_errors_keep_retryable_exit_one(self):
        for error in (StorageError('synthetic storage error'), BrokenPipeError()):
            with self.subTest(error=type(error).__name__), \
                    patch.object(cli, 'cmd_status', side_effect=error):
                self.assertEqual(cli.main(['status']), cli.EXIT_COLLECTION_FAILURE)

    def test_example_unit_blocks_only_hard_stop_and_bad_configuration(self):
        root = Path(__file__).resolve().parents[2]
        unit = (root / 'deploy/slim/etc/systemd/system/rtax-collector.service').read_text()
        line = next(line for line in unit.splitlines() if line.startswith('RestartPreventExitStatus='))
        blocked = {int(value) for value in line.split('=', 1)[1].split()}
        self.assertEqual(blocked, {cli.EXIT_HARD_STOP, cli.EXIT_BAD_OPTION})
        self.assertNotIn(cli.EXIT_COLLECTION_FAILURE, blocked)
        self.assertNotIn(cli.EXIT_LOCK_HELD, blocked)
