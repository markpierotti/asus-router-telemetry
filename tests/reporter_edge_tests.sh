#!/bin/bash
# reporter_edge_tests.sh <binary> — regression tests for the reporter's own
# output-integrity defects, against synthetic /proc trees.
#
#   * CPU serialisation truncation must still emit VALID JSON, with the
#     truncation represented explicitly (cpu_stat._truncated) and no partial
#     text left in the document;
#   * a mountpoint containing an invalid UTF-8 byte must not produce invalid
#     UTF-8/JSON (the byte is replaced with U+FFFD), while a valid non-ASCII
#     name is preserved.
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

python3 - "$TMP/trunc.json" "$TMP/utf.json" <<'PY'
import json, sys
trunc = open(sys.argv[1], "rb").read()
utf = open(sys.argv[2], "rb").read()
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

print()
print("reporter edge failures: %d" % len(fails))
sys.exit(1 if fails else 0)
PY
