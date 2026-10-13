#!/usr/bin/env python3
"""reporter_audit_regressions.py <binary> - regressions for the reporter defects
reproduced by the independent audit of 8778fb3 (reporter 1.0.4).

  R1  a `cpu<digit>...` key in /proc/stat is printed with a bare "%s". A key that
      carries a quote, a backslash or an invalid byte, or a repeated key, gives a
      document that is not valid JSON / UTF-8 or has duplicate members - with exit 0
  R2  `limits.files_truncated` is printed BEFORE cpuinfo, loadavg and meminfo are
      read, so an oversized one of those reports 0. A COMPLETE file of exactly
      file_read_max_bytes - 1 bytes is counted as truncated
  R3  an unreadable /proc/mounts and an empty one produce byte-identical documents,
      so "storage disappeared" and "could not look" cannot be told apart

Checks tagged REGRESSION fail on 8778fb3 and must pass after the correction. Checks
tagged GUARD pass on both: they pin what the correction must not change.

The checks state the invariant, not the fix: R1 does not say whether a malformed key
is skipped or escaped, and R3 does not say how the difference is represented.

Read-only; touches only a local temporary tree. No router contact.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

SLURP_MAX = 4 * 1024 * 1024

BASE = {
    "uptime": b"5342.20 12345.67\n",
    "loadavg": b"2.01 1.87 1.83 2/146 9999\n",
    "cpuinfo": b"processor : 0\nmodel name : ARMv7\nBogoMIPS : 100.00\n",
    "meminfo": b"MemTotal: 510456 kB\nMemFree: 303472 kB\nMemAvailable: 233476 kB\n",
    "stat": b"cpu 1 2 3 4 5 6 7 8\ncpu0 1 2 3 4 5 6 7 8\nbtime 1700000000\n",
    "mounts": b"/dev/root / ext4 ro 0 0\n",
}

fails = []


def chk(cond, tag, msg):
    print(("PASS  " if cond else "FAIL  ") + "%-10s %s" % (tag, msg))
    if not cond:
        fails.append(msg)


class DuplicateKey(ValueError):
    pass


def _no_dups(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise DuplicateKey("duplicate member %r" % k)
        seen[k] = v
    return seen


def run(binary, tmp, files):
    """Run the reporter against a synthetic /proc; a None value omits that file."""
    d = tempfile.mkdtemp(prefix="proc.", dir=tmp)
    tree = dict(BASE)
    tree.update(files)
    for name, data in tree.items():
        if data is None:
            continue
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(data)
    p = subprocess.run([binary, "--proc-root", d], capture_output=True, timeout=60)
    shutil.rmtree(d, ignore_errors=True)
    return p.returncode, p.stdout


def strict(raw):
    """(document, None) when raw is UTF-8, JSON and free of duplicate members."""
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dups), None
    except UnicodeDecodeError as e:
        return None, "invalid UTF-8 (%s)" % e.reason
    except DuplicateKey as e:
        return None, str(e)
    except ValueError as e:
        return None, "invalid JSON (%s)" % str(e)[:60]


def main():
    if len(sys.argv) != 2 or not os.access(sys.argv[1], os.X_OK):
        print("usage: reporter_audit_regressions.py <binary>")
        return 2
    binary = os.path.abspath(sys.argv[1])
    tmp = tempfile.mkdtemp(prefix="reporter-audit.")
    try:
        checks(binary, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("reporter audit regression failures: %d" % len(fails))
    return 1 if fails else 0


def checks(binary, tmp):
    agg = b"cpu 1 2 3 4 5 6 7 8\n"

    print("=== R1: a hostile or repeated cpu_stat key must not break the document ===")
    for label, stat in (
            ("a quote in the key", agg + b'cpu0"x 1 2 3 4 5 6 7 8\n'),
            ("a backslash in the key", agg + b"cpu0\\ 1 2 3 4 5 6 7 8\n"),
            ("an invalid byte in the key", agg + b"cpu0\xff 1 2 3 4 5 6 7 8\n"),
            ("a repeated cpu0 line", agg + b"cpu0 1 2 3 4 5 6 7 8\ncpu0 9 9 9 9 9 9 9 9\n")):
        rc, out = run(binary, tmp, {"stat": stat})
        doc, why = strict(out)
        chk(rc != 0 or doc is not None, "REGRESSION",
            "%s: exit 0 implies a strict UTF-8/JSON document (rc=%d, %s)"
            % (label, rc, why or "ok"))
        if doc is not None:
            chk(doc["cpu_stat"]["cpu"]["idle"] == 4, "GUARD",
                "%s: the aggregate 'cpu' entry is still reported" % label)

    rc, out = run(binary, tmp, {"stat": agg + b"cpu0 1 2 3 4 5 6 7 8\n"
                                              b"cpu1 2 3 4 5 6 7 8 9\ncpu10 3 4 5 6 7 8 9 10\n"})
    doc, why = strict(out)
    chk(rc == 0 and doc is not None, "GUARD", "well-formed keys give a strict document")
    if doc is not None:
        st = doc["cpu_stat"]
        chk(sorted(st) == ["cpu", "cpu0", "cpu1", "cpu10"], "GUARD",
            "well-formed keys are all emitted, multi-digit included (%r)" % sorted(st))
        chk(st["cpu1"]["user"] == 2 and st["cpu10"]["steal"] == 10, "GUARD",
            "well-formed counters keep their values")

    print("=== R2: limits.files_truncated must count every partially read file ===")
    pad = b"x" * (5 * 1024 * 1024)
    for name, data in (
            ("cpuinfo", BASE["cpuinfo"] + pad + b"\n"),
            ("meminfo", BASE["meminfo"] + b"Pad: 1 kB\n" * 600000),
            ("loadavg", b"2.01 1.87 1.83 2/146 9999 " + pad)):
        rc, out = run(binary, tmp, {name: data})
        doc, why = strict(out)
        n = doc["limits"]["files_truncated"] if doc else None
        chk(doc is not None and n >= 1, "REGRESSION",
            "an oversized %s (%d bytes) is counted as truncated (files_truncated=%r)"
            % (name, len(data), n))

    rc, out = run(binary, tmp, {"mounts": BASE["mounts"] + b"proc /proc proc rw 0 0\n" * 300000})
    doc, why = strict(out)
    chk(doc is not None and doc["limits"]["files_truncated"] >= 1
        and doc["limits"]["mounts_truncated"] is True, "GUARD",
        "an oversized mounts file is still counted and flagged")

    exact = BASE["mounts"] + b"#" * (SLURP_MAX - 1 - len(BASE["mounts"]))
    rc, out = run(binary, tmp, {"mounts": exact})
    doc, why = strict(out)
    n = doc["limits"]["files_truncated"] if doc else None
    chk(doc is not None and n == 0, "REGRESSION",
        "a COMPLETE file of exactly %d bytes is not counted as truncated "
        "(files_truncated=%r)" % (len(exact), n))

    rc, out = run(binary, tmp, {})
    doc, why = strict(out)
    chk(doc is not None and doc["limits"]["files_truncated"] == 0
        and doc["limits"]["mounts_truncated"] is False, "GUARD",
        "ordinary input reports no truncation")

    print("=== R3: an unreadable /proc/mounts must differ from an empty mount table ===")
    rc_missing, missing = run(binary, tmp, {"mounts": None})
    rc_empty, empty = run(binary, tmp, {"mounts": b""})
    chk((rc_missing, missing) != (rc_empty, empty), "REGRESSION",
        "a missing mounts file and an empty one are distinguishable "
        "(exit %d/%d, identical output: %s)" % (rc_missing, rc_empty, missing == empty))
    doc, why = strict(missing)
    chk(rc_missing != 0 or doc is not None, "GUARD",
        "a missing mounts file still gives a strict document on exit 0")
    doc, why = strict(empty)
    chk(rc_empty == 0 and doc is not None and doc["filesystems"] == []
        and doc["storage"]["candidates_count"] == 0, "GUARD",
        "an EMPTY mounts file still reports an empty table, not an error")


if __name__ == "__main__":
    sys.exit(main())
