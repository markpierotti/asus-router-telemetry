#!/usr/bin/env python3
"""Opt-in root integration check, in a PRIVATE mount/network namespace only.

Run: sudo unshare --mount --net --propagation private python3 tests/syslog_storage_linux.py
Uses a disposable 32 MiB image and loopback UDP; never opens the production log.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'usr/local/bin/slim-syslog-storage'


def run(*args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('condition did not become true')


def main():
    assert os.geteuid() == 0 and sys.platform == 'linux'
    # Both namespaces must differ from PID 1; fail before creating any files.
    for ns in ('mnt', 'net'):
        assert os.readlink('/proc/self/ns/' + ns) != os.readlink('/proc/1/ns/' + ns), ns
    run('ip', 'link', 'set', 'lo', 'up')
    base = Path(tempfile.mkdtemp(prefix='asus-storage-test-'))
    image, mount = base / 'private/logs.ext4', base / 'logs'
    mount.mkdir(mode=0o700)
    daemon = None
    try:
        run(sys.executable, HELPER, 'create', '--image', image, '--size-mib', '32')
        before = image.stat()
        # Repeated create must refuse without touching the existing filesystem.
        again = subprocess.run([sys.executable, str(HELPER), 'create', '--image', str(image), '--size-mib', '32'], capture_output=True)
        assert again.returncode != 0 and image.stat().st_ino == before.st_ino
        run('mount', '-o', 'loop,nodev,nosuid,noexec', image, mount)
        check = [sys.executable, HELPER, 'check', '--image', image, '--mountpoint', mount, '--size-mib', '32']
        run(*check)
        cfg = (ROOT / 'etc/rsyslog.d/10-asus-remote.conf').read_text()
        cfg = cfg.replace('192.0.2.20', '127.0.0.1').replace('192.0.2.10', '127.0.0.1')
        cfg = cfg.replace('port="514"', 'port="15514"').replace('/var/log/asus', str(mount))
        cfg = cfg.replace('action.resumeInterval="5"', 'action.resumeInterval="1"')
        # Route daemon diagnostics to a measured, separate host file.
        cfg += '\n*.* action(type="omfile" file="' + str(base / 'diagnostics') + '")\n'
        conf = base / 'rsyslog.conf'
        conf.write_text(cfg)
        # Ubuntu's pathname-attached production AppArmor profile excludes /tmp.
        # Run a disposable binary copy; do not relax the live daemon's profile.
        binary = base / 'rsyslogd-test'
        shutil.copy2(shutil.which('rsyslogd'), binary)
        run(binary, '-N1', '-f', conf)
        stderr = (base / 'stderr').open('wb')
        daemon = subprocess.Popen([str(binary), '-n', '-i', str(base / 'pid'), '-f', str(conf)], stdout=subprocess.DEVNULL, stderr=stderr)
        time.sleep(.3)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def send(text, count=1):
            for n in range(count):
                sock.sendto(('<14>Oct 19 12:00:00 router test: ' + text + str(n)).encode(), ('127.0.0.1', 15514))

        log = mount / 'asus.log'
        send('legitimate-before')
        wait_for(lambda: log.exists() and b'legitimate-before' in log.read_bytes())
        # Rotation keeps the .1 inode available to the existing incremental readers.
        log.rename(mount / 'asus.log.1')
        daemon.send_signal(1)
        send('legitimate-after-rotation', 10)
        wait_for(lambda: log.exists() and b'legitimate-after-rotation' in log.read_bytes())
        # Incompressible current/rotated content shares one hard filesystem budget.
        with (mount / 'full').open('wb', buffering=0) as f:
            while True:
                try:
                    f.write(os.urandom(1024 * 1024))
                except OSError as e:
                    assert e.errno == errno.ENOSPC
                    break
        send('overflow-' + 'x' * 1000, 2000)
        time.sleep(2)
        run(*check)
        assert image.stat().st_size == 32 * 1024 ** 2
        assert image.stat().st_blocks * 512 <= before.st_blocks * 512
        diagnostics = sum(p.stat().st_size for p in (base / 'diagnostics', base / 'stderr') if p.exists())
        if diagnostics >= 16384:
            for path in (base / 'diagnostics', base / 'stderr'):
                if path.exists():
                    print(path.name, path.stat().st_size, path.read_text(errors='replace')[:1500])
        assert diagnostics < 16384, diagnostics
        (mount / 'full').unlink()
        wait_for(lambda: daemon.poll() is None)
        for _ in range(20):
            send('legitimate-recovery')
            time.sleep(.3)
            if b'legitimate-recovery' in log.read_bytes():
                break
        assert b'legitimate-recovery' in log.read_bytes()
        daemon.terminate()
        daemon.wait(timeout=10)
        daemon = None
        stderr.close()
        run('umount', mount)
        assert subprocess.run([str(a) for a in check], capture_output=True).returncode != 0
        run('mount', '-o', 'loop,nodev,nosuid,noexec', image, mount)
        run(*check)
        assert b'legitimate-recovery' in log.read_bytes()
        print(json.dumps({'result': 'PASS', 'budget_bytes': image.stat().st_size,
                          'diagnostic_bytes': diagnostics, 'full_recovery_rotation_persistence': True}))
    finally:
        if daemon and daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=10)
        subprocess.run(['umount', str(mount)], capture_output=True)
        shutil.rmtree(base)


if __name__ == '__main__':
    main()
