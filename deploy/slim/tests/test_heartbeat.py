"""Failure routing and secret containment for the external heartbeat."""
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'usr/local/bin/slim-heartbeat'
loader = importlib.machinery.SourceFileLoader('slim_heartbeat', str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
hb = importlib.util.module_from_spec(spec)
loader.exec_module(hb)
URL = 'https://hc-ping.com/00000000-0000-4000-8000-000000000000'


class HeartbeatTests(unittest.TestCase):
    def test_ping_url_only_reaches_curl_stdin(self):
        for healthy, suffix in [(True, ''), (False, '/fail')]:
            with self.subTest(healthy=healthy), patch.object(hb, 'SECRET') as secret, patch.object(hb.subprocess, 'run') as run:
                secret.read_text.return_value = URL
                run.return_value = SimpleNamespace(returncode=0)
                self.assertTrue(hb.notify(healthy))
                args, kwargs = run.call_args
                self.assertNotIn(URL, repr(args))
                self.assertEqual(kwargs['input'], 'url = "' + URL + suffix + '"\n')
                self.assertEqual(kwargs['timeout'], 45)

    def test_invalid_url_is_never_sent(self):
        with patch.object(hb, 'SECRET') as secret, patch.object(hb.subprocess, 'run') as run:
            secret.read_text.return_value = 'https://example.net/untrusted"\noutput=/tmp/leak'
            self.assertFalse(hb.notify(True))
            run.assert_not_called()

    def test_failed_dependency_sends_failure_and_exits_nonzero(self):
        with patch.object(hb, 'dns_ok', return_value=False), patch.object(hb, 'http_ok', return_value=True), patch.object(hb, 'notify', return_value=True) as notify:
            self.assertEqual(hb.main(), 1)
            notify.assert_called_once_with(False)

    def test_healthy_but_undelivered_is_failure(self):
        with patch.object(hb, 'dns_ok', return_value=True), patch.object(hb, 'http_ok', return_value=True), patch.object(hb, 'notify', return_value=False):
            self.assertEqual(hb.main(), 1)

    def test_servfail_or_no_answers_does_not_pass_dns(self):
        for output in ['status: SERVFAIL, ANSWER: 0', 'status: NOERROR, ANSWER: 0', 'status: NOERROR, ANSWER: 1']:
            with patch.object(hb.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertEqual(hb.dns_ok(), output.endswith('ANSWER: 1'))
