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
  * a liveness GATE before every multiplexed command (`ssh -O check`), so a lost
    master becomes an explicit classified error instead of a silent fallback to a
    fresh direct connection that would need interactive authentication
  * master shutdown that is VERIFIED (`-O check` after `-O exit`), never inferred
    from the absence of an error
  * a hermetic ssh option set (`-F /dev/null`, no agent/forwarding/proxy) so the
    connection cannot be altered by ambient OpenSSH configuration

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
    ControlMasterLost,
    OutputLimitExceeded,
    ProvisioningError,
    RemoteCommandError,
    RemoteTimeout,
    TransportAuthError,
    TransportError,
    TransportHostKeyError,
    TransportUnreachableError,
    sanitize_detail,
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
# A remote script whose own signal trap fired exits 129 (HUP/INT/TERM); 130 and 143
# are SIGINT and SIGTERM. None of these is positive evidence that the watchdog
# TIMER fired, so they are reported as command failures, never as the bound.
_SCRIPT_TRAP_STATUS = 129
_SIGNAL_STATUSES = (129, 130, 143)
# The self-test must land within this window around the configured bound, and must
# show POSITIVE evidence the timer fired. A bare "status >= 128" (which would also
# accept a crash such as SIGSEGV 139) is deliberately NOT accepted.
_SELFTEST_LOWER_TOL = 0.5      # seconds below the bound that still count
_SELFTEST_UPPER_TOL = 3.0      # seconds above the bound that still count

# Local bound for `ssh -O check`, the cheap control-master liveness probe.
_MASTER_CHECK_BOUND = 10

# How long an idle control master is kept alive. Exposed so the CLI can refuse a
# loop interval at or above it (the master would idle-expire between attempts).
DEFAULT_CONTROL_PERSIST = 900

# --- master-shutdown outcome vocabulary -------------------------------------
# A shutdown is only "confirmed" by a POSITIVE follow-up observation. The other
# results are kept distinct because they mean different things: an absent socket is
# not the same fact as a probe that timed out, and neither proves that a detached
# master process has exited.
MS_NOT_ATTEMPTED = "no-master"                    # nothing was up and no socket existed
MS_GONE = "shutdown-verified"                     # the follow-up probe says it is gone
MS_STILL_ANSWERING = "shutdown-unconfirmed: master still answers the probe"
MS_PROBE_TIMEOUT = "shutdown-unconfirmed: the follow-up probe timed out"
MS_EXIT_FAILED = "shutdown-unconfirmed: the -O exit command did not complete"
# A CANCELLATION during the shutdown itself. Kept apart from the two results above
# because the cause is the operator's own signal, not the master or the link: the
# exit request was abandoned and the master still answers, or the follow-up probe was
# abandoned and the master's state is UNKNOWN.
MS_EXIT_INTERRUPTED = "shutdown-unconfirmed: the -O exit command was interrupted"
MS_PROBE_INTERRUPTED = "shutdown-unconfirmed: the follow-up probe was interrupted"

_UNCONFIRMED_SHUTDOWNS = (MS_STILL_ANSWERING, MS_PROBE_TIMEOUT, MS_EXIT_FAILED,
                          MS_EXIT_INTERRUPTED, MS_PROBE_INTERRUPTED)


def shutdown_is_unconfirmed(result):
    return result in _UNCONFIRMED_SHUTDOWNS

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

    SOURCE-DERIVED, NOT HARDWARE-VERIFIED: that the target's shell delivers a trap
    when the ssh channel closes has not been exercised on this unit's BusyBox ash /
    dropbear combination. The trap is a best-effort second line of defence; the
    normal path does not depend on it.
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
    trap that cleans up the reporter and the watchdog.

    Like the self-test, the watchdog writes a ``wd.fired`` marker IMMEDIATELY BEFORE
    it kills its target, and this command reports whether that marker exists. Without
    it a status of 137 (SIGKILL) is ambiguous: the watchdog's own SIGKILL and an OOM
    kill look identical, so the cause could not be attributed.
    """
    pid_file = "%s/wd.pid" % rd
    fired_file = "%s/wd.fired" % rd
    return (
        "p=; w=\n"
        + _trap_line(pid_file)
        + 'rm -f "%s"\n' % fired_file
        + '"%s/router_health" > "%s/out.json" 2> "%s/err.txt" &\n' % (rd, rd, rd)
        + "p=$!\n"
        + _wd_start(bound, "p", pid_file, fired_file)
        + 'ec=0\nwait "$p" || ec=$?\n'
        + _cancel(pid_file) + "\n"
        + 'cat "%s/out.json" 2>/dev/null\n' % rd
        + "printf 'router_health_exit=%s\\n' \"$ec\" 1>&2\n"
        + 'if [ -f "%s" ]; then printf "router_health_wd_fired=1\\n" 1>&2; '
          'else printf "router_health_wd_fired=0\\n" 1>&2; fi\n' % fired_file
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


def _child_env():
    """A minimal environment for an ssh child process.

    An inherited ``SSH_ASKPASS`` / ``SSH_ASKPASS_REQUIRE`` would let ssh obtain a
    password from an external helper program instead of the operator's terminal,
    so those variables (and ``SSH_ORIGINAL_COMMAND``) are removed. The password
    can then only come from /dev/tty, typed by the operator.
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("SSH_ASKPASS") or k == "SSH_ORIGINAL_COMMAND":
            env.pop(k, None)
    return env


def _last_token(raw):
    """The last whitespace-separated token of a device reply, or "".

    The device controls these bytes. ``text.split()`` returns [] for a reply that
    is empty OR made only of whitespace-like characters (including bytes that
    decode to U+00A0/U+2028 and control bytes such as 0x1C), so the result is
    indexed only after the empty case is handled. Reading ``[-1]`` on the raw
    split would raise IndexError and lose the attempt.
    """
    toks = _tokens(raw)
    return toks[-1] if toks else ""


def _tokens(raw):
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    return str(raw).split()


def _oneline(raw, limit=200):
    """A bounded, terminal-safe single-line rendering of captured device bytes."""
    return sanitize_detail(raw, limit=limit)


def _wd_fired(raw):
    """True when the remote run's stderr carries positive watchdog-timer evidence.

    The remote command prints ``router_health_wd_fired=1`` only when the watchdog's
    ``wd.fired`` marker exists, which it creates immediately before killing its
    target. Matching a COMPLETE marker (never a prefix of a longer digit run) keeps
    device-controlled text from forging or truncating the signal.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    return bool(re.search(r"router_health_wd_fired=1(?!\d)", str(raw), re.ASCII))


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
                log=None, max_stdout=DEFAULT_MAX_STDOUT, max_stderr=DEFAULT_MAX_STDERR,
                env=None):
    """Run argv in its own process group; kill and reap the GROUP on timeout,
    interruption OR an output-limit breach. Returns (rc, stdout_bytes, stderr_bytes).

    ``interactive=True`` lends the controlling terminal's foreground to the child's
    group for the duration, so a child that reads /dev/tty (an ssh password prompt)
    is not stopped by SIGTTIN. stdin is /dev/null, so no secret can travel through
    our pipes; the password is typed directly into the terminal by the operator and
    is never captured or logged.

    Output is drained incrementally under ``max_stdout`` / ``max_stderr`` byte
    ceilings; exceeding either terminates and reaps the child group and raises
    OutputLimitExceeded. ``env`` replaces the child's environment when given (used
    to strip SSH_ASKPASS* so a password can only come from the terminal).
    """
    kwargs = dict(_OWN_GROUP)
    if env is not None:
        kwargs["env"] = env
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
    """Map ssh stderr to the right exception type.

    Only used where authentication or the host-key check can legitimately be the
    cause: control-master ESTABLISHMENT. A multiplexed command runs over an
    already-pinned master, so its failures must not be classified from device text
    (see ``SSHTransport._rsh``).

    The embedded text is bounded and stripped of control bytes by
    ``sanitize_detail`` before it can reach a terminal or an outcome record.
    """
    t = (stderr_text or "").lower()
    if "host key verification failed" in t or "remote host identification has changed" in t \
            or "no matching host key" in t or ("host key" in t and "changed" in t):
        return TransportHostKeyError("pinned host key did not match: %s"
                                     % sanitize_detail(stderr_text))
    if "permission denied" in t or "authentication failed" in t or "too many authentication" in t:
        return TransportAuthError("ssh authentication was refused: %s"
                                  % sanitize_detail(stderr_text))
    if any(s in t for s in ("connection refused", "no route to host", "operation timed out",
                            "connection timed out", "network is unreachable",
                            "could not resolve hostname", "connection reset",
                            "connection closed by", "host is down", "no address associated")):
        return TransportUnreachableError(
            "management address unreachable: %s" % sanitize_detail(stderr_text))
    return TransportError("ssh failed: %s" % (sanitize_detail(stderr_text) or "unknown error"))


def classify_transport_failure(stderr_text):
    """Classify a MULTIPLEXED command's stderr — transport classes ONLY.

    A multiplexed command rides an already-established, already-pinned master, so
    it can no longer legitimately fail the host-key check or authentication. Mapping
    its stderr through ``classify_ssh_failure`` would let the DEVICE pick its own
    outcome class by printing "Host key verification failed" or "Permission denied"
    on its own stderr. Only the unreachable class is recognised here; everything
    else is a plain transport error. Authentication and host-key classes belong to
    master establishment, where they are decided by the ssh client itself.
    """
    t = (stderr_text or "").lower()
    if any(s in t for s in ("connection refused", "no route to host", "operation timed out",
                            "connection timed out", "network is unreachable",
                            "could not resolve hostname", "connection reset",
                            "connection closed by", "host is down", "no address associated",
                            "broken pipe", "transport endpoint is not connected",
                            "control socket connect")):
        return TransportUnreachableError(
            "management address unreachable: %s" % sanitize_detail(stderr_text))
    return TransportError("multiplexed ssh command failed: %s"
                          % (sanitize_detail(stderr_text) or "no diagnostic on stderr"))


class SSHSession:
    """Opaque handle for one provisioned collection session.

    ``ownership`` is a TRI-STATE, because "this run created it" and "we do not
    know" are different facts and only the first authorises a removal:

      ``none``     the remote directory provably was not created by this run
                   (mkdir was refused, or the name collided)
      ``unknown``  the name was chosen and the mkdir was SENT, but no reply ever
                   confirmed it; a directory MAY exist and must NOT be removed
                   automatically
      ``owned``    mkdir was acknowledged, so this run created the directory and
                   may remove exactly that path
    """

    def __init__(self, remote_dir, artifact_sha):
        self.remote_dir = remote_dir
        self.artifact_sha = artifact_sha
        self.provisioned = False
        self.ownership = "none"


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
        self._master_pid = None
        # Lifecycle facts for the collector: the precise master-shutdown result and,
        # when it is not confirmed, the reason. Recorded evidence only — nothing is
        # signalled from `_master_pid`.
        self.master_shutdown = None
        self.lifecycle_error = None

    # ------------------------------------------------------------- options
    def _ssh_opts(self):
        """The ssh option set. Deliberately HERMETIC.

        Every relevant setting is stated explicitly so the connection cannot be
        altered by ambient OpenSSH configuration: ``-F /dev/null`` ignores
        ``~/.ssh/config`` entirely, the system-wide known-hosts file is disabled,
        agent and connection forwarding are off, and ProxyCommand /
        KnownHostsCommand / UpdateHostKeys are pinned to their disabled forms.

        This does NOT change the working pinned connection: the bench host has no
        ``~/.ssh/config``, and the transport already selects password-only
        authentication with a pinned ``known_hosts`` and ``StrictHostKeyChecking``.
        """
        return [
            "-F", "/dev/null",
            "-o", "UserKnownHostsFile=%s" % self.known_hosts,
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "KnownHostsCommand=none",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "ProxyCommand=none",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=%d" % self.connect_timeout,
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
        ]

    def _multiplexed_opts(self):
        """Options for a command that must ride the EXISTING control master.

        ``BatchMode=yes`` makes a fallback unable to prompt for a password, and
        ``ControlMaster=no`` keeps it from opening a new master of its own — so a
        lost master can neither stall on a terminal read nor silently reconnect
        with interactive authentication.
        """
        return [
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=%s" % self._ctrl_path,
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
        self._master_pid = None
        if self._ctrl_dir and os.path.isdir(self._ctrl_dir):
            shutil.rmtree(self._ctrl_dir, ignore_errors=True)
        self._ctrl_dir = None
        self._ctrl_path = None

    def _master_check(self):
        """Ask OpenSSH itself whether the control master is alive.

        Returns ``(alive, pid, probe_failed)``. ``-O check`` exits 0 when the master
        answers and non-zero when the control socket is absent or dead. This is the
        authoritative liveness probe; ``_master_up`` alone only records what happened
        at establishment time.

        ``probe_failed`` is True when the PROBE itself could not be completed (a local
        deadline or an ssh binary that would not run). It is deliberately separate
        from ``alive=False``: a probe we could not run says the master's state is
        UNKNOWN, which must not be reported as "confirmed gone".

        The diagnostic is searched in **stderr as well as stdout**, and the CRLF form
        is tolerated. Real OpenSSH writes the success message with
        ``fprintf(stderr, "Master running (pid=%d)\\r\\n", ...)`` — independently
        verified by a follow-up review against `/usr/bin/ssh` on macOS — so parsing
        stdout alone finds `(True, None)`. The PID is used ONLY as recorded evidence:
        nothing is ever signalled from it (see ``_exit_master``).
        """
        if not self._ctrl_path:
            return False, None, False
        argv = [self.ssh_bin] + self._ssh_opts() + [
            "-o", "BatchMode=yes",
            "-o", "ControlPath=%s" % self._ctrl_path,
            "-O", "check", self._target(),
        ]
        try:
            rc, out, err = run_bounded(argv, _MASTER_CHECK_BOUND, env=_child_env(),
                                       max_stdout=4096, max_stderr=4096)
        except RemoteTimeout:
            return False, None, True
        except OSError:
            return False, None, True
        if rc != 0:
            return False, None, False
        combined = (out or b"") + b"\n" + (err or b"")
        m = re.search(rb"pid=(\d{1,10})", combined)
        return True, (int(m.group(1)) if m else None), False

    def _require_master(self):
        """Refuse a multiplexed command unless the master is PROVEN alive.

        Without this gate an OpenSSH client given only a ControlPath makes a fresh
        DIRECT connection as soon as the control socket is gone. That connection
        needs a password, and this call site has no terminal handover, so it would
        either stall on a `/dev/tty` read (SIGTTIN, surfacing as a bogus timeout) or
        fail confusingly. The gate converts a lost master into a fast, explicit,
        classified error instead.
        """
        if not self._master_up:
            raise ControlMasterLost(
                "ssh control master is not up; refusing to start a multiplexed command "
                "(a fresh connection would need interactive authentication)")
        alive, pid, probe_failed = self._master_check()
        if not alive:
            self._master_up = False
            self._master_pid = None
            if probe_failed:
                raise ControlMasterLost(
                    "the ssh control-master probe could not be completed (control socket "
                    "%s); the master's state is UNKNOWN, so no multiplexed command is "
                    "started and no fresh connection is made" % self._ctrl_path)
            raise ControlMasterLost(
                "ssh control master has gone away (control socket %s no longer answers); "
                "refusing to fall back to a fresh connection" % self._ctrl_path)
        if pid and pid > 0:
            self._master_pid = pid

    def _exit_master(self, verify=True):
        """Ask the master to exit, then VERIFY it is gone.

        Returns one of the ``MS_*`` results, distinguishing a confirmed exit from a
        master that still answers, a probe that timed out, an ``-O exit`` that did not
        complete, and the case where there was no master and no socket at all.

        Shutdown is never inferred from the ABSENCE of an error: success is the
        positive result of the follow-up ``-O check``.

        NO PROCESS IS SIGNALLED HERE. An earlier revision killed "the PID OpenSSH
        reported", which is unsafe: a cached PID can be reused by an unrelated
        process, and a PID alone carries no executable, owner or start-identity
        evidence. A follow-up review showed the cached PID would be signalled
        immediately and without any identity check, so the fallback was removed rather
        than hardened. The consequence is recorded instead: an unconfirmed shutdown
        becomes ``lifecycle_error`` on the transport, which the collector persists and
        reflects in the exit status. A master that genuinely will not exit is
        therefore left running — a resource cost that is reported, not a wrong
        process that is killed. ``ControlPersist`` is an IDLE timeout: it ends a
        master that has had no client for that long, and is NOT a deadline by which a
        stuck or still-busy master is guaranteed to be gone.

        A CANCELLATION during the shutdown is re-raised from here, but only AFTER the
        bounded follow-up probe (see ``_shutdown_master``). ``close()`` calls
        ``_shutdown_master`` directly so that it can also record the verdict and
        release the local resources before the cancellation continues.
        """
        result, interrupt = self._shutdown_master(verify)
        if interrupt is not None:
            raise interrupt
        return result

    def _shutdown_master(self, verify=True):
        """The shutdown itself. Returns ``(result, interrupt)`` and raises neither.

        ``interrupt`` is the cancellation-class exception (KeyboardInterrupt from
        Ctrl-C, or from SIGTERM/SIGHUP as routed by the CLI) that arrived during the
        exit request or the follow-up probe, or None. It is HELD rather than
        swallowed or allowed to skip the evidence: catching it and carrying on, as an
        earlier revision did, lost the operator's cancellation (a run that should
        have ended 130 continued, and could report ``shutdown-verified``); letting
        it straight through would leave the master's state unrecorded. The only work
        done while it is held is ONE bounded ``-O check``. Nothing is retried: an
        interrupted exit request is abandoned, and what the probe then observes is
        the recorded result. The caller MUST re-raise ``interrupt``.
        """
        if not self._ctrl_path:
            return MS_NOT_ATTEMPTED, None
        known_up = self._master_up
        socket_present = os.path.exists(self._ctrl_path)
        self._master_up = False
        if not (known_up or socket_present):
            return MS_NOT_ATTEMPTED, None
        exit_failed = False
        interrupt = None
        try:
            rc, _out, err = run_bounded(
                [self.ssh_bin] + self._ssh_opts() + [
                    "-o", "BatchMode=yes",
                    "-o", "ControlPath=%s" % self._ctrl_path,
                    "-O", "exit", self._target()],
                15, env=_child_env(), max_stdout=4096, max_stderr=4096)
            if rc != 0:
                exit_failed = True
                self.log("ssh -O exit returned %d: %s" % (rc, _oneline(err)))
        except Exception as e:                               # noqa: BLE001 - best effort
            exit_failed = True
            self.log("ssh -O exit did not complete: %s" % sanitize_detail(e))
        except BaseException as e:                           # noqa: BLE001 - held, see above
            interrupt = e
            self.log("ssh -O exit was INTERRUPTED by %s; the cancellation is kept and "
                     "re-raised after one bounded check of the master" % type(e).__name__)
        if not verify:
            return MS_NOT_ATTEMPTED, interrupt
        alive, pid, probe_failed, probe_interrupted = False, None, True, False
        try:
            alive, pid, probe_failed = self._master_check()
        except Exception:                                    # noqa: BLE001
            if interrupt is None:
                raise
            # a held cancellation must not be displaced by a probe fault: the probe
            # could not be completed, so the master's state stays UNKNOWN
        except BaseException as e:                           # noqa: BLE001 - held, see above
            probe_interrupted = True
            if interrupt is None:
                interrupt = e
            self.log("the follow-up master check was INTERRUPTED by %s; the master's "
                     "state is UNKNOWN" % type(e).__name__)
        self._master_pid = pid if (pid and pid > 0) else self._master_pid
        if probe_interrupted:
            return MS_PROBE_INTERRUPTED, interrupt
        if probe_failed:
            return MS_PROBE_TIMEOUT, interrupt
        if not alive:
            return MS_GONE, interrupt
        # The socket still answers. That is a positive observation, not an inference.
        if interrupt is not None:
            return MS_EXIT_INTERRUPTED, interrupt
        return (MS_STILL_ANSWERING if not exit_failed else MS_EXIT_FAILED), None

    def _start_master(self):
        try:
            self._start_master_inner()
        except BaseException:
            # even a failure or an interruption must not leave a master or a
            # control-socket directory behind. The release is in its own `finally`
            # because the best-effort exit can now re-raise a cancellation of its
            # own, which then takes precedence over the failure being handled.
            try:
                self._exit_master(verify=False)
            finally:
                self._release_master_resources()
            raise

    def _start_master_inner(self):
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
        # interactive prompt is possible at all.
        #
        # This is the ONLY call site allowed to hand over the terminal: interactive
        # authentication belongs to master establishment, never to a multiplexed
        # command.
        has_tty = has_controlling_terminal()
        interactive = has_tty if self.interactive_auth is None else bool(self.interactive_auth)
        if self.interactive_auth is None and not has_tty:
            self.log("no controlling terminal: interactive password authentication is "
                     "unavailable. Public-key authentication is DISABLED by this "
                     "transport (PubkeyAuthentication=no), so an installed key alone "
                     "would not enable unattended use - that needs its own implementation "
                     "change, not just a key in authorized_keys.")
        rc, _out, err = run_bounded(argv, self.connect_timeout + 20,
                                    interactive=interactive, log=self.log,
                                    env=_child_env(),
                                    max_stdout=self.max_stdout_bytes,
                                    max_stderr=self.max_stderr_bytes)
        if rc != 0:
            if rc == -signal.SIGINT:
                # Ctrl-C at the password prompt kills ssh itself. The operator
                # interrupted authentication; that is not a transport failure.
                raise KeyboardInterrupt
            raise classify_ssh_failure(err.decode("utf-8", "replace"))
        self._master_up = True
        alive, pid, probe_failed = self._master_check()
        if not alive:
            self._master_up = False
            raise ControlMasterLost(
                "ssh reported a successful master start but the control socket does "
                "not answer%s - refusing to continue"
                % (" (probe could not be completed)" if probe_failed else ""))
        self._master_pid = pid if (pid and pid > 0) else None
        self.log("ssh master established to %s" % self._target())

    def _rsh(self, cmd, timeout, stdin_bytes=None):
        """Run one command over the EXISTING control master."""
        self._require_master()
        argv = [self.ssh_bin] + self._ssh_opts() + self._multiplexed_opts()
        if stdin_bytes is None:
            argv += ["-n"]
        argv += [self._target(), cmd]
        rc, out, err = run_bounded(argv, timeout, stdin_bytes=stdin_bytes,
                                   env=_child_env(),
                                   max_stdout=self.max_stdout_bytes,
                                   max_stderr=self.max_stderr_bytes)
        if rc == 255:
            # A multiplexed command rides an established, pinned master, so it cannot
            # legitimately fail authentication or the host-key check. Its stderr is
            # classified into TRANSPORT classes only, never auth/hostkey: routing it
            # through classify_ssh_failure() would let the DEVICE choose its own
            # outcome class by printing "Host key verification failed" or
            # "Permission denied".
            raise classify_transport_failure(err.decode("utf-8", "replace"))
        return rc, out, err

    def _psh(self, stage, cmd, timeout, stdin_bytes=None):
        """Run one PROVISIONING command, tagging a PROVISIONING-SPECIFIC failure.

        Only one thing is rewritten here: a LOCAL DEADLINE expiry during
        provisioning. It previously surfaced as the reporter's own `timeout`
        ("reporter run exceeded its bound") even though the reporter had not started;
        it is now a provisioning failure that names the stage.

        Everything else passes through with its own classification on purpose: an
        unreachable address, an authentication refusal, a host-key mismatch and a
        control master loss keep their dedicated types (and outcomes), because those
        are the classes the recovery trial reasons about. The individual
        provisioning gates raise ``ProvisioningError`` themselves.
        """
        try:
            return self._rsh(cmd, timeout, stdin_bytes=stdin_bytes)
        except RemoteTimeout as e:
            raise ProvisioningError(
                "provisioning step '%s' exceeded its %ss deadline (the reporter never "
                "ran): %s" % (stage, timeout, sanitize_detail(e))) from e

    # ------------------------------------------------------------- session
    def start(self):
        self.preflight()
        self._start_master()
        remote_dir = "/tmp/rt." + secrets.token_hex(4)
        session = SSHSession(remote_dir, self.artifact_sha)
        try:
            self._provision(session)
        except BaseException as e:
            session.provisioned = False
            cleanup_ok = True
            cleanup_detail = None
            cleanup_interrupted = False
            cancelled = None        # a cancellation that arrived WHILE reconciling
            try:
                try:
                    self._dispose(session)
                    session.ownership = "none"
                except Exception as ce:                      # noqa: BLE001
                    cleanup_ok = False
                    cleanup_detail = sanitize_detail(ce)
                    self.log("cleanup after failed provision did not succeed: %s"
                             % cleanup_detail)
                except BaseException as ce:                  # noqa: BLE001 - cancellation
                    cleanup_ok = False
                    cleanup_interrupted = True
                    cleanup_detail = (
                        "removal of router dir %s after a failed provision was "
                        "INTERRUPTED by %s; its removal is UNCONFIRMED"
                        % (session.remote_dir, type(ce).__name__))
                    self.log("cleanup after failed provision: %s" % cleanup_detail)
                    raise
                finally:
                    self.close()
            except BaseException as ce:                      # noqa: BLE001
                if isinstance(ce, Exception):
                    raise
                # A cancellation during the removal or during the master exit. It is
                # the exception that must leave start() - but not at the price of the
                # facts below, which is what a bare re-raise from here would cost.
                cancelled = ce
            # Carry the provisioning-cleanup outcome OUT to the caller. A failed
            # provision is exactly when a remote directory is most likely to be left
            # behind, and the attempt record must say so rather than defaulting to
            # cleanup_ok=true.
            #
            # The facts are attached as ONE dict so their PRESENCE is unambiguous, and
            # they are attached to WHATEVER leaves: any exception class that ended the
            # provisioning phase (ControlMasterLost, ArtifactMismatch,
            # TransportUnreachableError, ...), a cancellation that ended it, and a
            # cancellation that arrived afterwards while this handler was reconciling.
            # The individual attributes are kept too, for callers and test doubles
            # written against the earlier shape.
            leaving = e if cancelled is None else cancelled
            if leaving is not e:
                self.log("provisioning had already failed (%s: %s) when %s arrived; the "
                         "cancellation is propagated with the provisioning facts"
                         % (type(e).__name__, sanitize_detail(e), type(leaving).__name__))
            facts = {
                "remote_dir": session.remote_dir,
                "remote_ownership": session.ownership,
                "cleanup_ok": cleanup_ok,
                "cleanup_detail": cleanup_detail,
                "cleanup_interrupted": cleanup_interrupted,
            }
            setattr(leaving, "provision_facts", facts)
            setattr(leaving, "remote_dir", session.remote_dir)
            setattr(leaving, "remote_ownership", session.ownership)
            setattr(leaving, "provision_cleanup_ok", cleanup_ok)
            setattr(leaving, "provision_cleanup_detail", cleanup_detail)
            raise leaving
        return session

    def _provision(self, session):
        rd = session.remote_dir
        self.log("router temp dir: %s" % rd)

        # 1. OpenSSL SHA-256 known-answer test (the router has no sha256sum/od)
        rc, out, err = self._psh("openssl sha256 known-answer test", KAT_CMD,
                                 self.connect_timeout + 10)
        kat = _last_token(out)
        if rc != 0 or kat != OPENSSL_KAT_ABC:
            raise ProvisioningError(
                "openssl SHA-256 known-answer test FAILED (rc=%d, got %s)"
                % (rc, sanitize_detail(kat) or "<empty>"))

        # 2. required-tool hard gate: require rc==0 AND exactly one well-formed
        #    marker line. A nonzero exit, a missing marker, several markers or an
        #    unrecognised marker all stop provisioning BEFORE any remote state is
        #    created or anything is executed.
        rc, out, err = self._psh("required-tool probe", TOOLS_CMD, self.connect_timeout + 10)
        if rc != 0:
            raise ProvisioningError(
                "required-tool probe failed (rc=%d): %s - hard stop" % (rc, _oneline(err)))
        markers = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines()
                   if ln.strip()]
        if len(markers) != 1:
            raise ProvisioningError(
                "required-tool probe returned %d marker line(s), expected exactly 1 "
                "- ambiguous, hard stop" % len(markers))
        marker = markers[0]
        if marker == "TOOLS_OK":
            pass
        elif marker.startswith("TOOLS_MISSING:"):
            miss = marker[len("TOOLS_MISSING:"):].strip()
            if not miss:
                raise ProvisioningError(
                    "required-tool probe reported an empty MISSING list - malformed, hard stop")
            raise ProvisioningError("required router tools MISSING: %s - hard stop"
                                    % sanitize_detail(miss))
        else:
            raise ProvisioningError(
                "required-tool probe returned an unrecognised result %s - hard stop"
                % sanitize_detail(marker))

        # 3. exclusive private directory (ownership tracking).
        #    From HERE a directory may exist: the mkdir is about to be sent and its
        #    reply can be lost. Ownership therefore becomes "unknown" FIRST and is
        #    upgraded to "owned" only when the reply acknowledges creation, or set
        #    back to "none" when the reply says the directory was not created.
        session.ownership = "unknown"
        rc, _out, _err = self._psh(
            "exclusive mkdir", 'mkdir -m 700 "%s" && [ -d "%s" ]' % (rd, rd),
            self.connect_timeout + 10)
        if rc != 0:
            session.ownership = "none"
            raise ProvisioningError(
                "exclusive mkdir failed at %s - this run created nothing" % rd)
        session.ownership = "owned"

        # 4. PROVE the remote watchdog bounds a long job before it is trusted
        if self.watchdog_selftest_enabled:
            self._watchdog_selftest(session)

        # 5. transfer + router-side SHA-256 gate (before any execution)
        with open(self.artifact, "rb") as fh:
            blob = fh.read()
        xfer = ('cat > "%s/router_health" && chmod 700 "%s/router_health" && '
                '/usr/sbin/openssl dgst -sha256 "%s/router_health"' % (rd, rd, rd))
        rc, out, err = self._psh("artifact transfer + router-side SHA-256", xfer,
                                 self.connect_timeout + 30, stdin_bytes=blob)
        rsha = _last_token(out)
        if rc != 0:
            raise ProvisioningError("transfer command failed (rc=%d): %s"
                                    % (rc, _oneline(err)))
        if self.artifact_sha and rsha != self.artifact_sha:
            # A hash gate failure keeps its own type (a hard stop, not a retry).
            raise ArtifactMismatch("router-side SHA-256 mismatch (%s) - refusing to execute"
                                   % (sanitize_detail(rsha) or "<empty>"))

        # 6. control: prove /tmp can execute at all. rc==0 alone is not accepted:
        #    the control command must also print its marker.
        ctl = ('cp /bin/busybox "%s/busybox" && chmod 700 "%s/busybox" && '
               '"%s/busybox" echo control-exec-ok; ec=$?; rm -f "%s/busybox"; exit $ec'
               % (rd, rd, rd, rd))
        rc, out, err = self._psh("control exec from /tmp", ctl, self.connect_timeout + 10)
        if rc != 0:
            raise ProvisioningError("control exec from /tmp failed (rc=%d): %s"
                                    % (rc, _oneline(err)))
        if b"control-exec-ok" not in (out or b""):
            raise ProvisioningError(
                "control exec reported rc=0 without the control-exec-ok marker - "
                "executing from /tmp is not proven - hard stop")

        session.provisioned = True
        self.log("artifact verified on the router (sha256 %s)"
                 % ((rsha[:16] + "...") if rsha else "?"))

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
        rc, out, err = self._psh("watchdog self-test", cmd,
                                 bound + self.connect_timeout + 10)
        elapsed = time.monotonic() - started
        if rc != 0:
            raise ProvisioningError(
                "watchdog self-test did not complete (rc=%d): %s" % (rc, _oneline(err)))
        text = out.decode("utf-8", "replace")
        # The exit status is matched as a COMPLETE 1..3-digit ASCII number. An
        # unbounded \d+ would let a device reply such as `selftest_exit=` followed by
        # thousands of digits reach int() and raise ValueError (Python's int/str
        # conversion limit), losing the attempt; and a bare \d{1,3} would silently
        # accept the first three digits of a longer run. The negative lookahead
        # rejects a prefix of a longer digit run, so such a reply is reported as
        # "missing or ambiguous", which is the honest classification.
        exits = re.findall(r"selftest_exit=(\d{1,3})(?!\d)", text, re.ASCII)
        fired = re.findall(r"selftest_wd_fired=([01])(?!\d)", text, re.ASCII)
        if len(exits) != 1 or len(fired) != 1:
            raise ProvisioningError(
                "watchdog self-test result is missing or ambiguous "
                "(exit markers=%d, fired markers=%d)" % (len(exits), len(fired)))
        st = int(exits[0])
        fired_flag = int(fired[0])
        if st == 0:
            raise ProvisioningError(
                "watchdog self-test FAILED: the long job completed instead of being "
                "killed at %ss - the remote bound is NOT proven - refusing to run" % bound)
        if st != _WD_SIGKILL_STATUS:
            raise ProvisioningError(
                "watchdog self-test FAILED: unexpected termination status %d "
                "(expected %d from the watchdog's SIGKILL) - refusing to run"
                % (st, _WD_SIGKILL_STATUS))
        if fired_flag != 1:
            raise ProvisioningError(
                "watchdog self-test FAILED: no evidence the watchdog timer fired "
                "(the job died from something other than the bound) - refusing to run")
        lower = max(0.0, bound - _SELFTEST_LOWER_TOL)
        upper = bound + _SELFTEST_UPPER_TOL
        if not (lower <= elapsed <= upper):
            raise ProvisioningError(
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
        if rc == _WD_SIGKILL_STATUS:
            if _wd_fired(err):
                raise RemoteTimeout("reporter run was killed by the remote watchdog "
                                    "(rc=%d, SIGKILL, timer-fired evidence present)" % rc)
            # A SIGKILL is ambiguous on its own: an OOM kill looks exactly the same.
            # Without the timer-fired marker the cause is NOT established, so it is
            # reported as a command failure rather than claimed to be the bound.
            raise RemoteCommandError(rc, (
                "reporter run was killed by SIGKILL (rc=%d) with NO watchdog timer-fired "
                "evidence, so this is NOT attributable to the bound (an out-of-memory kill "
                "looks identical). Treat the cause as unestablished: %s"
                % (rc, _oneline(err))))
        if rc in _SIGNAL_STATUSES:
            # 129 is this script's own trap exit for HUP/INT/TERM; 130 and 143 are
            # SIGINT and SIGTERM. None of them is the watchdog's SIGKILL, and there
            # is no timer-fired evidence, so this is reported as a command failure
            # rather than claimed to be the bound.
            raise RemoteCommandError(rc, (
                "reporter run was ended by signal %d (rc=%d); this is NOT proven to be "
                "the watchdog timer (the watchdog's status is %d): %s"
                % (rc - 128, rc, _WD_SIGKILL_STATUS, _oneline(err))))
        if rc != 0:
            raise RemoteCommandError(rc, "reporter exited %d: %s" % (rc, _oneline(err)))
        return out

    # ------------------------------------------------------------- cleanup
    def _dispose(self, session):
        """Remove the remote dir THIS run created. Raises CleanupError on failure.

        Ownership is respected exactly:
          * ``none``    nothing was created - nothing to do;
          * ``unknown`` the mkdir was sent but never acknowledged, so a directory MAY
                        exist and is deliberately NOT removed automatically (removing
                        it would be acting on a guess); the caller records the
                        uncertainty instead;
          * ``owned``   the mkdir was acknowledged - remove exactly that path, and
                        require the removal's own positive marker.
        """
        if session.ownership == "none":
            return
        if session.ownership != "owned":
            raise CleanupError(
                "remote dir %s may exist but its creation was never acknowledged "
                "(ownership=unknown); NOT removing it automatically - it needs read-only "
                "manual reconciliation" % session.remote_dir)
        if not self._master_up:
            raise CleanupError(
                "ssh master is down; remote dir %s may remain on the router" % session.remote_dir)
        cmd = CLEAN_CMD.format(rd=session.remote_dir)
        try:
            rc, out, err = self._rsh(cmd, self.connect_timeout + 10)
        except TransportError as e:
            raise CleanupError("failed to remove router dir %s: %s"
                               % (session.remote_dir, sanitize_detail(e)))
        if rc != 0:
            raise CleanupError("failed to remove router dir %s (rc=%d)" % (session.remote_dir, rc))
        if b"REMOVED" not in (out or b""):
            # Never infer success from the absence of an error: require the command's
            # own positive marker.
            raise CleanupError(
                "removal of router dir %s reported rc=0 without the REMOVED marker - "
                "the directory's absence is NOT confirmed" % session.remote_dir)

    def stop(self, session):
        """Tear the session down. A cleanup failure is raised AFTER the master is
        closed, so teardown always completes.

        A CANCELLATION leaves with ``teardown_facts`` attached, because "teardown was
        interrupted" is not one fact: an interrupt during the REMOVAL leaves the
        directory unconfirmed, while an interrupt during the MASTER EXIT that follows
        a confirmed removal leaves no directory to reconcile at all. The caller needs
        to know which, or it would queue a removed directory for manual cleanup - or
        lose the reason an earlier removal had already failed.
        """
        err = None
        removal_ok = False
        removal_detail = None
        removal_interrupted = False
        try:
            try:
                self._dispose(session)
                removal_ok = True
            except CleanupError as e:
                err = e
                removal_detail = sanitize_detail(e)
            except BaseException as e:                       # noqa: BLE001 - re-raised
                removal_interrupted = not isinstance(e, Exception)
                removal_detail = (
                    "removal of router dir %s was INTERRUPTED by %s; its removal is "
                    "UNCONFIRMED" % (session.remote_dir, type(e).__name__)
                ) if removal_interrupted else sanitize_detail(e)
                raise
            finally:
                self.close()
        except BaseException as e:                           # noqa: BLE001 - re-raised
            if not isinstance(e, Exception):
                setattr(e, "teardown_facts", {
                    "remote_dir": session.remote_dir,
                    "remote_ownership": session.ownership,
                    "cleanup_ok": removal_ok,
                    "cleanup_detail": removal_detail,
                    "cleanup_interrupted": removal_interrupted,
                    "interrupted_stage": ("directory-removal" if removal_interrupted
                                          else "master-exit"),
                })
            raise
        if err is not None:
            raise err

    def close(self):
        """Stop the control master (verified) and release the local socket dir.

        An UNCONFIRMED shutdown is not merely logged: it is recorded on the transport
        as ``lifecycle_error`` (with the precise ``master_shutdown`` result) so the
        collector can persist it durably and reflect it in the exit status. A run that
        cannot confirm it stopped an authenticated master must not look successful.

        A cancellation that arrived during the shutdown is re-raised LAST: after the
        verdict is recorded and after the local resources are released, so it can
        neither be swallowed nor skip either of them.
        """
        interrupt = None
        try:
            result, interrupt = self._shutdown_master()
            self.master_shutdown = result
            if result == MS_GONE:
                self.log("ssh control master exited (shutdown verified)")
            elif shutdown_is_unconfirmed(result):
                self.lifecycle_error = (
                    "ssh control master shutdown is NOT confirmed (%s); an authenticated "
                    "master process may still be running. ControlPersist is an IDLE "
                    "timeout, not a deadline for a stuck or busy master. No process was "
                    "signalled." % result)
                self.log("WARNING: %s" % self.lifecycle_error)
            else:
                self.log("no ssh control master to stop (no control socket and none up)")
        finally:
            self._release_master_resources()
        if interrupt is not None:
            raise interrupt
