#!/bin/bash
# demo_offline.sh — exercise every offline scenario and show the resulting state.
# Uses the MOCK transport only; contacts nothing.
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "############################################################"
echo "# 1. single-attempt failure classification (one CLI run each)"
echo "############################################################"
D=$(mktemp -d)
for pair in "malformed:malformed JSON" "wrong_schema:wrong schema tag" \
            "wrong_type:right schema, wrong field type" "timeout:reporter watchdog timeout" \
            "auth:ssh authentication refused" "unreachable:management address unreachable" \
            "hostkey:pinned host key mismatch" "reporter_failure:reporter exited non-zero"; do
  sc="${pair%%:*}"; label="${pair#*:}"
  printf '\n### %s\n' "$label"
  python3 collector.py collect --once --transport mock --mock-scenario "$sc" --data-dir "$D"
  printf 'exit=%s\n' "$?"
done
rm -rf "$D"

echo
echo "############################################################"
echo "# 2. multi-sample transitions (one process each, in-process)"
echo "############################################################"
python3 - "$ROOT" <<'PY'
import sys, tempfile, shutil
sys.path.insert(0, sys.argv[1])
from rtax_telemetry.mock import ScenarioTransport
from rtax_telemetry.core import Collector
from rtax_telemetry.store import Store

def pair(scenario, n=2, **kw):
    d = tempfile.mkdtemp(prefix="rtax-demo.")
    try:
        store = Store(d)
        c = Collector(ScenarioTransport(scenario, log=lambda *a: None), store,
                      log=lambda *a: None, **kw)
        states = []
        for _ in range(n):
            c.collect_once()
            states.append(store.last_sample()["derived"]["cpu_utilisation"]["state"])
        u = store.last_sample()["derived"]["cpu_utilisation"]
        agg = (u.get("aggregate") or {}).get("util_percent")
        return states, agg
    finally:
        shutil.rmtree(d, ignore_errors=True)

st, agg = pair("success")
print("success    : sample1 state=%s  sample2 state=%s  aggregate util=%s%%" % (st[0], st[1], agg))
for sc in ("reset", "reboot", "zero_elapsed"):
    st, agg = pair(sc)
    print("%-10s: sample1 state=%s  sample2 state=%s  aggregate util=%s%%" % (sc, st[0], st[1], agg))
PY

echo
echo "############################################################"
echo "# 3. incident open (3 failures) then recovery (2 successes)"
echo "############################################################"
python3 - "$ROOT" <<'PY'
import sys, tempfile, shutil
sys.path.insert(0, sys.argv[1])
from rtax_telemetry.mock import ScenarioTransport
from rtax_telemetry.core import Collector
from rtax_telemetry.store import Store

d = tempfile.mkdtemp(prefix="rtax-demo.")
try:
    store = Store(d); store.ensure()
    tr = ScenarioTransport("timeout", log=lambda *a: None)
    c = Collector(tr, store, log=lambda *a: None)
    def show(tag):
        st = store.load_state()
        inc = st["incident"]
        print("%-26s failures=%d successes=%d incident=%s" % (
            tag, st["consecutive_failures"], st["consecutive_successes"],
            ("OPEN(" + inc["reason_outcome"] + ")") if inc else "none"))
    for i in range(3):
        c.collect_once(); show("after failure %d" % (i + 1))
    tr.scenario = "success"
    c.collect_once(); show("after success 1")
    c.collect_once(); show("after success 2")
finally:
    shutil.rmtree(d, ignore_errors=True)
PY

echo
echo "############################################################"
echo "# 4. final status rendered from a real (mock) run"
echo "############################################################"
D=$(mktemp -d)
for _ in 1 2 3; do
  python3 collector.py collect --once --transport mock --mock-scenario unreachable --data-dir "$D" >/dev/null 2>&1
done
python3 collector.py status --transport mock --data-dir "$D"
rm -rf "$D"
