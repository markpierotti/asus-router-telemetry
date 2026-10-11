#!/bin/bash
# smoke_loop.sh — verify the foreground loop starts, collects, and stops cleanly
# on SIGINT. Uses the mock transport only; contacts nothing.
set -u
set -m          # job control: so the backgrounded loop does NOT ignore SIGINT
cd "$(dirname "$0")/.."
D=$(mktemp -d)
python3 collector.py collect --loop --interval 1 --transport mock --data-dir "$D" \
    > "$D/loop.out" 2>&1 &
LOOP=$!
sleep 3.5
kill -INT "$LOOP"
wait "$LOOP"; rc=$?
echo "loop exit=$rc (expect 130)"
echo "--- loop output (tail) ---"
tail -8 "$D/loop.out"
echo "--- samples stored ---"
ls "$D/samples" | wc -l
echo "--- status ---"
python3 collector.py status --transport mock --data-dir "$D"
echo "--- lock released? second run should succeed ---"
python3 collector.py collect --once --transport mock --data-dir "$D" >/dev/null 2>&1
echo "second run exit=$?"
rm -rf "$D"
