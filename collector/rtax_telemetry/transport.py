"""Bounded SSH transport.

This module deliberately RE-USES the semantics of the reviewed, hardware-proven
launchers in ``the original private bench project`` (``run_reporter_test.sh``,
``compare_router.sh``), rather than inventing a new transport:

  * the pinned ``known_hosts`` file and its expected SHA-256
  * the reporter artifact and its expected SHA-256
  * the ssh option set (password auth only, a single prompt, connect timeout,
    server-alive keepalives)
  * a router-side OpenSSL SHA-256 known-answer test and a required-tool gate
  * an exclusive ``mkdir -m 700`` remote temp dir with ownership tracking
  * a watchdog SELF-TEST on the target, proving the bound works before it is used
  * a SHA-256 gate on the transferred artifact BEFORE it is ever executed
  * a remote watchdog that bounds the reporter run
  * a scoped cleanup that removes a remote directory ONLY if this run created it,
    and whose failure is surfaced rather than swallowed
  * a single ControlMaster connection (one password prompt) reused for every
    command, then torn down; its resources are released even when the connection
    is never established

Host-side execution notes (each is a correctness requirement, not a nicety):

  * ``process_group=0`` puts every child in its OWN process group while keeping
    it in THIS session, so ``/dev/tty`` still resolves — the ssh password prompt
    keeps working — and a timeout can kill the whole group. ``start_new_session``
    (setsid) is deliberately avoided: it detaches the controlling terminal and
    breaks the prompt.
  * A ``KeyboardInterrupt`` during a bounded call terminates and reaps the child
    group before propagating, so Ctrl-C never leaks an ssh process.
  * The remote watchdog subshell has its descriptors detached (``>/dev/null``) and
    records its ``sleep`` pid, so it neither holds the ssh channel open after a
    fast reporter finishes nor survives cancellation.

Nothing here writes to NVRAM, flash, packages or the network configuration, and
the remote directory lives under /tmp and is removed on exit.
"""

import hashlib
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

try:                       # POSIX terminal control; absent on non-POSIX hosts
    import fcntl
    import termios
except ImportError:        # pragma: no cover
    fcntl = None
    termios = None

from .errors import (
    ArtifactMismatch,
    CleanupError,
    OutputLimitExceeded,
    RemoteCommandError,
    RemoteTimeout,
    TransportAuthError,
    TransportError,
    TransportHostKeyError,
    TransportUnreachableError,
)

DEFAULT_ARTIFACT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "out", "router_health_arm"))
DEFAULT_KNOWN_HOSTS = os.path.expanduser("~/.config/asus-router-telemetry/known_hosts")

# Each operator supplies hashes for their own build and pinned host-key file.
EXPECT_ARTIFACT_SHA = None
EXPECT_KNOWN_HOSTS_SHA = None
OPENSSL_KAT_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

# Statuses >= 128 mean the job was killed by a signal (128 + signo).
_KILLED_FROM = 128
# The watchdog terminates its target with SIGKILL (kill -9) -> 128 + 9.
_WD_SIGKILL_STATUS = 137
# The self-test must land within this window around the configured bound, and must
# show POSITIVE evidence the timer fired. A bare "status >= 128" (which would also
# accept a crash such as SIGSEGV 139) is deliberately NOT accepted.
_SELFTEST_LOWER_TOL = 0.5      # seconds below the bound that still count
_SELFTEST_UPPER_TOL = 3.0      # seconds above the bound that still count

# Byte ceilings for device output. Enforced WHILE RECEIVING (before any full
# buffering), so an authenticated-but-hostile endpoint cannot exhaust host memory.
DEFAULT_MAX_STDOUT = 1024 * 1024        # 1 MiB
DEFAULT_MAX_STDERR = 256 * 1024         # 256 KiB
_READ_CHUNK = 65536

# --- remote command strings (kept identical in spirit to the proven scripts) ---
TOOLS_CMD = (
    'miss=""; for t in dd mkdir chmod rm cp cat kill sleep date grep df; do '
    '{ [ -x /bin/$t ] || [ -x /usr/bin/$t ] || [ -x /usr/sbin/$t ] || [ -x /sbin/$t ]; } '
    '|| miss="$miss $t"; done; [ -x /usr/sbin/openssl ] || miss="$miss openssl"; '
    'if [ -z "$miss" ]; then printf "TOOLS_OK\n"; '
    'else printf "TOOLS_MISSING:%s\n" "$miss"; fi'
)
KAT_CMD = "printf abc | /usr/sbin/openssl dgst -sha256"

CLEAN_CMD = 'rm -rf "{rd}"; if [ -e "{rd}" ]; then echo REMOVAL_FAILED 1>&2; exit 1; else echo REMOVED; fi'


# ---------------------------------------------------------------------------
# Remote watchdog: a detached subshell that sleeps for the bound then kills the
# target. Its descriptors go to /dev/null so it can NEVER hold the ssh channel
# open, and it records the sleep's pid in <pid_file> so the whole watchdog can be
# terminated and reaped when the target finishes early.
# ---------------------------------------------------------------------------
def _cancel(pid_file):
    """Kill the reporter, the watchdog subshell and the watchdog's sleep, and remove
    the pid file.

    Used BOTH as the normal cancellation path and inside the remote shell's signal
    trap, so a HUP/INT/TERM on the remote shell (for example the ssh channel being
    closed or the local watchdog killing ssh) cannot leave children running on the
    target. `${p:-}` / `${w:-}` keep it safe when the variables are not set yet.
    """
    return ('kill -9 ${p:-} ${w:-} 2>/dev/null; '
            'if [ -f "%s" ]; then kill -9 "$(cat "%s")" 2>/dev/null; rm -f "%s"; fi'
            % (pid_file, pid_file, pid_file))


def _trap_line(pid_file):
    return "trap '%s; exit 129' HUP INT TERM\n" % _cancel(pid_file)


def _wd_start(bound, target_var, pid_file, fired_file=None):
    """Start the detached watchdog.

    When ``fired_file`` is given, the watchdog creates it IMMEDIATELY BEFORE it
    kills the target, so its existence is positive evidence that the TIMER (not
    some unrelated crash) terminated the job. The self-test relies on that marker;
    the normal reporter run does not need it and passes ``None``.
    """
    fire = (': > "%s"\n' % fired_file) if fired_file else ""
    tpl = (
        "(\n"
        "  sleep %d >/dev/null 2>&1 &\n"
        '  echo $! > "%s"\n'
        "  wait\n"
        + fire +
        '  kill -9 "$%s" 2>/dev/null\n'
        ") >/dev/null 2>&1 &\n"
        "w=$!\n"
    )
    return tpl % (bound, pid_file, target_var)


def run_cmd(rd, bound):
    """The remote reporter run: stdout is JSON only, diagnostics on stderr, the
    reporter's own exit status preserved through ssh (``exit $ec``), and a signal
    trap that cleans up the reporter and the watchdog."""
    pid_file = "%s/wd.pid" % rd
    return (
        "p=; w=\n"
        + _trap_line(pid_file)
        + '"%s/router_health" > "%s/out.json" 2> "%s/err.txt" &\n' % (rd, rd, rd)
        + "p=$!\n"
        + _wd_start(bound, "p", pid_file)
        + 'ec=0\nwait "$p" || ec=$?\n'
        + _cancel(pid_file) + "\n"
        + 'cat "%s/out.json" 2>/dev/null\n' % rd
        + "printf 'router_health_exit=%s\\n' \"$ec\" 1>&2\n"
        + 'cat "%s/err.txt" 1>&2\n' % rd
        + "exit $ec\n"
    )


def selftest_cmd(rd, bound):
    """The watchdog self-test: a long job that MUST be killed at the bound, with the
    same signal trap as the real run.

    It reports BOTH the job's exit status and whether the watchdog timer actually
    fired (a marker file the watchdog writes just before it kills). A watchdog that
    does not bound, a crash that happens to exit >= 128, or a timer that never fired
    are all distinguishable from a genuine bounded kill.
    """
    pid_file = "%s/wd.pid" % rd
    fired_file = "%s/wd.fired" % rd
    return (
        "p=; w=\n"
        + _trap_line(pid_file)
        + 'rm -f "%s"\n' % fired_file
        + "/bin/dd if=/dev/zero of=/dev/null bs=4096 count=100000000 2>/dev/null &\n"
        + "p=$!\n"
        + _wd_start(bound, "p", pid_file, fired_file)
        + 'ec=0\nwait "$p" || ec=$?\n'
        + _cancel(pid_file) + "\n"
        + "printf 'selftest_exit=%s\\n' \"$ec\"\n"
        + 'if [ -f "%s" ]; then printf "selftest_wd_fired=1\\n"; '
          'else printf "selftest_wd_fired=0\\n"; fi\n' % fired_file
    )


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- host side
REAP_GRACE = 3.0        # seconds between SIGTERM and SIGKILL on the child group
                        # (module global so tests can shorten it)

if sys.version_info >= (3, 11):
    _OWN_GROUP = {"process_group": 0}          # own process group, same session
else:                                          # pragma: no cover - old interpreters
    _OWN_GROUP = {"preexec_fn": os.setpgrp}


def has_controlling_terminal():
    """True when /dev/tty can be opened, i.e. an interactive terminal is attached."""
    if termios is None:
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


class _TerminalHandover:
    """Lend the controlling terminal's FOREGROUND to a child's process group.

    A child placed in its own process group is a *background* group of the terminal,
    so any read from /dev/tty stops it with SIGTTIN — which is exactly how an ssh
    password prompt would stall silently instead of reading the password. Making the
    child's group the foreground group lets the prompt read normally. The previous
    foreground group and the terminal modes are restored on success, on timeout and
    on interruption (the restore runs from a ``finally``).

    Nothing is read from the terminal here: the password goes straight from the
    operator's keyboard into ssh and is never captured or logged.
    """

    def __init__(self, pgid, log=None):
        self.pgid = pgid
        self.log = log or (lambda *a: None)
        self.fd = None
        self.old_pgrp = None
        self.old_term = None
        self.old_ttou = None
        self.active = False

    def __enter__(self):
        if termios is None:
            return self
        try:
            self.fd = os.open("/dev/tty", os.O_RDWR)
        except OSError:
            return self
        try:
            # tcsetpgrp from a background group raises SIGTTOU; ignore it while we work
            if threading.current_thread() is threading.main_thread():
                self.old_ttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            self.old_pgrp = os.tcgetpgrp(self.fd)
            self.old_term = termios.tcgetattr(self.fd)
            os.tcsetpgrp(self.fd, self.pgid)
            self.active = True
        except OSError as e:
            self.log("could not hand the terminal to the child (%s)" % e)
        return self

    def __exit__(self, *_exc):
        try:
            if self.fd is not None:
                if self.old_pgrp is not None:
                    try:
                        os.tcsetpgrp(self.fd, self.old_pgrp)
                    except OSError:
                        pass
                if self.old_term is not None:
                    try:
                        # TCSANOW: restore immediately. TCSADRAIN can block if the
                        # terminal's output is not being consumed (e.g. a PTY whose
                        # master is not being read), which would hang teardown.
                        termios.tcsetattr(self.fd, termios.TCSANOW, self.old_term)
                    except (OSError, termios.error):
                        pass
                os.close(self.fd)
                self.fd = None
        finally:
            if self.old_ttou is not None:
                try:
                    signal.signal(signal.SIGTTOU, self.old_ttou)
                except (ValueError, OSError):          # not the main thread any more
                    pass
        return False


def _group_alive(pgid):
    """True while ANY process remains in the group (not just the leader)."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(pgid, sig, p=None):
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        if p is not None:
            try:
                p.send_signal(sig)
            except Exception:                      # noqa: BLE001
                pass
        return True


def _terminate_and_reap(p, grace=None):
    """Terminate and REAP the whole child GROUP; release its streams.

    The GROUP is signalled unconditionally — even when the leader has already
    exited and been reaped. A descendant that inherited the group can still be
    alive holding the output pipes, and the leader's exit says nothing about it.
    """
    if grace is None:
        grace = REAP_GRACE
    pgid = p.pid
    try:
        p.poll()                                   # reap the leader if it has exited
        _signal_group(pgid, signal.SIGTERM, p)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            p.poll()                               # keep reaping so a dead leader
            if not _group_alive(pgid):             # does not keep the group "alive"
                break
            time.sleep(0.05)
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL, p)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and _group_alive(pgid):
                p.poll()
                time.sleep(0.05)
    finally:
        for stream in (getattr(p, "stdin", None), getattr(p, "stdout", None),
                       getattr(p, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except Exception:                  # noqa: BLE001
                    pass
    try:
        p.wait(timeout=5)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _close_registered(sel, stream):
    try:
        sel.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except Exception:                      # noqa: BLE001
        pass


def _drain_bounded(p, timeout, stdin_bytes, max_stdout, max_stderr):
    """Drain stdout/stderr incrementally, enforcing byte ceilings WHILE RECEIVING.

    Nothing is fully buffered before the limits are checked: every read is counted
    and an over-limit stream raises OutputLimitExceeded immediately, so an
    authenticated-but-hostile endpoint cannot make the host allocate without bound.

    stdin (the artifact) is written CONCURRENTLY *and NON-BLOCKING*. Selector
    readiness only means SOME bytes can be written — never that a whole chunk
    fits — so a plain blocking ``os.write`` could stall past the deadline while a
    slow (or partial) reader keeps the pipe full. The write end is therefore
    switched to non-blocking: a partial write advances ``pos``, EAGAIN
    (``BlockingIOError``) simply leaves stdin registered so the outputs keep being
    drained and the deadline keeps being checked, and a broken pipe (the child
    closed its stdin) is treated as "input finished". An empty input is closed off
    immediately so the child still sees EOF.
    """
    sel = selectors.DefaultSelector()
    out = bytearray()
    err = bytearray()
    view = memoryview(stdin_bytes) if stdin_bytes is not None else None
    pos = 0
    deadline = time.monotonic() + timeout
    try:
        if p.stdout is not None:
            sel.register(p.stdout, selectors.EVENT_READ, "out")
        if p.stderr is not None:
            sel.register(p.stderr, selectors.EVENT_READ, "err")
        if view is not None and p.stdin is not None:
            # Non-blocking write end: readiness is not a promise the chunk fits.
            os.set_blocking(p.stdin.fileno(), False)
            if len(view) == 0:
                _close_registered(sel, p.stdin)        # empty input -> immediate EOF
            else:
                sel.register(p.stdin, selectors.EVENT_WRITE, "in")
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(p.args, timeout)
            for key, _mask in sel.select(timeout=min(remaining, 0.5)):
                stream = key.fileobj
                if key.data == "in":
                    if view is None:               # not registered without stdin_bytes
                        _close_registered(sel, stream)
                        continue
                    try:
                        n = os.write(stream.fileno(), view[pos:pos + _READ_CHUNK])
                    except BlockingIOError:
                        # pipe buffer full: keep it registered and loop; stdout/
                        # stderr are still drained and the deadline is still checked
                        continue
                    except BrokenPipeError:
                        # the child closed its stdin: input is finished
                        n = len(view) - pos
                    pos += n
                    if pos >= len(view):
                        _close_registered(sel, stream)
                    continue
                chunk = os.read(stream.fileno(), _READ_CHUNK)
                if not chunk:
                    _close_registered(sel, stream)
                    continue
                if key.data == "out":
                    out += chunk
                    if len(out) > max_stdout:
                        raise OutputLimitExceeded(
                            "device stdout exceeded the %d-byte limit" % max_stdout)
                else:
                    err += chunk
                    if len(err) > max_stderr:
                        raise OutputLimitExceeded(
                            "device stderr exceeded the %d-byte limit" % max_stderr)
    finally:
        sel.close()
    # Every pipe is closed, so the child is (or is about to be) finished; bound the
    # final wait by whatever remains of the deadline.
    remaining = deadline - time.monotonic()
    p.wait(timeout=max(0.1, remaining))
    return bytes(out), bytes(err)


def run_bounded(argv, timeout, stdin_bytes=None, capture=True, interactive=False,
                log=None, max_stdout=DEFAULT_MAX_STDOUT, max_stderr=DEFAULT_MAX_STDERR):
    """Run argv in its own process group; kill and reap the GROUP on timeout,
    interruption OR an output-limit breach. Returns (rc, stdout_bytes, stderr_bytes).

    ``interactive=True`` lends the controlling terminal's foreground to the child's
    group for the duration, so a child that reads /dev/tty (an ssh password prompt)
    is not stopped by SIGTTIN. stdin is /dev/null, so no secret can travel through
    our pipes; the password is typed directly into the terminal by the operator and
    is never captured or logged.

    Output is drained incrementally under ``max_stdout`` / ``max_stderr`` byte
    ceilings; exceeding either terminates and reaps the child group and raises
    OutputLimitExceeded.
    """
    kwargs = dict(_OWN_GROUP)
    if capture:
        kwargs.update(
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        kwargs.update(stdin=subprocess.DEVNULL)
    p = subprocess.Popen(argv, **kwargs)
    handover = None
    out = b""
    err = b""
    try:
        if interactive:
            handover = _TerminalHandover(p.pid, log)
            handover.__enter__()
        if capture:
            out, err = _drain_bounded(p, timeout, stdin_bytes, max_stdout, max_stderr)
        else:
            p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_and_reap(p)
        raise RemoteTimeout("command exceeded %ss and was killed: %s" % (timeout, argv[0]))
    except BaseException:
        # KeyboardInterrupt, an output-limit breach, or any other exception:
        # never leak the child group.
        _terminate_and_reap(p)
        raise
    finally:
        if handover is not None:
            handover.__exit__(None, None, None)
    return p.returncode, (out or b""), (err or b"")


def classify_ssh_failure(stderr_text):
    """Map ssh stderr to the right exception type."""
    t = (stderr_text or "").lower()
    if "host key verification failed" in t or "remote host identification has changed" in t \
            or "no matching host key" in t or ("host key" in t and "changed" in t):
        return TransportHostKeyError("pinned host key did not match: %s" % stderr_text.strip())
    if "permission denied" in t or "authentication failed" in t or "too many authentication" in t:
        return TransportAuthError("ssh authentication was refused: %s" % stderr_text.strip())
    if any(s in t for s in ("connection refused", "no route to host", "operation timed out",
                            "connection timed out", "network is unreachable",
                            "could not resolve hostname", "connection reset",
                            "connection closed by", "host is down", "no address associated")):
        return TransportUnreachableError(
            "management address unreachable: %s" % stderr_text.strip())
    return TransportError("ssh failed: %s" % (stderr_text.strip() or "unknown error"))


def _oneline(raw):
    """A bounded single-line rendering of captured bytes, for error messages."""
    text = (raw or b"").decode("utf-8", "replace")
    return " ".join(text.split())[:200]


class SSHSession:
    """Opaque handle for one provisioned collection session."""

    def __init__(self, remote_dir, artifact_sha):
        self.remote_dir = remote_dir
        self.artifact_sha = artifact_sha
        self.provisioned = False
        self.owned = False      # True only after WE created remote_dir


class SSHTransport:
    """A single-master, bounded, verified ssh transport to the router."""

    name = "ssh"

    def __init__(self, host, user, *,
                 artifact=DEFAULT_ARTIFACT,
                 known_hosts=DEFAULT_KNOWN_HOSTS,
                 artifact_sha=EXPECT_ARTIFACT_SHA,
                 known_hosts_sha=EXPECT_KNOWN_HOSTS_SHA,
                 ssh_bin="ssh",
                 connect_timeout=8,
                 run_bound=15,
                 remote_grace=10,
                 control_persist=900,
                 watchdog_selftest=True,
                 watchdog_selftest_bound=3,
                 verify_artifact=True,
                 interactive_auth=None,
                 max_stdout_bytes=DEFAULT_MAX_STDOUT,
                 max_stderr_bytes=DEFAULT_MAX_STDERR,
                 log=None):
        self.host = host
        self.user = user
        self.artifact = os.path.expanduser(artifact)
        self.known_hosts = os.path.expanduser(known_hosts)
        self.artifact_sha = artifact_sha
        self.known_hosts_sha = known_hosts_sha
        self.ssh_bin = ssh_bin
        self.connect_timeout = connect_timeout
        self.run_bound = run_bound
        self.remote_grace = remote_grace
        self.control_persist = control_persist
        self.watchdog_selftest_enabled = watchdog_selftest
        self.watchdog_selftest_bound = watchdog_selftest_bound
        self.verify_artifact = verify_artifact
        self.interactive_auth = interactive_auth   # None -> auto-detect a controlling tty
        self.max_stdout_bytes = int(max_stdout_bytes)
        self.max_stderr_bytes = int(max_stderr_bytes)
        self.log = log or (lambda *a: None)

        self._ctrl_dir = None
        self._ctrl_path = None
        self._master_up = False

    # ------------------------------------------------------------- options
    def _ssh_opts(self):
        return [
            "-o", "UserKnownHostsFile=%s" % self.known_hosts,
            "-o", "StrictHostKeyChecking=yes",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=%d" % self.connect_timeout,
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
        ]

    def _target(self):
        return "%s@%s" % (self.user, self.host)

    # ------------------------------------------------------------ preflight
    def preflight(self):
        """Verify the local artifact and pinned known_hosts. No network."""
        if not os.path.isfile(self.known_hosts):
            raise ArtifactMismatch("pinned known_hosts not found: %s" % self.known_hosts)
        k = sha256_file(self.known_hosts)
        if self.known_hosts_sha and k != self.known_hosts_sha:
            raise ArtifactMismatch(
                "pinned known_hosts hash mismatch (%s != %s) - refusing"
                % (k, self.known_hosts_sha))
        if not os.path.isfile(self.artifact):
            raise ArtifactMismatch("artifact not found: %s" % self.artifact)
        if self.verify_artifact:
            if not self.artifact_sha:
                raise ArtifactMismatch("configure artifact_sha for your reviewed ARM build before collection")
            a = sha256_file(self.artifact)
            if self.artifact_sha and a != self.artifact_sha:
                raise ArtifactMismatch(
                    "artifact hash mismatch (%s != %s) - refusing to transfer"
                    % (a, self.artifact_sha))
        return True

    # --------------------------------------------------------------- master
    def _release_master_resources(self):
        self._master_up = False
        if self._ctrl_dir and os.path.isdir(self._ctrl_dir):
            shutil.rmtree(self._ctrl_dir, ignore_errors=True)
        self._ctrl_dir = None
        self._ctrl_path = None

    def _start_master(self):
        self._ctrl_dir = tempfile.mkdtemp(prefix="rt-ax56u-cm.")
        os.chmod(self._ctrl_dir, 0o700)
        self._ctrl_path = os.path.join(self._ctrl_dir, "cm")
        argv = [self.ssh_bin] + self._ssh_opts() + [
            "-o", "ControlMaster=yes",
            "-o", "ControlPath=%s" % self._ctrl_path,
            "-o", "ControlPersist=%d" % self.control_persist,
            "-f", "-N", self._target(),
        ]
        # stdout/stderr are captured so a failure can be classified. ssh reads the
        # one password prompt from /dev/tty: when an interactive terminal is attached
        # the child's group is lent the terminal's foreground for the duration (so
        # the read is not stopped by SIGTTIN), and the previous foreground group and
        # terminal modes are restored afterwards. Without a controlling terminal no
        # interactive prompt is possible at all. A failure OR an interruption must
        # not leave the control-socket directory behind.
        has_tty = has_controlling_terminal()
        interactive = has_tty if self.interactive_auth is None else bool(self.interactive_auth)
        if self.interactive_auth is None and not has_tty:
            self.log("no controlling terminal: interactive password authentication is "
                     "unavailable (a key in authorized_keys is required for unattended use)")
        try:
            rc, _out, err = run_bounded(argv, self.connect_timeout + 20,
                                        interactive=interactive, log=self.log,
                                        max_stdout=self.max_stdout_bytes,
                                        max_stderr=self.max_stderr_bytes)
        except BaseException:
            self._release_master_resources()
            raise
        if rc != 0:
            msg = err.decode("utf-8", "replace")
            self._release_master_resources()
            raise classify_ssh_failure(msg)
        self._master_up = True
        self.log("ssh master established to %s" % self._target())

    def _rsh(self, cmd, timeout, stdin_bytes=None):
        argv = [self.ssh_bin] + self._ssh_opts()
        if stdin_bytes is None:
            argv += ["-n"]
        argv += ["-o", "ControlPath=%s" % self._ctrl_path, self._target(), cmd]
        rc, out, err = run_bounded(argv, timeout, stdin_bytes=stdin_bytes,
                                   max_stdout=self.max_stdout_bytes,
                                   max_stderr=self.max_stderr_bytes)
        if rc == 255:
            raise classify_ssh_failure(err.decode("utf-8", "replace"))
        return rc, out, err

    # ------------------------------------------------------------- session
    def start(self):
        self.preflight()
        self._start_master()
        remote_dir = "/tmp/rt." + secrets.token_hex(4)
        session = SSHSession(remote_dir, self.artifact_sha)
        try:
            self._provision(session)
        except BaseException as e:
            try:
                self._dispose(session)
            except Exception as ce:                          # noqa: BLE001
                self.log("cleanup after failed provision did not succeed: %s" % ce)
            finally:
                self.close()
            raise e
        return session

    def _provision(self, session):
        rd = session.remote_dir
        self.log("router temp dir: %s" % rd)

        # 1. OpenSSL SHA-256 known-answer test (the router has no sha256sum/od)
        rc, out, err = self._rsh(KAT_CMD, self.connect_timeout + 10)
        kat = out.decode("utf-8", "replace").strip().split()[-1] if out.strip() else ""
        if rc != 0 or kat != OPENSSL_KAT_ABC:
            raise TransportError("openssl SHA-256 known-answer test FAILED (got %r)" % kat)

        # 2. required-tool hard gate: require rc==0 AND exactly one well-formed
        #    marker line. A nonzero exit, a missing marker, several markers or an
        #    unrecognised marker all stop provisioning BEFORE any remote state is
        #    created or anything is executed.
        rc, out, err = self._rsh(TOOLS_CMD, self.connect_timeout + 10)
        if rc != 0:
            raise TransportError(
                "required-tool probe failed (rc=%d): %s - hard stop" % (rc, _oneline(err)))
        markers = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines()
                   if ln.strip()]
        if len(markers) != 1:
            raise TransportError(
                "required-tool probe returned %d marker line(s), expected exactly 1 "
                "- ambiguous, hard stop" % len(markers))
        marker = markers[0]
        if marker == "TOOLS_OK":
            pass
        elif marker.startswith("TOOLS_MISSING:"):
            miss = marker[len("TOOLS_MISSING:"):].strip()
            if not miss:
                raise TransportError(
                    "required-tool probe reported an empty MISSING list - malformed, hard stop")
            raise TransportError("required router tools MISSING: %s - hard stop" % miss)
        else:
            raise TransportError(
                "required-tool probe returned an unrecognised result %r - hard stop" % marker)

        # 3. exclusive private directory (ownership tracking)
        rc, _out, _err = self._rsh('mkdir -m 700 "%s" && [ -d "%s" ]' % (rd, rd),
                                   self.connect_timeout + 10)
        if rc != 0:
            raise TransportError("exclusive mkdir failed at %s - this run created nothing" % rd)
        session.owned = True

        # 4. PROVE the remote watchdog bounds a long job before it is trusted
        if self.watchdog_selftest_enabled:
            self._watchdog_selftest(session)

        # 5. transfer + router-side SHA-256 gate (before any execution)
        with open(self.artifact, "rb") as fh:
            blob = fh.read()
        xfer = ('cat > "%s/router_health" && chmod 700 "%s/router_health" && '
                '/usr/sbin/openssl dgst -sha256 "%s/router_health"' % (rd, rd, rd))
        rc, out, err = self._rsh(xfer, self.connect_timeout + 30, stdin_bytes=blob)
        rsha = out.decode("utf-8", "replace").strip().split()[-1] if out.strip() else ""
        if rc != 0:
            raise RemoteCommandError(rc, "transfer command failed (rc=%d)" % rc)
        if self.artifact_sha and rsha != self.artifact_sha:
            raise ArtifactMismatch("router-side SHA-256 mismatch (%s) - refusing to execute" % rsha)

        # 6. control: prove /tmp can execute at all
        ctl = ('cp /bin/busybox "%s/busybox" && chmod 700 "%s/busybox" && '
               '"%s/busybox" echo control-exec-ok; ec=$?; rm -f "%s/busybox"; exit $ec'
               % (rd, rd, rd, rd))
        rc, out, err = self._rsh(ctl, self.connect_timeout + 10)
        if rc != 0:
            raise RemoteCommandError(rc, "control exec from /tmp failed (rc=%d)" % rc)

        session.provisioned = True
        self.log("artifact verified on the router (sha256 %s)" % (rsha[:16] + "..." if rsha else "?"))

    def _watchdog_selftest(self, session):
        """Run a long job under the SAME watchdog shape used for the reporter and
        require that the watchdog TIMER killed it at the bound.

        Three independent things are required, and none is sufficient alone: the
        intended SIGKILL status (137), positive evidence the watchdog timer fired
        (a marker the watchdog writes immediately before it kills), and an elapsed
        time within tolerance of the bound. A bare status >= 128 would also accept
        an unrelated crash (e.g. SIGSEGV 139), and a printed success line proves
        nothing about the timer.
        """
        bound = self.watchdog_selftest_bound
        cmd = selftest_cmd(session.remote_dir, bound)
        started = time.monotonic()
        rc, out, err = self._rsh(cmd, bound + self.connect_timeout + 10)
        elapsed = time.monotonic() - started
        if rc != 0:
            raise TransportError(
                "watchdog self-test did not complete (rc=%d): %s" % (rc, _oneline(err)))
        text = out.decode("utf-8", "replace")
        exits = re.findall(r"selftest_exit=(\d+)", text)
        fired = re.findall(r"selftest_wd_fired=([01])", text)
        if len(exits) != 1 or len(fired) != 1:
            raise TransportError(
                "watchdog self-test result is missing or ambiguous "
                "(exit markers=%d, fired markers=%d)" % (len(exits), len(fired)))
        st = int(exits[0])
        fired_flag = int(fired[0])
        if st == 0:
            raise TransportError(
                "watchdog self-test FAILED: the long job completed instead of being "
                "killed at %ss - the remote bound is NOT proven - refusing to run" % bound)
        if st != _WD_SIGKILL_STATUS:
            raise TransportError(
                "watchdog self-test FAILED: unexpected termination status %d "
                "(expected %d from the watchdog's SIGKILL) - refusing to run"
                % (st, _WD_SIGKILL_STATUS))
        if fired_flag != 1:
            raise TransportError(
                "watchdog self-test FAILED: no evidence the watchdog timer fired "
                "(the job died from something other than the bound) - refusing to run")
        lower = max(0.0, bound - _SELFTEST_LOWER_TOL)
        upper = bound + _SELFTEST_UPPER_TOL
        if not (lower <= elapsed <= upper):
            raise TransportError(
                "watchdog self-test FAILED: the job ended after %.2fs, outside the "
                "expected %ss bound (tolerance %.1f-%.1fs) - refusing to run"
                % (elapsed, bound, lower, upper))
        self.log("watchdog self-test OK: long job killed at %ss "
                 "(status %d, timer fired, %.2fs)" % (bound, st, elapsed))

    def collect_sample(self, session, timeout=None):
        """Run the reporter once under the remote watchdog. Returns raw JSON bytes."""
        cmd = run_cmd(session.remote_dir, self.run_bound)
        bound = timeout if timeout is not None else self.run_bound + self.remote_grace
        rc, out, err = self._rsh(cmd, bound)
        if rc in (129, 130, 137, 143):
            raise RemoteTimeout("reporter run was killed by the watchdog (rc=%d)" % rc)
        if rc != 0:
            diag = err.decode("utf-8", "replace").strip().replace("\n", " ")
            raise RemoteCommandError(rc, "reporter exited %d: %s" % (rc, diag[:400]))
        return out

    # ------------------------------------------------------------- cleanup
    def _dispose(self, session):
        """Remove the remote dir THIS run created. Raises CleanupError on failure.

        A directory this run did NOT create is never touched (returns quietly).
        """
        if not session.owned:
            return
        if not self._master_up:
            raise CleanupError(
                "ssh master is down; remote dir %s may remain on the router" % session.remote_dir)
        cmd = CLEAN_CMD.format(rd=session.remote_dir)
        try:
            rc, _out, err = self._rsh(cmd, self.connect_timeout + 10)
        except TransportError as e:
            raise CleanupError("failed to remove router dir %s: %s" % (session.remote_dir, e))
        if rc != 0:
            raise CleanupError("failed to remove router dir %s (rc=%d)" % (session.remote_dir, rc))

    def stop(self, session):
        """Tear the session down. A cleanup failure is raised AFTER the master is
        closed, so teardown always completes."""
        err = None
        try:
            self._dispose(session)
        except CleanupError as e:
            err = e
        finally:
            self.close()
        if err is not None:
            raise err

    def close(self):
        try:
            if self._master_up and self._ctrl_path:
                try:
                    run_bounded([self.ssh_bin, "-o", "ControlPath=%s" % self._ctrl_path,
                                 "-O", "exit", self._target()], 15)
                except BaseException:                       # noqa: BLE001 - best effort
                    pass
                self._master_up = False
        finally:
            # always release the local control-socket directory, even if the
            # master shutdown was interrupted
            if self._ctrl_dir and os.path.isdir(self._ctrl_dir):
                shutil.rmtree(self._ctrl_dir, ignore_errors=True)
            self._ctrl_dir = None
            self._ctrl_path = None
