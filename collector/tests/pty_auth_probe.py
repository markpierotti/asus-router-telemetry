#!/usr/bin/env python3
"""Helper for the controlling-PTY regression tests.

The test launches this with a PTY slave as its stdin. It becomes a session leader,
acquires that PTY as its CONTROLLING TERMINAL, then exercises the transport either

  PTY_MODE=run_bounded   directly through run_bounded(interactive=...)
  PTY_MODE=start         through SSHTransport.start() (interactive MASTER startup),
                         i.e. the real password-prompt path
  PTY_MODE=detect        only reports transport.has_controlling_terminal()

so the child can only succeed if the terminal's foreground group is handled
correctly (otherwise the read stops on SIGTTIN).

Before it signals readiness the probe also records, in PTY_PRECHECK, what THIS
RUNNER lets a process do with the controlling terminal it has just acquired. That
record uses plain ``os`` calls and no transport code, so it describes the
environment and nothing else: a sandboxed runner can grant the terminal and still
refuse to let it be opened for writing, which is exactly the open ssh needs in
order to prompt.

Driven by tests/test_pty_and_reap.py. No network, no router.
"""

import errno
import fcntl
import os
import sys
import termios

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rtax_telemetry import transport  # noqa: E402


def _open_verdict(flags):
    """``ok``, or the errno the runner answered with, for one open of /dev/tty."""
    try:
        os.close(os.open("/dev/tty", flags))
    except OSError as e:
        return "errno %s %s (%s)" % (e.errno, errno.errorcode.get(e.errno, "?"),
                                     e.strerror)
    return "ok"


def terminal_precheck():
    """What this runner permits on the controlling terminal - no transport code.

    One ``key=value`` per line. ``devtty_rdwr`` is the one that matters: it is the
    open ssh's own password prompt performs, and the one the transport uses both to
    decide that a terminal is usable and to hand its foreground to the child.
    """
    return ("devtty_rdonly=%s\ndevtty_rdwr=%s\n"
            % (_open_verdict(os.O_RDONLY), _open_verdict(os.O_RDWR)))


def main():
    transport.REAP_GRACE = 0.5          # keep the negative case quick

    try:
        os.setsid()                     # new session: needed to acquire a controlling tty
    except OSError as e:
        sys.stderr.write("setsid failed: %s\n" % e)
        return 2
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError as e:
        sys.stderr.write("TIOCSCTTY failed: %s\n" % e)
        return 3

    # Recorded BEFORE readiness is signalled, so it exists for every run the test
    # goes on to judge.
    precheck = os.environ.get("PTY_PRECHECK")
    if precheck:
        with open(precheck, "w") as fh:
            fh.write(terminal_precheck())

    with open(os.environ["PTY_READY"], "w") as fh:
        fh.write("ready")

    mode = os.environ.get("PTY_MODE", "run_bounded")
    try:
        if mode == "detect":
            result = "has_tty=%s" % transport.has_controlling_terminal()
        elif mode == "start":
            tr = transport.SSHTransport(
                os.environ.get("PTY_HOST", "192.0.2.1"),
                os.environ.get("PTY_USER", "routeradmin"),
                artifact=os.environ["PTY_ARTIFACT"],
                known_hosts=os.environ["PTY_KNOWN_HOSTS"],
                artifact_sha=os.environ["PTY_ARTIFACT_SHA"],
                known_hosts_sha=None,
                ssh_bin=os.environ["PTY_SSH_BIN"],
                connect_timeout=5,
                run_bound=5,
                watchdog_selftest=True,
                watchdog_selftest_bound=2,
                interactive_auth=None,          # auto-detect -> the PTY counts
                log=lambda m: None,
            )
            session = tr.start()
            result = "started:%s" % session.remote_dir
            tr.close()
        else:
            interactive = os.environ.get("PTY_INTERACTIVE", "1") == "1"
            timeout = float(os.environ.get("PTY_TIMEOUT", "4"))
            before_term = termios.tcgetattr(0)
            before_pgrp = os.tcgetpgrp(0)
            try:
                rc, _out, _err = transport.run_bounded(
                    [os.environ["PTY_FAKE_SSH"]], timeout, interactive=interactive)
                result = "rc=%d" % rc
            except Exception as e:                          # noqa: BLE001
                result = "exc=%s" % type(e).__name__
            # the terminal's foreground group AND its settings must be restored
            try:
                restored = ((termios.tcgetattr(0) == before_term)
                            and (os.tcgetpgrp(0) == before_pgrp))
            except OSError as e:
                restored = "err:%s" % e
            result += ";restored=%s" % restored
    except Exception as e:                                  # noqa: BLE001
        result = "exc=%s" % type(e).__name__

    with open(os.environ["PTY_MARKER"], "w") as fh:
        fh.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
