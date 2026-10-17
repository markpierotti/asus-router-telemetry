"""Public-key authentication (``identity_file``) for unattended collection.

Password mode is unchanged and remains the default. Key mode offers exactly one
pinned private key under BatchMode: no password, no agent, no default ~/.ssh key
and no terminal handover. Contacts nothing: the fake ssh stands in for the router.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest

from tests import harness  # noqa: F401 - puts the collector on sys.path

from rtax_telemetry import cli, transport
from rtax_telemetry.core import Collector, OUTCOME_SUCCESS
from rtax_telemetry.errors import TransportAuthError
from rtax_telemetry.store import Store
from rtax_telemetry.transport import SSHTransport

END = "--- end of ssh call ---"
PASSWORD_ONLY = ("PreferredAuthentications=password", "PubkeyAuthentication=no",
                 "NumberOfPasswordPrompts=1")


class KeyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-key-")
        self.key = os.path.join(self.tmp, "id_ed25519")
        with open(self.key, "w") as fh:
            fh.write("-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n"
                     "-----END OPENSSH PRIVATE KEY-----\n")
        os.chmod(self.key, 0o600)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def logging_transport(self, mode="ok", **kw):
        """A harness transport whose ssh logs every argv it is invoked with."""
        tr = harness.make_transport(self.tmp, mode, **kw)
        argv_log = os.path.join(self.tmp, "argv.log")
        logger = os.path.join(self.tmp, "logging-ssh")
        with open(logger, "w") as fh:
            fh.write('#!/bin/bash\nprintf "%%s\\n%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n'
                     % (END, argv_log, tr.ssh_bin))
        os.chmod(logger, 0o755)
        tr.ssh_bin = logger
        return tr, argv_log


class TestAuthOptions(KeyCase):
    def test_password_mode_is_the_unchanged_default(self):
        tr = SSHTransport("192.0.2.1", "u", known_hosts="/tmp/kh")
        self.assertFalse(tr.key_auth)
        joined = " ".join(tr._ssh_opts())
        for opt in PASSWORD_ONLY:
            self.assertIn(opt, joined)
        self.assertNotIn("IdentityFile", joined)
        self.assertNotIn("BatchMode", joined)

    def test_key_mode_offers_only_the_pinned_key(self):
        tr = SSHTransport("192.0.2.1", "u", known_hosts="/tmp/kh", identity_file=self.key)
        self.assertTrue(tr.key_auth)
        opts = tr._ssh_opts()
        joined = " ".join(opts)
        for opt in ("PreferredAuthentications=publickey", "PubkeyAuthentication=yes",
                    "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                    "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes",
                    "IdentityFile=%s" % self.key):
            self.assertIn(opt, joined)
        for opt in PASSWORD_ONLY:
            self.assertNotIn(opt, joined)
        self.assertEqual(sum(1 for o in opts if o.startswith("IdentityFile=")), 1)
        # the hermetic set is untouched
        for opt in ("-F /dev/null", "GlobalKnownHostsFile=/dev/null", "ForwardAgent=no",
                    "StrictHostKeyChecking=yes", "UserKnownHostsFile=/tmp/kh",
                    "ProxyCommand=none"):
            self.assertIn(opt, joined)

    def test_identity_file_tilde_is_expanded(self):
        tr = SSHTransport("192.0.2.1", "u", identity_file="~/k")
        self.assertEqual(tr.identity_file, os.path.expanduser("~/k"))


class TestKeySession(KeyCase):
    def test_full_session_uses_the_key_and_never_takes_the_terminal(self):
        tr, argv_log = self.logging_transport("ok", interactive_auth=True)
        tr.identity_file = self.key
        handovers = []
        real = transport._TerminalHandover

        class Recorder(real):
            def __init__(self, *a, **k):
                handovers.append(a)
                super().__init__(*a, **k)

        transport._TerminalHandover = Recorder
        try:
            session = tr.start()
            self.assertTrue(session.provisioned)
            tr.collect_sample(session)
            tr.stop(session)
            tr.close()
        finally:
            transport._TerminalHandover = real
        self.assertEqual(handovers, [], "key mode must never hand over the terminal")
        with open(argv_log) as fh:
            calls = [c for c in fh.read().split(END) if c.strip()]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("IdentityFile=%s" % self.key, call)
            self.assertIn("PasswordAuthentication=no", call)
            self.assertNotIn("PreferredAuthentications=password", call)
        self.assertTrue(any("ControlMaster=yes" in c and "BatchMode=yes" in c for c in calls),
                        "the master itself must be established under BatchMode")

    def test_end_to_end_through_collector(self):
        tr, _ = self.logging_transport("ok")
        tr.identity_file = self.key
        store = Store(os.path.join(self.tmp, "data"))
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_SUCCESS)
        c.close()

    def test_rejected_key_is_an_auth_failure(self):
        tr, _ = self.logging_transport("authfail")
        tr.identity_file = self.key
        with self.assertRaises(TransportAuthError):
            tr.start()

    def test_missing_key_is_refused_before_any_connection(self):
        tr, argv_log = self.logging_transport("ok")
        tr.identity_file = os.path.join(self.tmp, "absent")
        with self.assertRaises(TransportAuthError):
            tr.start()
        self.assertFalse(os.path.exists(argv_log), "no ssh may run without the key")


class TestIdentityFileValidation(KeyCase):
    def err(self, value, interactive=None):
        return cli.identity_file_error(value, interactive)

    def test_null_keeps_password_mode(self):
        self.assertIsNone(self.err(None))

    def test_a_private_regular_file_is_accepted(self):
        self.assertIsNone(self.err(self.key))
        self.assertIsNone(self.err(self.key, interactive=False))

    def test_unusable_values_are_refused(self):
        os.mkdir(os.path.join(self.tmp, "dir"), 0o700)
        for value in ("", "relative/key", 5, False, ["/k"],
                      os.path.join(self.tmp, "absent"), os.path.join(self.tmp, "dir")):
            self.assertIsNotNone(self.err(value), value)

    def test_group_or_world_access_is_refused(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            os.chmod(self.key, mode)
            why = self.err(self.key)
            self.assertIsNotNone(why, oct(mode))
            self.assertIn("chmod 600", why)

    def test_interactive_auth_true_conflicts(self):
        self.assertIn("never prompts", self.err(self.key, interactive=True))

    def test_identity_file_is_a_known_config_key(self):
        self.assertIn("identity_file", cli.CONFIG_KEYS)


class TestCliKeyMode(KeyCase):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, "data")

    def configure(self, **cfg):
        # the master "goes away" on -O exit, so shutdown is confirmed
        gone = os.path.join(self.tmp, "master.gone")
        harness.write_ssh_config(self.data, self.tmp, "ok",
                                 extra_env={"FAKE_SSH_MASTER_STATE": gone}, **cfg)

    def outcomes(self):
        with open(os.path.join(self.data, "outcomes.jsonl")) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_collect_once_with_a_key_succeeds(self):
        self.configure(identity_file=self.key)
        r = harness.run_cli(["collect", "--once", "--data-dir", self.data])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("public-key authentication", r.stderr)
        self.assertEqual(self.outcomes()[-1]["outcome"], OUTCOME_SUCCESS)

    def test_an_exposed_key_exits_2_before_any_attempt(self):
        os.chmod(self.key, 0o644)
        self.configure(identity_file=self.key)
        r = harness.run_cli(["collect", "--once", "--data-dir", self.data])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("chmod 600", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.data, "outcomes.jsonl")))

    def test_the_flag_overrides_config(self):
        self.configure()
        r = harness.run_cli(["collect", "--once", "--data-dir", self.data,
                             "--identity-file", "relative/key"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("absolute path", r.stderr)

    def test_key_mode_runs_without_a_terminal(self):
        # run_cli has no controlling terminal; password mode could not authenticate
        # there, key mode must not even try to prompt.
        self.configure(identity_file=self.key, interactive_auth=None)
        r = harness.run_cli(["collect", "--once", "--data-dir", self.data])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("no controlling terminal", r.stderr)


if __name__ == "__main__":
    unittest.main()
