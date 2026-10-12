"""Offline regressions for the second follow-up (findings A and B).

A focused review of `5059031` (collector 1.5.1) reproduced two remaining gaps:

  A  provisioning facts were still lost for the classification branches that did
     not keep their own exception - `unreachable` (the concrete blocker), `timeout`
     and the authentication classes among them
  B  a cancellation during the control-master exit request was caught and
     swallowed, so the run did not end as interrupted and could even report
     `shutdown-verified`

Nothing here contacts a router. The transport is the real `SSHTransport`, driven
either by the fake `ssh` process used across the suite or - where a cancellation
has to land at an exact point - by a scripted stand-in for `run_bounded`, so that
no process is started at all and only the network-facing call is replaced.

Tests whose docstring starts with REGRESSION fail on `5059031` and pass after the
correction. Tests whose docstring starts with GUARD pass on both: they pin what the
correction must NOT change (the outcome class, a live session's ownership, the
absence of any process signalling).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import cli, errors, transport                        # noqa: E402
from rtax_telemetry.mock import MockSession                              # noqa: E402
from rtax_telemetry.core import (                                        # noqa: E402
    OUTCOME_ARTIFACT, OUTCOME_AUTH, OUTCOME_HOSTKEY, OUTCOME_INTERNAL,
    OUTCOME_MALFORMED, OUTCOME_MASTER_LOST, OUTCOME_OVERFLOW, OUTCOME_PROVISION,
    OUTCOME_REPORTER_FAILURE, OUTCOME_SCHEMA, OUTCOME_STORAGE, OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT, OUTCOME_TRANSPORT, OUTCOME_UNREACHABLE, Collector,
)
from rtax_telemetry.errors import (                                      # noqa: E402
    ArtifactMismatch, CleanupError, ControlMasterLost, LockHeld, MalformedData,
    OutputLimitExceeded, ProvisioningError, QuotaExceeded, RemoteCommandError,
    RemoteTimeout, SchemaInvalid, StorageError, TelemetryError, TransportAuthError,
    TransportError, TransportHostKeyError, TransportUnreachableError,
)
from rtax_telemetry.store import Store                                   # noqa: E402

# What transport.MS_* is expected to be called once the correction is in. Spelled
# out so these tests fail by ASSERTION on the earlier code instead of erroring on a
# missing name.
MS_EXIT_INTERRUPTED = "shutdown-unconfirmed: the -O exit command was interrupted"
MS_PROBE_INTERRUPTED = "shutdown-unconfirmed: the follow-up probe was interrupted"

DIR_RE = re.compile(r"/tmp/rt\.[0-9a-f]{8}")


class _Unnamed(Exception):
    """Something the taxonomy does not name (the classifier's final catch-all)."""


# Every branch of the classifier in `Collector.collect_once`, in its order, with the
# outcome that branch records. `test_the_matrix_covers_the_whole_taxonomy` keeps
# this honest against `rtax_telemetry.errors`.
CLASSIFICATION = [
    (lambda: MalformedData("reporter output was not JSON"), OUTCOME_MALFORMED),
    (lambda: SchemaInvalid("a required field has the wrong type"), OUTCOME_SCHEMA),
    (lambda: ProvisioningError("the remote tool probe failed"), OUTCOME_PROVISION),
    (lambda: ControlMasterLost("the master went away"), OUTCOME_MASTER_LOST),
    (lambda: TransportAuthError("ssh authentication was refused"), OUTCOME_AUTH),
    (lambda: TransportHostKeyError("pinned host key did not match"), OUTCOME_HOSTKEY),
    (lambda: TransportUnreachableError("management address unreachable: broken pipe"),
     OUTCOME_UNREACHABLE),
    (lambda: RemoteTimeout("command exceeded 18s and was killed"), OUTCOME_TIMEOUT),
    (lambda: RemoteCommandError(1, "reporter exited 1"), OUTCOME_REPORTER_FAILURE),
    (lambda: ArtifactMismatch("router-side SHA-256 mismatch"), OUTCOME_ARTIFACT),
    (lambda: OutputLimitExceeded("stdout budget exceeded"), OUTCOME_OVERFLOW),
    (lambda: QuotaExceeded("the sample exceeds the per-write budget"), OUTCOME_STORAGE),
    (lambda: StorageError("evidence filesystem refused the write"), OUTCOME_STORAGE),
    (lambda: CleanupError("a removal failed"), OUTCOME_TRANSPORT),
    (lambda: TransportError("ssh failed"), OUTCOME_TRANSPORT),
    (lambda: LockHeld("another run owns the data directory"), OUTCOME_TRANSPORT),
    (lambda: TelemetryError("a deliberate error with no narrower class"),
     OUTCOME_TRANSPORT),
    (lambda: _Unnamed("something the taxonomy does not name"), OUTCOME_INTERNAL),
    (lambda: OSError("the ssh binary would not run"), OUTCOME_INTERNAL),
]

FIXTURE_DIR = "/tmp/rt.a11fac75"
FIXTURE_DETAIL = "failed to remove router dir %s (rc=1)" % FIXTURE_DIR


def _with_facts(exc, remote_dir=FIXTURE_DIR, ownership="owned", cleanup_ok=False,
                cleanup_detail=FIXTURE_DETAIL, **more):
    """Attach provisioning facts exactly the way `SSHTransport.start()` does."""
    facts = {"remote_dir": remote_dir, "remote_ownership": ownership,
             "cleanup_ok": cleanup_ok, "cleanup_detail": cleanup_detail}
    facts.update(more)
    setattr(exc, "provision_facts", facts)
    setattr(exc, "remote_dir", remote_dir)
    setattr(exc, "remote_ownership", ownership)
    setattr(exc, "provision_cleanup_ok", cleanup_ok)
    setattr(exc, "provision_cleanup_detail", cleanup_detail)
    return exc


class _StartFails(object):
    """A local double whose `start()` raises a chosen exception."""

    name = "double"
    lifecycle_error = None
    master_shutdown = None

    def __init__(self, exc):
        self.exc = exc
        self.stopped = []

    def start(self):
        raise self.exc

    def stop(self, session):
        self.stopped.append(session)


class _CollectFails(object):
    """A local double that provisions a LIVE session and then fails to collect."""

    name = "double"
    lifecycle_error = None
    master_shutdown = None

    def __init__(self, exc):
        self.exc = exc
        self.session = MockSession(remote_dir="/tmp/rt.11fe5e55", ownership="owned")
        self.stopped = []

    def start(self):
        return self.session

    def collect_sample(self, session):
        raise self.exc

    def stop(self, session):
        self.stopped.append(session)


class _ScriptedSsh(object):
    """Stands in for `transport.run_bounded`. NO process is started.

    Only the network-facing call is replaced: the real `start()`, `_provision`,
    `_rsh`, the real stderr classifier, `_dispose`, `_master_check` and the real
    shutdown all run against it. It plays the device (the known-answer test, the
    tool probe and an ACKNOWLEDGED mkdir always succeed) and the local ssh client's
    control commands. Any scripted reply may be an exception: raising
    KeyboardInterrupt from here is how an operator's cancellation arrives while the
    collector is waiting on ssh.
    """

    OK = (0, b"", b"")
    ALIVE = (0, b"", b"Master running (pid=4242)\r\n")
    ABSENT = (255, b"", b"Control socket connect(/nowhere/cm): No such file or directory\r\n")
    LINK_LOST = (255, b"", b"mux_client_request_session: read from master failed: "
                           b"Broken pipe\r\n")
    REMOVED = (0, b"REMOVED\n", b"")
    REMOVAL_FAILED = (1, b"", b"REMOVAL_FAILED\n")

    def __init__(self, after_mkdir=LINK_LOST, removal=REMOVED, exit_request=OK,
                 check_after_exit=ABSENT):
        self.after_mkdir = after_mkdir
        self.removal = removal
        self.exit_request = exit_request
        self.check_after_exit = check_after_exit
        self.ops = []               # "check" / "exit" / the command text
        self.exit_seen = False

    def _reply(self, argv):
        if "-O" in argv:
            op = argv[argv.index("-O") + 1]
            self.ops.append(op)
            if op == "exit":
                self.exit_seen = True
                return self.exit_request
            return self.check_after_exit if self.exit_seen else self.ALIVE
        cmd = argv[-1]
        self.ops.append(cmd)
        if "printf abc" in cmd:
            return (0, ("SHA256(stdin)= %s\n" % transport.OPENSSL_KAT_ABC).encode(), b"")
        if "for t in dd" in cmd:
            return (0, b"TOOLS_OK\n", b"")
        if "mkdir -m 700" in cmd:
            return self.OK
        if "rm -rf" in cmd:
            return self.removal
        return self.after_mkdir

    def __call__(self, argv, timeout, **kwargs):
        reply = self._reply(list(argv))
        if isinstance(reply, BaseException) or (
                isinstance(reply, type) and issubclass(reply, BaseException)):
            raise reply
        return reply

    def count(self, op):
        return sum(1 for o in self.ops if o == op)

    def made_dir(self):
        """The exact path the 'device' was asked to create."""
        for o in self.ops:
            if "mkdir -m 700" in o:
                return DIR_RE.search(o).group(0)
        return None

    def after(self, op):
        """Everything that was run after the first `op`."""
        return self.ops[self.ops.index(op) + 1:]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr5.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def store(self, name="data"):
        store = Store(os.path.join(self.tmp, name))
        store.ensure()
        return store

    def teardowns(self, store):
        return [r for r in store.outcomes() if r.get("record") == "teardown"]

    def scripted_transport(self, ssh, name="work"):
        """A real SSHTransport whose only replaced dependency is `run_bounded`.

        Establishing a master needs a server, so `_start_master` is replaced by one
        that creates the local control-socket directory and marks the master up -
        exactly the state a successful `ssh -f -N` leaves behind.
        """
        work = os.path.join(self.tmp, name)
        os.makedirs(work)
        tr = harness.make_transport(work, "ok")

        def _synthetic_master():
            tr._ctrl_dir = tempfile.mkdtemp(prefix="cm.", dir=work)
            tr._ctrl_path = os.path.join(tr._ctrl_dir, "cm")
            tr._master_up = True

        tr._start_master = _synthetic_master
        patcher = mock.patch.object(transport, "run_bounded", ssh)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tr

    def live_session(self, tr):
        """As after a successful provision: a master, and an OWNED directory."""
        tr._start_master()
        session = transport.SSHSession("/tmp/rt.5e55104e", tr.artifact_sha)
        session.ownership = "owned"
        session.provisioned = True
        return session


# ---------------------------------------------------------------------------
# A - provisioning facts survive EVERY classification branch
# ---------------------------------------------------------------------------
class TestAProvisioningFacts(_Base):

    def test_facts_survive_every_classification_branch(self):
        """REGRESSION: no branch of the classifier may decide whether facts survive.

        On `5059031` this fails for MalformedData, SchemaInvalid, TransportAuthError,
        TransportHostKeyError, TransportUnreachableError, RemoteTimeout,
        QuotaExceeded, LockHeld and TelemetryError: each recorded `cleanup_ok: true`,
        ownership `none` and no path, and queued nothing.
        """
        for make, expected in CLASSIFICATION:
            exc = _with_facts(make())
            with self.subTest(exc=type(exc).__name__):
                store = self.store("facts-" + type(exc).__name__)
                attempt = Collector(_StartFails(exc), store, host_label="bench").collect_once()

                self.assertEqual(attempt["outcome"], expected,
                                 "the original outcome classification was changed")
                self.assertEqual(attempt["remote_dir"], FIXTURE_DIR)
                self.assertEqual(attempt["remote_ownership"], "owned")
                self.assertIs(attempt["cleanup_ok"], False,
                              "an unconfirmed removal was recorded as cleanup_ok")
                self.assertEqual(attempt["cleanup_detail"], FIXTURE_DETAIL)

                # durable: read back from disk through a fresh Store
                disk = Store(store.dir)
                pend = disk.pending_cleanup()
                self.assertEqual(len(pend), 1, "no pending-cleanup evidence was written")
                self.assertEqual(pend[0]["remote_dir"], FIXTURE_DIR)
                self.assertEqual(pend[0]["remote_ownership"], "owned")
                self.assertEqual(pend[0]["reason"], FIXTURE_DETAIL)
                self.assertIs(pend[0]["manual_reconciliation_required"], True)
                self.assertIs(pend[0]["automatic_deletion"], False)
                recorded = disk.outcomes()[-1]
                for key in ("outcome", "remote_dir", "remote_ownership", "cleanup_ok",
                            "cleanup_detail"):
                    self.assertEqual(recorded[key], attempt[key])
                last = disk.load_state()["last_attempt"]
                self.assertIs(last["cleanup_ok"], False)
                self.assertEqual(last["remote_dir"], FIXTURE_DIR)

    def test_the_matrix_covers_the_whole_taxonomy(self):
        """GUARD: a class added to `errors` cannot stay out of the matrix above."""
        covered = set(type(make()) for make, _ in CLASSIFICATION)
        taxonomy = set(
            obj for obj in vars(errors).values()
            if isinstance(obj, type) and issubclass(obj, TelemetryError))
        self.assertEqual(taxonomy - covered, set(),
                         "error classes with no facts-preservation coverage")

    def test_without_facts_the_outcome_and_the_live_ownership_are_kept(self):
        """GUARD: an exception that carries no facts changes nothing.

        The session is live and owns its directory; whatever ends the collection,
        the attempt keeps that session's path and ownership and the class's own
        outcome, and nothing is queued for reconciliation.
        """
        for make, expected in CLASSIFICATION:
            exc = make()
            with self.subTest(exc=type(exc).__name__):
                store = self.store("live-" + type(exc).__name__)
                tr = _CollectFails(exc)
                attempt = Collector(tr, store, host_label="bench").collect_once()
                self.assertEqual(attempt["outcome"], expected)
                self.assertEqual(attempt["remote_dir"], "/tmp/rt.11fe5e55")
                self.assertEqual(attempt["remote_ownership"], "owned",
                                 "the live session ownership was overwritten")
                self.assertIs(attempt["cleanup_ok"], True)
                self.assertIsNone(attempt["cleanup_detail"])
                self.assertEqual(Store(store.dir).pending_cleanup(), [])

    def test_a_start_failure_without_facts_records_no_directory(self):
        """GUARD: a failure before any remote name was chosen invents nothing."""
        for make, expected in CLASSIFICATION:
            exc = make()
            with self.subTest(exc=type(exc).__name__):
                store = self.store("bare-" + type(exc).__name__)
                attempt = Collector(_StartFails(exc), store, host_label="bench").collect_once()
                self.assertEqual(attempt["outcome"], expected)
                self.assertIsNone(attempt["remote_dir"])
                self.assertEqual(attempt["remote_ownership"], "none")
                self.assertIs(attempt["cleanup_ok"], True)
                self.assertEqual(Store(store.dir).pending_cleanup(), [])

    def test_unreachable_after_acknowledged_mkdir_with_failed_removal(self):
        """REGRESSION: the real `SSHTransport.start()` against the fake `ssh` process.

        The device acknowledges the mkdir, then the link drops: the next command and
        the removal both exit 255 with a network diagnostic. The real classifier
        makes that `unreachable`, and the directory is still on the router. On
        `5059031` the attempt recorded no path, ownership `none`, `cleanup_ok: true`
        and no pending-cleanup entry.
        """
        store = self.store()
        cmd_log = os.path.join(self.tmp, "device-commands.log")
        tr = harness.make_transport(
            self.tmp, "unreachable_after_mkdir",
            extra_env={"FAKE_SSH_CMD_LOG": cmd_log,
                       "FAKE_SSH_MASTER_STATE": os.path.join(self.tmp, "master.gone")})
        attempt = Collector(tr, store, host_label="bench").collect_once()

        with open(cmd_log) as fh:
            sent = fh.read()
        made = re.search(r'mkdir -m 700 "(/tmp/rt\.[0-9a-f]{8})"', sent)
        self.assertIsNotNone(made, "the device was never asked to create a directory")
        path = made.group(1)
        self.assertIn('rm -rf "%s"' % path, sent, "no removal of that path was attempted")

        self.assertEqual(attempt["outcome"], OUTCOME_UNREACHABLE, attempt["detail"])
        self.assertIn("management address unreachable", attempt["detail"])
        self.assertIn("does not prove the router is down", attempt["detail"])
        self.assertEqual(attempt["remote_dir"], path)
        self.assertEqual(attempt["remote_ownership"], "owned")
        self.assertIs(attempt["cleanup_ok"], False)
        self.assertIn("failed to remove router dir %s" % path, attempt["cleanup_detail"])

        disk = Store(store.dir)
        pend = disk.pending_cleanup()
        self.assertEqual(len(pend), 1, "no durable pending-cleanup evidence")
        self.assertEqual(pend[0]["remote_dir"], path)
        self.assertEqual(pend[0]["remote_ownership"], "owned")
        self.assertIs(pend[0]["manual_reconciliation_required"], True)
        self.assertIs(pend[0]["automatic_deletion"], False)
        recorded = disk.outcomes()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["outcome"], OUTCOME_UNREACHABLE)
        self.assertEqual(recorded[0]["remote_dir"], path)
        self.assertEqual(recorded[0]["remote_ownership"], "owned")
        self.assertIs(recorded[0]["cleanup_ok"], False)
        self.assertIs(disk.load_state()["last_attempt"]["cleanup_ok"], False)
        # the master itself was stopped and verified: only the directory is uncertain
        self.assertEqual(tr.master_shutdown, transport.MS_GONE)

    def test_real_start_keeps_facts_whatever_ended_the_provision(self):
        """REGRESSION: the real `start()` with only `run_bounded` replaced.

        What the device does after acknowledging the mkdir decides the exception
        class; the facts must not depend on it. On `5059031` this fails for the link
        loss (`unreachable`) case; the other two pin classes that already worked.
        """
        cases = [
            ("link lost", _ScriptedSsh.LINK_LOST, OUTCOME_UNREACHABLE),
            ("local deadline", RemoteTimeout("command exceeded 15s and was killed: ssh"),
             OUTCOME_PROVISION),        # _psh names the provisioning stage
            ("output flood", OutputLimitExceeded("stdout exceeded 1048576 bytes"),
             OUTCOME_OVERFLOW),
        ]
        for label, after_mkdir, expected in cases:
            with self.subTest(case=label):
                slug = label.replace(" ", "-")
                store = self.store("data-" + slug)
                ssh = _ScriptedSsh(after_mkdir=after_mkdir,
                                   removal=_ScriptedSsh.REMOVAL_FAILED)
                tr = self.scripted_transport(ssh, name="work-" + slug)
                attempt = Collector(tr, store, host_label="bench").collect_once()
                path = ssh.made_dir()
                self.assertIsNotNone(path)
                self.assertEqual(attempt["outcome"], expected, attempt["detail"])
                self.assertEqual(attempt["remote_dir"], path)
                self.assertEqual(attempt["remote_ownership"], "owned")
                self.assertIs(attempt["cleanup_ok"], False)
                self.assertEqual(attempt["cleanup_detail"],
                                 "failed to remove router dir %s (rc=1)" % path)
                self.assertEqual([p["remote_dir"] for p in Store(store.dir).pending_cleanup()],
                                 [path])


# ---------------------------------------------------------------------------
# A + B - a CANCELLATION that leaves start() carries the facts too
# ---------------------------------------------------------------------------
class TestCancelledProvision(_Base):

    def _assert_recorded(self, store, path, interrupted, detail_part):
        disk = Store(store.dir)
        td = self.teardowns(disk)
        self.assertEqual(len(td), 1, "no durable teardown record was written")
        self.assertEqual(td[0]["outcome"], "cleanup_failure")
        self.assertEqual(td[0]["remote_dir"], path)
        self.assertEqual(td[0]["remote_ownership"], "owned")
        self.assertIs(td[0]["cleanup_ok"], False)
        self.assertIs(td[0]["cleanup_interrupted"], interrupted)
        self.assertIn(detail_part, td[0]["cleanup_detail"])
        pend = disk.pending_cleanup()
        self.assertEqual(len(pend), 1, "the uncertain directory was not queued")
        self.assertEqual(pend[0]["remote_dir"], path)
        self.assertEqual(pend[0]["remote_ownership"], "owned")
        self.assertIs(pend[0]["manual_reconciliation_required"], True)
        self.assertIs(pend[0]["automatic_deletion"], False)
        return td[0]

    def test_cancelled_provision_with_failed_removal_is_recorded(self):
        """REGRESSION: Ctrl-C during provisioning, after the mkdir was acknowledged.

        `start()` tries the removal, it fails, and the KeyboardInterrupt leaves
        with the facts. It is not classified, so on `5059031` nothing read them: no
        record, no pending entry, and a directory left on the router unnamed.
        """
        store = self.store()
        ssh = _ScriptedSsh(after_mkdir=KeyboardInterrupt,
                           removal=_ScriptedSsh.REMOVAL_FAILED)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        c.close()
        path = ssh.made_dir()
        self._assert_recorded(store, path, False, "failed to remove router dir %s" % path)
        self.assertEqual([r for r in store.outcomes() if r.get("record") != "teardown"], [],
                         "a cancelled attempt must not be given an outcome")

    def test_cancellation_during_the_removal_after_a_failed_provision(self):
        """REGRESSION: provisioning fails (unreachable), then Ctrl-C lands on the rm.

        The cancellation must leave `start()` - and take the facts with it, marked
        as an INTERRUPTED removal. On `5059031` it left bare.
        """
        store = self.store()
        ssh = _ScriptedSsh(removal=KeyboardInterrupt)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        with self.assertRaises(KeyboardInterrupt) as cm:
            c.collect_once()
        self.assertIsInstance(cm.exception.__context__, TransportUnreachableError,
                              "the failure that was being handled was not kept as context")
        c.close()
        td = self._assert_recorded(store, ssh.made_dir(), True, "INTERRUPTED")
        self.assertEqual(td["master_shutdown"], transport.MS_GONE)

    def test_cancellation_during_master_exit_after_a_failed_provision(self):
        """REGRESSION: unreachable after mkdir, failed removal, Ctrl-C on `-O exit`.

        The three things at once. The cancellation propagates, the directory's
        facts are durable with the removal's OWN failure reason, and the master's
        verdict says the exit request was interrupted. On `5059031` the
        cancellation was swallowed and the attempt was then recorded as a clean
        `unreachable` with no path.
        """
        store = self.store()
        ssh = _ScriptedSsh(removal=_ScriptedSsh.REMOVAL_FAILED,
                           exit_request=KeyboardInterrupt,
                           check_after_exit=_ScriptedSsh.ALIVE)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        c.close()
        path = ssh.made_dir()
        td = self._assert_recorded(store, path, False,
                                   "failed to remove router dir %s (rc=1)" % path)
        self.assertEqual(td["master_shutdown"], MS_EXIT_INTERRUPTED)
        self.assertIn(MS_EXIT_INTERRUPTED, td["lifecycle_error"] or "")
        self.assertIsNone(tr._ctrl_dir, "the local control-socket directory leaked")

    def test_a_cleanly_reconciled_cancelled_provision_queues_nothing(self):
        """GUARD: a cancelled provision whose removal SUCCEEDED is not an incident."""
        store = self.store()
        ssh = _ScriptedSsh(after_mkdir=KeyboardInterrupt)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        c.close()
        self.assertIsNone(c.cleanup_error)
        self.assertEqual(Store(store.dir).pending_cleanup(), [])
        self.assertEqual(self.teardowns(Store(store.dir)), [])

    def test_cli_sigint_during_provisioning_after_mkdir(self):
        """REGRESSION: the same through the CLI, with a real SIGINT to a real process.

        The self-test (the step after mkdir) hangs, SIGINT arrives, and the removal
        fails. Expect exit 130, a teardown record naming the directory the device
        was asked to create, and a pending-cleanup entry for exactly that path.
        """
        data = os.path.join(self.tmp, "data")
        cmd_log = os.path.join(self.tmp, "device-commands.log")
        harness.write_ssh_config(data, self.tmp, mode="selftest_hangs_cleanup_fail",
                                 extra_env={"FAKE_SSH_CMD_LOG": cmd_log,
                                            "FAKE_SSH_MASTER_STATE":
                                                os.path.join(self.tmp, "master.gone")})
        proc = _spawn_collect(data)
        _wait_for(lambda: "if=/dev/zero" in _read(cmd_log), proc,
                  "the provision never reached the step after mkdir")
        out, err = _interrupt(self, proc)
        self.assertEqual(proc.returncode, cli.EXIT_INTERRUPTED, err)
        path = re.search(r'mkdir -m 700 "(/tmp/rt\.[0-9a-f]{8})"', _read(cmd_log)).group(1)
        self._assert_recorded(Store(data), path, False,
                              "failed to remove router dir %s" % path)
        self.assertNotEqual(_mock_run(data).returncode, cli.EXIT_LOCK_HELD,
                            "the data-dir lock leaked")


# ---------------------------------------------------------------------------
# B - cancellation during the control-master exit
# ---------------------------------------------------------------------------
class TestBCancellationDuringMasterExit(_Base):

    VARIANTS = [
        ("absent socket", _ScriptedSsh.ABSENT, transport.MS_GONE, False),
        ("answering master", _ScriptedSsh.ALIVE, MS_EXIT_INTERRUPTED, True),
    ]

    def test_exit_master_reraises_after_one_bounded_check(self):
        """REGRESSION: the review's reproduction, called the way the review called it.

        On `5059031` `_exit_master()` RETURNED - `shutdown-verified` with an absent
        socket - and the KeyboardInterrupt was gone.
        """
        for label, after, _result, _unconfirmed in self.VARIANTS:
            with self.subTest(then=label):
                ssh = _ScriptedSsh(exit_request=KeyboardInterrupt, check_after_exit=after)
                tr = self.scripted_transport(ssh, name="w-" + label.replace(" ", "-"))
                tr._start_master()
                with self.assertRaises(KeyboardInterrupt):
                    tr._exit_master()
                self.assertEqual(ssh.count("exit"), 1,
                                 "an interrupted exit request must not be retried")
                self.assertEqual(ssh.after("exit"), ["check"],
                                 "the cancellation must wait for exactly one bounded check")

    def test_close_records_the_verdict_releases_then_reraises(self):
        """REGRESSION: verdict recorded, resources released, cancellation re-raised.

        With an absent socket the follow-up check is a positive observation, so the
        verdict is `shutdown-verified` and nothing is unconfirmed - but the run is
        still cancelled. With an answering master the shutdown is unconfirmed and
        says why. No process is signalled in either case.
        """
        for label, after, result, unconfirmed in self.VARIANTS:
            with self.subTest(then=label):
                ssh = _ScriptedSsh(exit_request=KeyboardInterrupt, check_after_exit=after)
                tr = self.scripted_transport(ssh, name="w-" + label.replace(" ", "-"))
                tr._start_master()
                ctrl_dir = tr._ctrl_dir
                with mock.patch.object(os, "kill") as kill, \
                        mock.patch.object(os, "killpg") as killpg:
                    with self.assertRaises(KeyboardInterrupt):
                        tr.close()
                self.assertEqual(kill.call_args_list + killpg.call_args_list, [],
                                 "a process was signalled")
                self.assertEqual(tr.master_shutdown, result)
                self.assertEqual(transport.shutdown_is_unconfirmed(tr.master_shutdown),
                                 unconfirmed)
                if unconfirmed:
                    self.assertIn(result, tr.lifecycle_error)
                    self.assertIn("No process was signalled", tr.lifecycle_error)
                else:
                    self.assertIsNone(tr.lifecycle_error)
                self.assertFalse(os.path.exists(ctrl_dir),
                                 "the local control-socket directory was not released")
                self.assertIsNone(tr._ctrl_path)
                self.assertFalse(tr._master_up)
                self.assertFalse(hasattr(tr, "_kill_master_pid"),
                                 "the PID-killing fallback is back")

    def test_cancellation_during_the_follow_up_check_is_unknown_not_gone(self):
        """REGRESSION: the exit request completed; Ctrl-C lands on `-O check`.

        The master's state is UNKNOWN. On `5059031` the cancellation propagated but
        recorded nothing about the master at all.
        """
        ssh = _ScriptedSsh(check_after_exit=KeyboardInterrupt)
        tr = self.scripted_transport(ssh)
        tr._start_master()
        with self.assertRaises(KeyboardInterrupt):
            tr.close()
        self.assertEqual(tr.master_shutdown, MS_PROBE_INTERRUPTED)
        self.assertTrue(transport.shutdown_is_unconfirmed(tr.master_shutdown))
        self.assertIn(MS_PROBE_INTERRUPTED, tr.lifecycle_error or "")
        self.assertIsNone(tr._ctrl_dir)

    def test_an_uninterrupted_shutdown_is_unchanged(self):
        """GUARD: the vocabulary for a shutdown nobody cancelled is what it was."""
        expected = [
            (_ScriptedSsh.OK, _ScriptedSsh.ABSENT, transport.MS_GONE),
            (_ScriptedSsh.OK, _ScriptedSsh.ALIVE, transport.MS_STILL_ANSWERING),
            ((255, b"", b"boom"), _ScriptedSsh.ALIVE, transport.MS_EXIT_FAILED),
            (RemoteTimeout("exit hung"), _ScriptedSsh.ALIVE, transport.MS_EXIT_FAILED),
            (_ScriptedSsh.OK, RemoteTimeout("check hung"), transport.MS_PROBE_TIMEOUT),
        ]
        for n, (exit_request, after, result) in enumerate(expected):
            with self.subTest(result=result):
                ssh = _ScriptedSsh(exit_request=exit_request, check_after_exit=after)
                tr = self.scripted_transport(ssh, name="w%d" % n)
                tr._start_master()
                self.assertEqual(tr._exit_master(), result)

    def test_stop_says_which_stage_was_interrupted(self):
        """REGRESSION: "teardown was interrupted" is three different facts."""
        cases = [
            # removal reply, exit reply, cleanup_ok, interrupted, stage, detail part
            (_ScriptedSsh.REMOVED, KeyboardInterrupt, True, False, "master-exit", None),
            (_ScriptedSsh.REMOVAL_FAILED, KeyboardInterrupt, False, False, "master-exit",
             "failed to remove router dir"),
            (KeyboardInterrupt, _ScriptedSsh.OK, False, True, "directory-removal",
             "INTERRUPTED"),
        ]
        for n, (removal, exit_request, ok, interrupted, stage, part) in enumerate(cases):
            with self.subTest(stage=stage, removal_ok=ok):
                ssh = _ScriptedSsh(removal=removal, exit_request=exit_request,
                                   check_after_exit=_ScriptedSsh.ALIVE)
                tr = self.scripted_transport(ssh, name="w%d" % n)
                session = self.live_session(tr)
                with self.assertRaises(KeyboardInterrupt) as cm:
                    tr.stop(session)
                facts = getattr(cm.exception, "teardown_facts", None)
                self.assertIsInstance(facts, dict, "the cancellation carries no stage facts")
                self.assertEqual(facts["remote_dir"], session.remote_dir)
                self.assertIs(facts["cleanup_ok"], ok)
                self.assertIs(facts["cleanup_interrupted"], interrupted)
                self.assertEqual(facts["interrupted_stage"], stage)
                if part is None:
                    self.assertIsNone(facts["cleanup_detail"])
                else:
                    self.assertIn(part, facts["cleanup_detail"])
                self.assertIsNone(tr._ctrl_dir, "the master's resources were not released")

    def test_confirmed_removal_then_interrupted_exit_queues_no_directory(self):
        """REGRESSION: through the Collector, for both follow-up observations.

        The removal was confirmed BEFORE the cancellation, so no directory may be
        named for reconciliation. Only the master can be uncertain - and with an
        absent socket nothing is. On `5059031` the run was not cancelled at all.
        """
        for label, after, result, unconfirmed in self.VARIANTS:
            with self.subTest(then=label):
                slug = label.replace(" ", "-")
                store = self.store("data-" + slug)
                ssh = _ScriptedSsh(exit_request=KeyboardInterrupt, check_after_exit=after)
                tr = self.scripted_transport(ssh, name="w-" + slug)
                c = Collector(tr, store, host_label="bench")
                c._session = self.live_session(tr)
                with self.assertRaises(KeyboardInterrupt):
                    c.close()
                self.assertIsNone(c.cleanup_error,
                                  "a confirmed removal was recorded as unconfirmed")
                self.assertFalse(c.cleanup_interrupted)
                disk = Store(store.dir)
                self.assertEqual(disk.pending_cleanup(), [],
                                 "a removed directory was queued for reconciliation")
                td = self.teardowns(disk)
                if unconfirmed:
                    self.assertEqual(len(td), 1)
                    self.assertEqual(td[0]["outcome"], "lifecycle_failure")
                    self.assertEqual(td[0]["master_shutdown"], result)
                    self.assertIn(result, td[0]["lifecycle_error"])
                    self.assertIsNone(td[0]["cleanup_detail"])
                    self.assertIs(td[0]["cleanup_interrupted"], False)
                else:
                    self.assertEqual(td, [], "nothing was unconfirmed, yet a failure "
                                             "was recorded")
                    self.assertIsNone(c.lifecycle_error)

    def test_failed_removal_then_interrupted_exit_keeps_the_removal_reason(self):
        """REGRESSION: the removal failed by itself; the Ctrl-C came afterwards.

        The record must carry the removal's own reason and must NOT call the
        removal interrupted.
        """
        store = self.store()
        ssh = _ScriptedSsh(removal=_ScriptedSsh.REMOVAL_FAILED,
                           exit_request=KeyboardInterrupt,
                           check_after_exit=_ScriptedSsh.ABSENT)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        session = c._session = self.live_session(tr)
        with self.assertRaises(KeyboardInterrupt):
            c.close()
        disk = Store(store.dir)
        td = self.teardowns(disk)
        self.assertEqual(len(td), 1)
        self.assertEqual(td[0]["outcome"], "cleanup_failure")
        self.assertEqual(td[0]["cleanup_detail"],
                         "failed to remove router dir %s (rc=1)" % session.remote_dir)
        self.assertIs(td[0]["cleanup_interrupted"], False)
        self.assertEqual(td[0]["master_shutdown"], transport.MS_GONE)
        self.assertEqual([p["remote_dir"] for p in disk.pending_cleanup()],
                         [session.remote_dir])

    def test_interrupted_removal_is_still_recorded_as_before(self):
        """GUARD: the directory-removal interruption (the earlier R2 fix) is intact."""
        store = self.store()
        ssh = _ScriptedSsh(removal=KeyboardInterrupt)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench")
        session = c._session = self.live_session(tr)
        with self.assertRaises(KeyboardInterrupt):
            c.close()
        self.assertIn("INTERRUPTED", c.cleanup_error or "")
        self.assertTrue(c.cleanup_interrupted)
        disk = Store(store.dir)
        td = self.teardowns(disk)
        self.assertEqual(len(td), 1)
        self.assertIs(td[0]["cleanup_interrupted"], True)
        self.assertEqual([p["remote_dir"] for p in disk.pending_cleanup()],
                         [session.remote_dir])

    def test_a_classified_attempt_is_recorded_before_the_cancellation_leaves(self):
        """REGRESSION: the reporter fails, and Ctrl-C lands on that attempt's teardown.

        The failure has already been classified; the cancellation is held for the
        one local write that records it, then re-raised. On `5059031` a cancellation
        during the master exit was swallowed (the run carried on), and one during
        the removal took the attempt's outcome with it.
        """
        reporter_failed = (1, b"", b"reporter boom\n")
        cases = [
            ("master exit", dict(exit_request=KeyboardInterrupt,
                                 check_after_exit=_ScriptedSsh.ALIVE), True, None),
            ("directory removal", dict(removal=KeyboardInterrupt), False, "INTERRUPTED"),
        ]
        for label, script, cleanup_ok, detail_part in cases:
            with self.subTest(interrupted=label):
                slug = label.replace(" ", "-")
                store = self.store("data-" + slug)
                ssh = _ScriptedSsh(after_mkdir=reporter_failed, **script)
                tr = self.scripted_transport(ssh, name="w-" + slug)
                c = Collector(tr, store, host_label="bench")
                session = c._session = self.live_session(tr)
                with self.assertRaises(KeyboardInterrupt):
                    c.collect_once()
                recorded = Store(store.dir).outcomes()
                self.assertEqual(len(recorded), 1, "the classified attempt was not recorded")
                self.assertEqual(recorded[0]["outcome"], OUTCOME_REPORTER_FAILURE)
                self.assertIn("reporter exited 1", recorded[0]["detail"])
                self.assertEqual(recorded[0]["remote_dir"], session.remote_dir)
                self.assertEqual(recorded[0]["remote_ownership"], "owned")
                self.assertIs(recorded[0]["cleanup_ok"], cleanup_ok)
                if detail_part:
                    self.assertIn(detail_part, recorded[0]["cleanup_detail"])
                else:
                    self.assertIsNone(recorded[0]["cleanup_detail"])
                c.close()                       # what the CLI does next
                disk = Store(store.dir)
                td = self.teardowns(disk)
                self.assertEqual(len(td), 1)
                if cleanup_ok:
                    self.assertEqual(td[0]["outcome"], "lifecycle_failure")
                    self.assertEqual(td[0]["master_shutdown"], MS_EXIT_INTERRUPTED)
                    self.assertEqual(disk.pending_cleanup(), [])
                else:
                    self.assertEqual(td[0]["outcome"], "cleanup_failure")
                    self.assertIs(td[0]["cleanup_interrupted"], True)
                    self.assertEqual([p["remote_dir"] for p in disk.pending_cleanup()],
                                     [session.remote_dir])

    def test_interrupted_exit_while_a_master_start_fails_releases_resources(self):
        """REGRESSION: the best-effort exit after a FAILED master start is cancelled.

        The cancellation wins over the failure being handled, and the local
        control-socket directory is still released. On `5059031` the cancellation
        was swallowed and the authentication failure was raised instead.
        """
        ssh = _ScriptedSsh(exit_request=KeyboardInterrupt)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        tr = harness.make_transport(work, "ok")
        made = []

        def _inner():
            tr._ctrl_dir = tempfile.mkdtemp(prefix="cm.", dir=work)
            tr._ctrl_path = os.path.join(tr._ctrl_dir, "cm")
            tr._master_up = True
            made.append(tr._ctrl_dir)
            raise TransportAuthError("ssh authentication was refused")

        tr._start_master_inner = _inner
        with mock.patch.object(transport, "run_bounded", ssh):
            with self.assertRaises(KeyboardInterrupt):
                tr._start_master()
        self.assertFalse(os.path.exists(made[0]), "the control-socket directory leaked")
        self.assertIsNone(tr._ctrl_path)

    # ---- the same through the CLI: a real SIGINT, a real ssh-shaped child ----
    def _cli_interrupted_exit(self, mode):
        data = os.path.join(self.tmp, "data")
        exit_log = os.path.join(self.tmp, "exit-requests.log")
        harness.write_ssh_config(
            data, self.tmp, mode=mode,
            extra_env={"FAKE_SSH_EXIT_LOG": exit_log,
                       "FAKE_SSH_MASTER_STATE": os.path.join(self.tmp, "master.gone")})
        proc = _spawn_collect(data)
        # The exit request is the LAST thing a run does: by the time the fake logs
        # it the sample is stored and the directory removal has been confirmed, so
        # the SIGINT below lands during the master exit and nowhere else.
        _wait_for(lambda: os.path.exists(exit_log), proc,
                  "the run never reached the master exit")
        out, err = _interrupt(self, proc)
        return data, err

    def test_cli_sigint_during_master_exit_with_an_answering_master(self):
        """REGRESSION: exit 130, and the master's uncertainty is durable.

        On `5059031` the SIGINT was swallowed and the run ended 5 (cleanup failure).
        """
        data, err = self._cli_interrupted_exit("exit_hangs_undelivered")
        store = Store(data)
        attempts = [r for r in store.outcomes() if r.get("record") != "teardown"]
        self.assertEqual([a["outcome"] for a in attempts], [OUTCOME_SUCCESS], err)
        self.assertIs(attempts[0]["cleanup_ok"], True)
        td = self.teardowns(store)
        self.assertEqual(len(td), 1, err)
        self.assertEqual(td[0]["outcome"], "lifecycle_failure")
        self.assertEqual(td[0]["master_shutdown"], MS_EXIT_INTERRUPTED)
        self.assertIsNone(td[0]["cleanup_detail"])
        self.assertIs(td[0]["cleanup_interrupted"], False)
        self.assertEqual(store.pending_cleanup(), [],
                         "a confirmed removal was queued for reconciliation")
        self.assertIn("master shutdown: SEPARATE FAILURE", err)
        self.assertNotEqual(_mock_run(data).returncode, cli.EXIT_LOCK_HELD,
                            "the data-dir lock leaked after an interrupted master exit")

    def test_cli_sigint_during_master_exit_with_the_socket_then_absent(self):
        """REGRESSION: exit 130 with nothing left unconfirmed.

        On `5059031` the SIGINT was swallowed and the run ended 0.
        """
        data, err = self._cli_interrupted_exit("exit_hangs_delivered")
        store = Store(data)
        attempts = [r for r in store.outcomes() if r.get("record") != "teardown"]
        self.assertEqual([a["outcome"] for a in attempts], [OUTCOME_SUCCESS], err)
        self.assertEqual(self.teardowns(store), [], err)
        self.assertEqual(store.pending_cleanup(), [])
        self.assertIn("ssh control master exited (shutdown verified)", err)
        self.assertNotIn("SEPARATE FAILURE", err)
        self.assertNotEqual(_mock_run(data).returncode, cli.EXIT_LOCK_HELD,
                            "the data-dir lock leaked after an interrupted master exit")


# ------------------------------------------------------------------ CLI helpers
def _spawn_collect(data):
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "collector.py"),
         "collect", "--once", "--data-dir", data],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _wait_for(ready, proc, why, limit=40):
    deadline = time.time() + limit
    while time.time() < deadline:
        if ready():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    out, err = "", ""
    if proc.poll() is None:
        proc.kill()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    raise AssertionError("%s (exit %s):\n%s" % (why, proc.returncode, err))


def _interrupt(case, proc):
    """SIGINT the collector and require the INTERRUPTED exit status."""
    time.sleep(0.5)                      # let the collector block on the hung child
    proc.send_signal(signal.SIGINT)
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        case.fail("the collector did not stop after SIGINT:\n" + err)
    case.assertEqual(proc.returncode, cli.EXIT_INTERRUPTED,
                     "the cancellation did not reach the exit status:\n" + err)
    return out, err


def _mock_run(data):
    """A following run that needs the data-dir lock (no ssh at all)."""
    return harness.run_cli(["collect", "--once", "--transport", "mock", "--data-dir", data])


if __name__ == "__main__":
    unittest.main()
