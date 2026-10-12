"""Validation of the reporter's JSON document.

Two distinct failures are kept apart, because they mean different things
(requirement 6):

    parse()     bytes -> dict     raises MalformedData   on a containment or syntax failure
    validate()  dict  -> None     raises SchemaInvalid   on a wrong schema/key/type

parse() enforces CONTAINMENT before the document is decoded or parsed (finding:
unbounded / deeply nested device output):

  * a byte ceiling, checked before UTF-8 decoding and JSON parsing;
  * a nesting budget, scanned over the raw text in a string- and escape-aware way,
    so a small but deeply nested document cannot reach the interpreter recursion
    limit; and
  * rejection of the non-standard JSON constants NaN / Infinity / -Infinity, and
    conversion of Python's RecursionError into MalformedData, so a bad response is
    recorded as a failed attempt and the loop can accept the next good sample.

validate() is deliberately strict about the fields the collector consumes and
permissive about extra fields, so the reporter can grow without breaking the
collector. Numeric fields must be FINITE and, where a range is defined, within it;
booleans are never accepted where a number is required, and legitimate null /
unavailable measurements are preserved.
"""

import json
import math

from .errors import MalformedData, SchemaInvalid

SCHEMA = "router_health/2"

# Containment budgets. parse() also takes explicit per-call overrides.
MAX_DOC_BYTES = 1024 * 1024     # 1 MiB: far above any legitimate reporter document
MAX_NESTING = 64                # the real document nests about five levels deep

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
MEM_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree")


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_finite_num(x):
    return _is_num(x) and math.isfinite(x)


def _is_nonneg_num(x):
    return _is_finite_num(x) and x >= 0


def _is_nonneg_int(x):
    return _is_int(x) and x >= 0


def _fail(path, msg):
    raise SchemaInvalid("%s: %s" % (path, msg))


def _req(doc, key, path, kind, kinds):
    if key not in doc:
        _fail("%s.%s" % (path, key), "missing required field")
    v = doc[key]
    if not isinstance(v, kind):
        _fail("%s.%s" % (path, key), "expected %s, got %r" % (kinds, type(v).__name__))
    return v


def _opt_nullable(doc, key, path, pred, what):
    if key in doc and doc[key] is not None and not pred(doc[key]):
        _fail("%s.%s" % (path, key), "expected %s or null, got %r" % (what, doc[key]))
    return doc.get(key)


def _reject_constant(name):
    """json.loads parse_constant hook: refuse NaN / Infinity / -Infinity."""
    raise ValueError("non-standard JSON constant %r is not allowed" % name)


def _check_nesting(text, max_depth):
    """Reject text whose brace/bracket nesting exceeds ``max_depth``.

    A cheap, string- and escape-aware scan of the raw text: braces inside string
    literals (or after an escaped quote) do not count, so a legitimate path or note
    cannot inflate the depth. Runs BEFORE json.loads, so a small but deeply nested
    document is refused instead of reaching the interpreter recursion limit.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > max_depth:
                raise MalformedData("JSON nesting exceeds the %d-level budget" % max_depth)
        elif ch in "}]":
            depth -= 1
    return depth


def parse(raw, *, max_bytes=MAX_DOC_BYTES, max_nesting=MAX_NESTING):
    """bytes/str -> dict, raising MalformedData on a containment or syntax failure."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            raise MalformedData(
                "response is %d bytes, over the %d-byte limit" % (len(raw), max_bytes))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise MalformedData("stdout is not valid UTF-8: %s" % e)
    else:
        text = raw
        if len(text) > max_bytes:
            raise MalformedData(
                "response is %d characters, over the %d-byte limit" % (len(text), max_bytes))
    _check_nesting(text, max_nesting)
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except RecursionError as e:
        raise MalformedData("JSON is nested too deeply to decode: %s" % e)
    except ValueError as e:
        raise MalformedData("not valid JSON: %s" % e)
    if not isinstance(doc, dict):
        raise MalformedData("top-level JSON value is %s, expected an object" % type(doc).__name__)
    return doc


def validate(doc):
    """Validate a parsed reporter document. Raises SchemaInvalid; returns doc."""
    if doc.get("schema") != SCHEMA:
        _fail("schema", "expected %r, got %r" % (SCHEMA, doc.get("schema")))

    _req(doc, "tool", "", str, "string")
    _req(doc, "version", "", str, "string")
    _req(doc, "units", "", dict, "object")

    host = _req(doc, "host", "", dict, "object")
    _req(host, "sysname", "host", str, "string")
    _req(host, "release", "host", str, "string")
    _req(host, "machine", "host", str, "string")
    _opt_nullable(host, "uptime_seconds", "host", _is_nonneg_num, "finite non-negative number")

    cpu = _req(doc, "cpu", "", dict, "object")
    if "count" not in cpu:
        _fail("cpu.count", "missing required field")
    if not _is_nonneg_int(cpu["count"]):
        _fail("cpu.count", "expected a non-negative integer, got %r" % (cpu["count"],))

    la = _req(doc, "loadavg", "", dict, "object")
    for k in ("load1", "load5", "load15"):
        if k not in la:
            _fail("loadavg.%s" % k, "missing required field")
        if not _is_nonneg_num(la[k]):
            _fail("loadavg.%s" % k,
                  "expected a finite non-negative number, got %r" % (la[k],))
    for k in ("procs_running", "procs_total"):
        if k not in la:
            _fail("loadavg.%s" % k, "missing required field")
        if not _is_nonneg_int(la[k]):
            _fail("loadavg.%s" % k,
                  "expected a non-negative integer, got %r" % (la[k],))

    mem = _req(doc, "memory_kb", "", dict, "object")
    for k in MEM_KEYS:
        if k not in mem:
            _fail("memory_kb.%s" % k, "missing required field")
        if mem[k] is not None and not _is_nonneg_int(mem[k]):
            _fail("memory_kb.%s" % k,
                  "expected a non-negative integer kB or null, got %r" % (mem[k],))

    st = doc.get("cpu_stat")
    if st is not None:
        if not isinstance(st, dict):
            _fail("cpu_stat", "expected object or null")
        truncated = st.get("_truncated")
        if "_truncated" in st and truncated is not True:
            _fail("cpu_stat._truncated", "must be boolean true when present")
        names = [k for k in st if k != "_truncated"]
        if not truncated and "cpu" not in names:
            _fail("cpu_stat", "missing aggregate 'cpu' entry")
        for name in names:
            vals = st[name]
            if not isinstance(vals, dict):
                _fail("cpu_stat.%s" % name, "expected object")
            for f in CPU_FIELDS:
                if f not in vals:
                    _fail("cpu_stat.%s.%s" % (name, f), "missing required counter")
                if not _is_nonneg_int(vals[f]):
                    _fail("cpu_stat.%s.%s" % (name, f),
                          "expected a non-negative integer of jiffies, got %r" % (vals[f],))

    fss = _req(doc, "filesystems", "", list, "array")
    for i, f in enumerate(fss):
        p = "filesystems[%d]" % i
        if not isinstance(f, dict):
            _fail(p, "expected object")
        _req(f, "path", p, str, "string")
        _req(f, "available", p, bool, "boolean")
        for k in ("total_bytes", "free_bytes", "avail_bytes", "used_bytes"):
            _opt_nullable(f, k, p, _is_nonneg_int, "non-negative integer bytes")
        _opt_nullable(f, "used_percent", p, _is_nonneg_int, "non-negative integer")
        if f.get("used_percent") is not None and f["used_percent"] > 100:
            _fail("%s.used_percent" % p, "must be within 0..100, got %r" % (f["used_percent"],))
        _opt_nullable(f, "inodes", p, _is_nonneg_int, "non-negative integer")
        _opt_nullable(f, "inodes_free", p, _is_nonneg_int, "non-negative integer")

    storage = _req(doc, "storage", "", dict, "object")
    cands = _req(storage, "candidates", "storage", list, "array")
    for i, c in enumerate(cands):
        p = "storage.candidates[%d]" % i
        if not isinstance(c, dict):
            _fail(p, "expected object")
        _req(c, "device", p, str, "string")
        _req(c, "mountpoint", p, str, "string")
        _req(c, "fstype", p, str, "string")
    for k in ("candidates_count", "placeholders_excluded", "duplicate_records_dropped"):
        if k not in storage:
            _fail("storage.%s" % k, "missing required field")
        if not _is_nonneg_int(storage[k]):
            _fail("storage.%s" % k,
                  "expected a non-negative integer, got %r" % (storage[k],))

    return doc


def parse_and_validate(raw):
    doc = parse(raw)
    validate(doc)
    return doc
