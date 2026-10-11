#!/usr/bin/env python3
"""Host integration smoke check; Linux telemetry assertions require Linux."""
import json
import platform
import subprocess
import sys
from pathlib import Path

binary = str(Path(sys.argv[1]).resolve())
for options in ([], ["--pretty"]):
    completed = subprocess.run([binary, *options], check=True, capture_output=True, text=True, timeout=10)
    document = json.loads(completed.stdout)
    if document.get("schema") != "router_health/2":
        raise SystemExit("Unexpected schema")
    if document.get("host", {}).get("sysname") != platform.system():
        raise SystemExit("Host identity does not match the executing system")
    for key in ("cpu", "loadavg", "memory_kb", "cpu_stat", "filesystems", "storage"):
        if key not in document:
            raise SystemExit(f"Missing section: {key}")
    if not isinstance(document["filesystems"], list) or not isinstance(document["storage"].get("candidates"), list):
        raise SystemExit("Storage sections must be arrays")
    if platform.system() == "Linux":
        if document["memory_kb"].get("MemTotal", 0) <= 0:
            raise SystemExit("Linux memory collection failed")
        if not document["filesystems"]:
            raise SystemExit("Linux filesystem collection failed")
print("JSON smoke checks: OK (compact and pretty)")
if platform.system() != "Linux":
    print("Linux telemetry integration was not tested on this host")
