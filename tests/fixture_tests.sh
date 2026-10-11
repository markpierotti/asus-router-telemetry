#!/bin/bash
# fixture_tests.sh <binary>  — run router_health against synthetic /proc trees.
# Exercises: virtual placeholders, duplicate mounts, a block-backed storage mount,
# escaped spaces, and unavailable (not zero) memory fields. Read-only; touches only
# a local mktemp tree. No router contact, no writes outside the temp tree.
set -u

BIN="$1"
[ -x "$BIN" ] || { echo "usage: fixture_tests.sh <binary>"; exit 2; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
P="$TMP/proc"; mkdir -p "$P"
mkdir -p "$TMP/store/usbdisk" "$TMP/store/My Disk"

# --- synthetic /proc ---
cat > "$P/meminfo" <<'EOF'
MemTotal:         510456 kB
MemFree:          303472 kB
MemAvailable:     233476 kB
Buffers:               0 kB
Cached:            14988 kB
EOF

cat > "$P/stat" <<'EOF'
cpu  100 0 200 3000 0 0 5 0 0 0
cpu0 25 0 50 750 0 0 1 0 0 0
cpu1 25 0 50 750 0 0 1 0 0 0
intr 12345 0 0
EOF

printf '2.01 1.87 1.83 2/146 9999\n'  > "$P/loadavg"
printf '5342.20 12345.67\n'          > "$P/uptime"
cat > "$P/cpuinfo" <<'EOF'
processor	: 0
model name	: ARMv7 Processor rev 5 (v7l)
BogoMIPS	: 100.00
Hardware	: Generic DT based system
EOF

# --- fixture A: placeholders + duplicates + block-backed + escaped space ---
# NOTE: "\\040" must survive into the file as the literal \040 escape.
cat > "$P/mounts" <<EOF
proc /proc proc rw,relatime 0 0
sysfs /sys sysfs rw,relatime 0 0
tmpfs /var tmpfs rw,relatime 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
tmpfs /tmp tmpfs rw,relatime 0 0
/dev/sda1 $TMP/store/usbdisk vfat rw,relatime 0 0
/dev/sda1 $TMP/store/usbdisk vfat rw,relatime 0 0
/dev/sda2 $TMP/store/My\\040Disk ext4 rw,relatime 0 0
/dev/root / ubifs ro,relatime 0 0
EOF

# --- fixture B: placeholders only, no disk attached (the pre-attachment case) ---
mkdir -p "$TMP/procB"
cp "$P/meminfo" "$P/stat" "$P/loadavg" "$P/uptime" "$P/cpuinfo" "$TMP/procB/"
cat > "$TMP/procB/mounts" <<'EOF'
proc /proc proc rw,relatime 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
/dev/root / ubifs ro,relatime 0 0
EOF

# --- fixture C: the REAL /proc/mounts captured from the RT-AX56U (regression) ---
mkdir -p "$TMP/procC"
cp "$P/meminfo" "$P/stat" "$P/loadavg" "$P/uptime" "$P/cpuinfo" "$TMP/procC/"
cat > "$TMP/procC/mounts" <<'EOF'
ubi:rootfs_ubifs / ubifs ro,relatime 0 0
devtmpfs /dev devtmpfs rw,relatime,size=255124k,nr_inodes=63781,mode=755 0 0
proc /proc proc rw,relatime 0 0
tmpfs /var tmpfs rw,relatime 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
sysfs /sys sysfs rw,relatime 0 0
debugfs /sys/kernel/debug debugfs rw,relatime 0 0
devpts /dev/pts devpts rw,relatime,mode=600 0 0
ubi1:data /data ubifs rw,relatime 0 0
tmpfs /tmp/mnt tmpfs rw,relatime,size=16k,mode=755 0 0
tmpfs /tmp tmpfs rw,relatime 0 0
/dev/mtdblock9 /jffs jffs2 rw,noatime 0 0
EOF

# --- fixtures D/E/F: uptime — genuine zero vs unavailable vs malformed ---
for v in D E F; do mkdir -p "$TMP/proc$v"; cp "$P/mounts" "$P/meminfo" "$P/stat" "$P/loadavg" "$P/cpuinfo" "$TMP/proc$v/"; done
printf '0.00 0.00\n'  > "$TMP/procD/uptime"     # genuine zero
: > "$TMP/procE/uptime.missing"                 # (no uptime file at all)
printf 'garbage\n'    > "$TMP/procF/uptime"     # malformed
rm -f "$TMP/procE/uptime"

echo "=== fixture A: placeholders + duplicates + block-backed + escaped space ==="
"$BIN" --pretty --proc-root "$P" > "$TMP/a.json" 2>"$TMP/a.err"; ARC=$?
echo "binary exit: $ARC"
[ "$ARC" = 0 ] || { echo "FAIL: exit $ARC"; cat "$TMP/a.err"; exit 1; }

echo "=== fixture B: placeholders only (no disk) ==="
"$BIN" --pretty --proc-root "$TMP/procB" > "$TMP/b.json"; BRC=$?
echo "binary exit: $BRC"

echo "=== fixture C: the router's REAL /proc/mounts (regression) ==="
"$BIN" --pretty --proc-root "$TMP/procC" > "$TMP/c.json"; CRC=$?
echo "binary exit: $CRC"

echo "=== fixtures D/E/F: uptime zero / missing / malformed ==="
for v in D E F; do "$BIN" --proc-root "$TMP/proc$v" > "$TMP/$v.json"; echo "  fixture $v exit: $?"; done

python3 - "$TMP/a.json" "$TMP/b.json" "$TMP/store/My Disk" "$TMP/c.json" "$TMP/D.json" "$TMP/E.json" "$TMP/F.json" <<'PY'
import json, sys
A = json.load(open(sys.argv[1])); B = json.load(open(sys.argv[2])); mydisk = sys.argv[3]
fails = []
def chk(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c: fails.append(m)

chk(A.get("schema") == "router_health/2", "schema is router_health/2")
chk(isinstance(A.get("units"), dict) and "memory_kb" in A["units"], "units block present")

st = A["storage"]
cand = st["candidates"]
mps = [c["mountpoint"] for c in cand]
chk(len(cand) == 2, "fixture A: exactly 2 storage candidates (got %d: %s)" % (len(cand), mps))
chk(all(c["fstype"] != "tmpfs" for c in cand), "fixture A: no tmpfs placeholder among candidates")
chk(mydisk in mps, "fixture A: escaped-space mount appears with a literal space")
chk(len(set((c["device"], c["mountpoint"], c["fstype"]) for c in cand)) == len(cand),
    "fixture A: no duplicate candidate records")
chk(st.get("placeholders_excluded") == 1, "fixture A: 1 placeholder excluded (got %r)" % st.get("placeholders_excluded"))
chk(st.get("duplicate_records_dropped") == 2, "fixture A: 2 duplicate records dropped (got %r)" % st.get("duplicate_records_dropped"))
md = [c for c in cand if c["mountpoint"] == mydisk]
chk(md and md[0]["capacity"].get("available") is True,
    "fixture A: capacity resolved for the space-containing path (unescape reached statvfs)")
chk(md and md[0]["capacity"].get("total_bytes", 0) > 0, "fixture A: space-path total_bytes > 0")
chk(all(c.get("usb_identity") == "not_established" for c in cand),
    "fixture A: USB identity explicitly not established")

paths = [f["path"] for f in A["filesystems"]]
chk(len(paths) == len(set(paths)), "fixture A: no duplicate filesystem paths (%s)" % paths)
chk(all(f.get("available") is True for f in A["filesystems"]), "fixture A: filesystems all available")

mem = A["memory_kb"]
chk(mem.get("MemTotal") == 510456, "memory_kb.MemTotal parsed")
chk(mem.get("SwapTotal") is None, "memory_kb.SwapTotal is null (unavailable, NOT zero)")
chk(mem.get("SwapFree") is None, "memory_kb.SwapFree is null (unavailable, NOT zero)")
chk(mem.get("Buffers") == 0, "memory_kb.Buffers is a real 0 (present in meminfo)")

stB = B["storage"]
chk(len(stB["candidates"]) == 0, "fixture B: no candidates when no disk is attached")
chk(stB.get("placeholders_excluded") == 1, "fixture B: placeholder counted, not reported as storage")

C = json.load(open(sys.argv[4]))
stC = C["storage"]
chk(len(stC["candidates"]) == 0,
    "fixture C (real router mounts): NO storage candidates when no disk attached (was 2 bogus tmpfs entries)")
chk(stC.get("placeholders_excluded") == 1,
    "fixture C: tmpfs placeholder excluded (got %r)" % stC.get("placeholders_excluded"))
chk(stC.get("duplicate_records_dropped") == 1,
    "fixture C: duplicated /tmp/mnt record dropped (got %r)" % stC.get("duplicate_records_dropped"))
pathsC = [f["path"] for f in C["filesystems"]]
chk(len(pathsC) == len(set(pathsC)), "fixture C: no duplicate filesystem paths (%s)" % pathsC)
chk("/tmp/mnt" not in pathsC, "fixture C: tmpfs /tmp/mnt absent from filesystems (%s)" % pathsC)
chk(C["memory_kb"].get("MemAvailable") == 233476, "fixture C: MemAvailable from the real meminfo")

# uptime: genuine zero preserved, unavailable/malformed -> null
D = json.load(open(sys.argv[5])); E = json.load(open(sys.argv[6])); F = json.load(open(sys.argv[7]))
dU = D["host"].get("uptime_seconds"); eU = E["host"].get("uptime_seconds"); fU = F["host"].get("uptime_seconds")
chk(dU == 0.0, "uptime: genuine zero (0.00) is reported as 0.0, not null (got %r)" % (dU,))
chk(isinstance(eU, type(None)), "uptime: MISSING file is null, not 0 (got %r)" % (eU,))
chk(isinstance(fU, type(None)), "uptime: MALFORMED file is null, not 0 (got %r)" % (fU,))
chk(not (isinstance(eU, (int, float)) and eU == 0), "uptime: unavailable is not silently 0")

print()
print("fixture failures: %d" % len(fails))
sys.exit(1 if fails else 0)
PY
