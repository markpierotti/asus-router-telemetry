#!/bin/bash
# reporter_edge_tests.sh <binary> — regression tests for the reporter's own
# output-integrity defects, against synthetic /proc trees.
#
#   * CPU serialisation truncation must still emit VALID JSON, with the
#     truncation represented explicitly (cpu_stat._truncated) and no partial
#     text left in the document;
#   * a mountpoint containing an invalid UTF-8 byte must not produce invalid
#     UTF-8/JSON (the byte is replaced with U+FFFD), while a valid non-ASCII
#     name is preserved;
#   * an OVERLONG /proc/mounts field must be SKIPPED AND COUNTED, never silently
#     truncated (truncation shifted every later field and pointed statvfs at a
#     mangled path);
#   * device text (BogoMIPS) that contains a quote, a backslash or an invalid
#     byte must still produce valid JSON, escaped;
#   * an octal escape that is not a single byte (\000, \777) must be left literal
#     instead of embedding a NUL or wrapping;
#   * host.boot_time must be emitted from /proc/stat `btime`, and null when the
#     line is absent;
#   * the "limits" block must report every hard bound and what it did;
#   * a stdout write failure must exit NON-ZERO.
#
# Read-only; touches only a local mktemp tree. No router contact.
set -u

BIN="$1"
[ -x "$BIN" ] || { echo "usage: reporter_edge_tests.sh <binary>"; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
P="$TMP/proc"; mkdir -p "$P"

printf '5342.20 12345.67\n' > "$P/uptime"
printf '2.01 1.87 1.83 2/146 9999\n' > "$P/loadavg"
printf 'processor : 0\nmodel name : ARMv7\nBogoMIPS : 100.00\n' > "$P/cpuinfo"
printf 'MemTotal: 510456 kB\nMemFree: 303472 kB\nMemAvailable: 233476 kB\n' > "$P/meminfo"
: > "$P/mounts"

echo "=== edge 1: 1000-CPU cpu_stat exceeds the serialisation bound ==="
{ printf 'cpu 1 2 3 4 5 6 7 8\n'; for i in $(seq 0 999); do printf 'cpu%d 1 2 3 4 5 6 7 8\n' "$i"; done; } > "$P/stat"
"$BIN" --proc-root "$P" > "$TMP/trunc.json" 2> "$TMP/trunc.err"; TRC=$?
echo "binary exit: $TRC"
[ "$TRC" = 0 ] || { echo "FAIL: exit $TRC"; cat "$TMP/trunc.err"; exit 1; }

echo "=== edge 2: invalid byte and valid non-ASCII in a mountpoint ==="
printf 'cpu 1 2 3 4 5 6 7 8\n' > "$P/stat"
printf '/dev/sda1 /tmp/mnt/Bad\377Disk ext4 rw 0 0\n/dev/sda2 /tmp/mnt/Good\303\251Disk ext4 rw 0 0\n' > "$P/mounts"
"$BIN" --proc-root "$P" > "$TMP/utf.json" 2> "$TMP/utf.err"; URC=$?
echo "binary exit: $URC"
[ "$URC" = 0 ] || { echo "FAIL: exit $URC"; cat "$TMP/utf.err"; exit 1; }

echo "=== edge 3: overlong mount field, hostile cpuinfo, literal octal escapes, btime ==="
# A device field far longer than its buffer, then two normal mounts that must still
# parse correctly (the old code shifted fields by one). BogoMIPS carries a quote, a
# backslash and an invalid byte. Two paths carry octal escapes that are not bytes.
python3 - "$P" <<'PY'
import os, sys
p = sys.argv[1]
with open(os.path.join(p, "mounts"), "w") as fh:
    fh.write("/dev/" + "L" * 400 + " /tmp/mnt/Big vfat rw 0 0\n")
    fh.write("/dev/sda1 /tmp/mnt/USB vfat rw,relatime 0 0\n")
    fh.write("/dev/sda2 /tmp/mnt/a\\000b ext4 rw 0 0\n")
    fh.write("/dev/sda3 /tmp/mnt/a\\777b ext4 rw 0 0\n")
with open(os.path.join(p, "cpuinfo"), "wb") as fh:
    fh.write(b"processor : 0\nmodel name : ARMv7\nBogoMIPS : 1\"2\\3\xff4\n")
with open(os.path.join(p, "stat"), "w") as fh:
    fh.write("cpu 1 2 3 4 5 6 7 8\ncpu0 \"x 1 2 3 4 5 6 7 8\nbtime 1700000000\n")
PY
"$BIN" --proc-root "$P" > "$TMP/hostile.json" 2> "$TMP/hostile.err"; HRC=$?
echo "binary exit: $HRC"
[ "$HRC" = 0 ] || { echo "FAIL: exit $HRC"; cat "$TMP/hostile.err"; exit 1; }

echo "=== edge 4: /proc/stat with no btime line (boot identity unavailable) ==="
printf 'cpu 1 2 3 4 5 6 7 8\n' > "$P/stat"
"$BIN" --proc-root "$P" > "$TMP/nobtime.json" 2>/dev/null; NRC=$?
echo "binary exit: $NRC"
[ "$NRC" = 0 ] || { echo "FAIL: exit $NRC"; exit 1; }

echo "=== edge 5: stdout write failure must exit non-zero ==="
python3 - "$BIN" "$P" <<'PY'
import os, subprocess, sys
binary, procroot = sys.argv[1], sys.argv[2]
r, w = os.pipe()
os.close(r)                       # nobody will ever read: writes get EPIPE
p = subprocess.run([binary, "--proc-root", procroot], stdout=w, stderr=subprocess.DEVNULL)
os.close(w)
print("write-failure exit:", p.returncode)
if p.returncode == 0:
    raise SystemExit("FAIL: a failed stdout write still exited 0")
PY
WRC=$?
[ "$WRC" = 0 ] || { echo "FAIL: stdout write-failure handling"; exit 1; }

python3 - "$TMP/trunc.json" "$TMP/utf.json" "$TMP/hostile.json" "$TMP/nobtime.json" <<'PY'
import json, sys
trunc = open(sys.argv[1], "rb").read()
utf = open(sys.argv[2], "rb").read()
hostile_raw = open(sys.argv[3], "rb").read()
nobtime_raw = open(sys.argv[4], "rb").read()
fails = []
def chk(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c: fails.append(m)

# --- CPU truncation ---
try:
    t = json.loads(trunc)
    chk(True, "truncated cpu_stat output is valid JSON (%d bytes)" % len(trunc))
except ValueError as e:
    chk(False, "truncated cpu_stat output is MALFORMED JSON: %s" % e)
    t = None
if t is not None:
    st = t.get("cpu_stat")
    chk(isinstance(st, dict), "cpu_stat is an object")
    chk(st.get("_truncated") is True, "truncation is represented explicitly (cpu_stat._truncated is true)")
    chk("cpu" in st, "the aggregate 'cpu' entry survives truncation")
    counters = {k: v for k, v in st.items() if k != "_truncated"}
    chk(all(isinstance(v, dict) and "idle" in v for v in counters.values()),
        "every emitted counter entry is complete (no partial text)")
    chk(len(counters) < 1001, "not every per-CPU entry was emitted (bound was reached)")

# --- UTF-8 / JSON encoding ---
try:
    utf.decode("utf-8")
    chk(True, "mountpoint output is valid UTF-8")
except UnicodeDecodeError as e:
    chk(False, "mountpoint output is INVALID UTF-8: %s" % e)
try:
    u = json.loads(utf)
    chk(True, "mountpoint output is valid JSON")
except ValueError as e:
    chk(False, "mountpoint output is INVALID JSON: %s" % e)
    u = None
if u is not None:
    paths = [f["path"] for f in u["filesystems"]] + [c["mountpoint"] for c in u["storage"]["candidates"]]
    chk(any("\ufffd" in p for p in paths),
        "the invalid byte was replaced with U+FFFD (replacement policy)")
    chk(any("Good\u00e9Disk" in p for p in paths),
        "the valid non-ASCII name (e-acute) was preserved")
    chk(not any("\udcff" in p for p in paths), "no lone surrogates were emitted")

# --- overlong mount field, hostile cpuinfo, literal octal escapes ---
try:
    hostile_raw.decode("utf-8")
    chk(True, "hostile-input output is valid UTF-8")
except UnicodeDecodeError as e:
    chk(False, "hostile-input output is INVALID UTF-8: %s" % e)
try:
    h = json.loads(hostile_raw)
    chk(True, "hostile-input output is valid JSON")
except ValueError as e:
    chk(False, "hostile-input output is INVALID JSON: %s" % e)
    h = None
if h is not None:
    lim = h.get("limits") or {}
    chk(lim.get("mounts_overlong") == 1,
        "the overlong mount record was SKIPPED AND COUNTED (%r)" % lim.get("mounts_overlong"))
    chk(lim.get("mounts_seen") == 4, "every mount line was accounted for (%r)" % lim.get("mounts_seen"))
    chk(lim.get("mounts_max") == 512 and lim.get("file_read_max_bytes") == 4194304,
        "the limits block names the hard bounds")
    chk(lim.get("mounts_truncated") is False and lim.get("files_truncated") == 0,
        "no truncation was reported for this input")
    mp = [f["path"] for f in h["filesystems"]]
    chk("/tmp/mnt/USB" in mp and "/tmp/mnt/Big" not in mp,
        "mounts AFTER the overlong record still parsed correctly (no field shift): %r" % mp)
    chk("a\\000b" in " ".join(mp), "a \\000 escape was left LITERAL, not turned into a NUL")
    chk("a\\777b" in " ".join(mp), "a \\777 escape was left LITERAL, not wrapped to a byte")
    bogo = h["cpu"]["bogomips"]
    chk('"' in bogo and "\\" in bogo, "BogoMIPS was escaped, not emitted raw: %r" % bogo)
    chk("\ufffd" in bogo, "the invalid byte in BogoMIPS was replaced: %r" % bogo)
    chk(h["host"].get("boot_time") == 1700000000,
        "host.boot_time came from /proc/stat btime (%r)" % h["host"].get("boot_time"))
    chk(all(isinstance(v, dict) and "idle" in v
            for k, v in (h.get("cpu_stat") or {}).items() if k != "_truncated"),
        "the malformed cpu0 stat line was skipped without breaking the block")

# --- boot identity unavailable ---
try:
    nb = json.loads(nobtime_raw)
    chk(nb["host"].get("boot_time") is None,
        "boot_time is null when /proc/stat has no btime line")
except ValueError as e:
    chk(False, "no-btime output is INVALID JSON: %s" % e)

print()
print("reporter edge failures: %d" % len(fails))
sys.exit(1 if fails else 0)
PY
