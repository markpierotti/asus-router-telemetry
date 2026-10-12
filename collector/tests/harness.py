"""Shared offline test harness.

Builds a real `SSHTransport` wired to the fake `ssh` (no network), and helpers to
run the REAL remote command text under a local POSIX shell so its timing and
process behaviour are exercised, not just its canned output.
"""

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "live_sample.json")
FAKE_SSH = os.path.join(HERE, "fake_ssh.sh")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rtax_telemetry.transport import SSHTransport  # noqa: E402


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_artifact(workdir, data=b"FAKE-ARM-ARTIFACT-BYTES" * 100):
    path = os.path.join(workdir, "artifact")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def make_known_hosts(workdir):
    path = os.path.join(workdir, "known_hosts")
    with open(path, "w") as fh:
        fh.write("192.0.2.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample\n")
    return path


def make_wrapper(workdir, mode, name="fake-ssh", extra_env=None):
    """A stand-in ssh that fixes the fake's mode/doc via the environment."""
    path = os.path.join(workdir, name)
    lines = ['#!/bin/bash',
             'export FAKE_SSH_MODE=%s' % mode,
             'export FAKE_SSH_DOC=%s' % FIXTURE]
    for k, v in (extra_env or {}).items():
        lines.append('export %s=%s' % (k, v))
    lines.append('exec "%s" "$@"' % FAKE_SSH)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


def make_transport(workdir, mode="ok", *, artifact=None, known_hosts=None,
                   artifact_sha=None, known_hosts_sha=None, verify_artifact=True,
                   run_bound=5, connect_timeout=5, watchdog_selftest=True,
                   watchdog_selftest_bound=2, interactive_auth=False, log=None,
                   extra_env=None):
    artifact = artifact or make_artifact(workdir)
    known_hosts = known_hosts or make_known_hosts(workdir)
    wrapper = make_wrapper(workdir, mode, extra_env=extra_env)
    return SSHTransport(
        "192.0.2.1", "routeradmin",
        artifact=artifact,
        known_hosts=known_hosts,
        artifact_sha=artifact_sha if artifact_sha is not None else _sha(artifact),
        known_hosts_sha=known_hosts_sha,      # None -> skip that gate in unit tests
        ssh_bin=wrapper,
        connect_timeout=connect_timeout,
        run_bound=run_bound,
        watchdog_selftest=watchdog_selftest,
        watchdog_selftest_bound=watchdog_selftest_bound,
        verify_artifact=verify_artifact,
        interactive_auth=interactive_auth,
        log=log or (lambda *a: None),
    )


def write_ssh_config(data_dir, workdir, mode="ok", extra_env=None, **extra):
    """Write <data_dir>/config.json pointing the CLI at the fake ssh.

    ``extra_env`` is exported by the wrapper (FAKE_SSH_* knobs); every other keyword
    is a config.json key.
    """
    import json
    artifact = make_artifact(workdir)
    kh = make_known_hosts(workdir)
    wrapper = make_wrapper(workdir, mode, extra_env=extra_env)
    cfg = {
        "host": "192.0.2.1",
        "user": "routeradmin",
        "artifact": artifact,
        "known_hosts": kh,
        "artifact_sha": _sha(artifact),
        "known_hosts_sha": _sha(kh),
        "ssh": wrapper,
        "connect_timeout": 5,
        "run_bound": 5,
        "watchdog_selftest_bound": 2,
        "interactive_auth": False,
    }
    cfg.update(extra)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w") as fh:
        json.dump(cfg, fh)
    return cfg


def run_cli(args, timeout=60):
    return subprocess.run([sys.executable, os.path.join(ROOT, "collector.py")] + list(args),
                          capture_output=True, text=True, timeout=timeout)


# ---- local execution of the REAL remote command text ----------------------
def remote_dir(workdir, reporter_body=None, name="rt"):
    rd = os.path.join(workdir, name)
    os.makedirs(rd, exist_ok=True)
    if reporter_body is not None:
        path = os.path.join(rd, "router_health")
        with open(path, "w") as fh:
            fh.write(reporter_body)
        os.chmod(path, 0o755)
    return rd


def run_local_script(text, workdir, timeout=30, name="script.sh"):
    path = os.path.join(workdir, name)
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)
    return subprocess.run(["sh", path], capture_output=True, text=True, timeout=timeout)
