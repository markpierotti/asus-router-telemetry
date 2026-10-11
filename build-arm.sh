#!/bin/sh
# Recorded toolchain build; apt packages are not pinned.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
IMAGE='gcc@sha256:f6ddfc063e12d8141189d5b5fd1afc64aedc6eb50a6b8876263f9a944fa9f8ca'
mkdir -p "$HERE/out"
docker run --rm --platform linux/amd64 -v "$HERE:/work" "$IMAGE" bash -s <<'BUILD'
set -euo pipefail
apt-get update -qq
apt-get install -y -qq gcc-arm-linux-gnueabi binutils file qemu-user-static python3
cd /work
{
    gcc --version
    arm-linux-gnueabi-gcc --version
    dpkg-query -W gcc-arm-linux-gnueabi libc6-dev-armel-cross qemu-user-static
} > out/toolchain.txt
cat out/toolchain.txt
gcc -O2 -Wall -Wextra -std=c11 -o out/router_health_native src/router_health.c
out/router_health_native --selftest
python3 tests/smoke.py out/router_health_native
arm-linux-gnueabi-gcc -static -O2 -Wall -Wextra -std=c11 -march=armv7-a -o out/router_health_arm src/router_health.c
file out/router_health_arm
readelf -h out/router_health_arm
readelf -n out/router_health_arm
if readelf -d out/router_health_arm | grep -q NEEDED; then
    echo 'FAIL: unexpected dynamic dependency' >&2
    exit 1
fi
qemu-arm-static out/router_health_arm --selftest
qemu-arm-static out/router_health_arm --pretty > out/arm-sample.json
python3 - <<'CHECK'
import json
with open('out/arm-sample.json') as stream:
    result = json.load(stream)
if result.get('schema') != 'router_health/1' or result.get('host', {}).get('machine') != 'armv7l':
    raise SystemExit('FAIL: unexpected ARM schema or architecture')
print('ARM JSON: OK')
CHECK
sha256sum src/router_health.c out/router_health_arm > out/SHA256SUMS
cat out/SHA256SUMS
BUILD
