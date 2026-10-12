# Test fixtures

## `live_sample.json`

A real capture from the physical RT-AX56U (reporter **v1.0.3**, schema
`router_health/2`) taken on 2026-10-12. It predates the explicit boot identity, so
it has **no** `host.boot_time` — deliberately kept byte-for-byte so the collector is
still exercised against a document from the hardware-accepted reporter.

## `snap_sample.json`

A second real capture from the same session, used by the CPU-derivation tests.

## `null_sections.json`

Generated from the REAL reporter binary (**v1.0.4**) with `--proc-root` pointed at a
synthetic `/proc` in which `/proc/cpuinfo` and `/proc/loadavg` are **unreadable**.
The reporter then emits `"cpu": null` and `"loadavg": null`, which is the exact
document the collector previously **rejected** (one unreadable file discarded the
whole sample). It also carries `host.boot_time` and the `limits` block.

Reproduce it from the repository root:

```sh
cc -O2 -Wall -Wextra -std=c11 -o /tmp/router_health_native src/router_health.c
P=$(mktemp -d)/proc && mkdir -p "$P"
printf '900.00 900.00\n'                                                 > "$P/uptime"
printf 'MemTotal: 510456 kB\nMemFree: 303060 kB\nMemAvailable: 233476 kB\n' > "$P/meminfo"
printf 'cpu 1 2 3 4 5 6 7 8\ncpu0 1 2 3 4 5 6 7 8\nbtime 1700000000\n'    > "$P/stat"
printf '/dev/root / ext4 ro 0 0\n'                                      > "$P/mounts"
for f in cpuinfo loadavg; do printf 'processor : 0\n' > "$P/$f"; chmod 000 "$P/$f"; done
/tmp/router_health_native --proc-root "$P" | python3 -m json.tool > collector/tests/fixtures/null_sections.json
```

(The absolute value of `boot_time`, `MemFree` and the uptime are synthetic; only the
shape and the null sections are the point. The file is regenerated rather than
hand-edited so it can never drift from what the reporter actually emits.)
