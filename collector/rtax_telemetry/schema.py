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
  * rejection of EVERY non-finite number, however it is expressed: the literal
    constants NaN / Infinity / -Infinity (the ``parse_constant`` hook) AND a
    numeric literal that overflows to a non-finite double such as ``1e999`` (the
    ``parse_float`` hook). ``json`` only routes the LITERAL tokens through
    ``parse_constant``; an overflowing exponent is produced by the number parser,
    so without the second hook a non-finite value could enter an extra/unknown
    field and later be re-emitted as the non-standard token ``Infinity``.
  * conversion of Python's RecursionError into MalformedData, so a bad response is
    recorded as a failed attempt and the loop can accept the next good sample.

validate() is deliberately strict about the fields the collector consumes and
permissive about extra fields, so the reporter can grow without breaking the
collector. Numeric fields must be FINITE and, where a range is defined, within it;
booleans are never accepted where a number is required, and legitimate null /
unavailable measurements are preserved.

Numeric limits (see the constants below) are derived from the reporter's actual
field types, so anything the reporter CANNOT emit is rejected as schema_invalid
through the ordinary classifier — never by letting a numeric check raise. The
predicates are safe for ARBITRARY JSON integers: ``math.isfinite`` converts its
argument to a C double and raises ``OverflowError`` on an int such as ``10**400``
(which JSON can express as a plain integer), so only floats are passed to it.
"""

import json
import math
import reprlib

from .errors import MalformedData, SchemaInvalid

SCHEMA = "router_health/2"

# Containment budgets. parse() also takes explicit per-call overrides.
MAX_DOC_BYTES = 1024 * 1024     # 1 MiB: far above any legitimate reporter document
MAX_NESTING = 64                # the real document nests about five levels deep

# Every value or attacker-controlled KEY NAME that reaches a diagnostic is
# rendered through this bound. A device can put a megabyte-long string in a field
# or a key, and that text ends up in an outcome `detail`; an unbounded render
# would let one bad document evict earlier evidence from the outcome log.
MAX_RENDER = 120
_repr = reprlib.Repr()
_repr.maxstring = 60
_repr.maxother = 60


def _short(value):
    """A bounded repr of an untrusted value (or key name), for a diagnostic."""
    try:
        text = _repr.repr(value)
    except Exception:                          # noqa: BLE001 - never fail rendering
        text = "<unrenderable %s>" % type(value).__name__
    if len(text) > MAX_RENDER:
        text = text[:MAX_RENDER] + "..."
    return text

# Numeric limits, derived from what the reporter can actually emit
# (src/router_health.c):
#   * byte / kB / jiffy counters are C ``unsigned long long`` (64-bit), so
#     0..2**64-1 is the widest legitimate integer; ``cpu.count``, the process
#     counts and the storage counts are narrower but 64-bit is a safe ceiling.
#   * ``host.uptime_seconds`` is a double, and the reporter itself treats any
#     value >= 1e12 (or negative) as unreadable and emits null — so nothing at or
#     above that bound is legitimate.
#   * ``loadavg.*`` are doubles with no kernel bound, but a router load never
#     approaches 1e9 runnable tasks; a value that large is corruption.
# A number outside its field's range is rejected as schema_invalid through the
# ordinary classifier, never by letting a numeric check raise.
INT_LIMIT = 2 ** 64 - 1
MAX_UPTIME_SECONDS = 1e12
MAX_LOAD = 1e9

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
MEM_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree")


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_finite_num(x):
    """True for any finite real number, INCLUDING arbitrary-precision ints.

    ``math.isfinite`` converts its argument to a C double, so calling it on a
    large Python int (e.g. ``10**400``, expressible as a plain JSON integer)
    raises ``OverflowError`` instead of returning False. A Python int is ALWAYS
    finite, so only floats are handed to ``math.isfinite``.
    """
    if not _is_num(x):
        return False
    if isinstance(x, float):
        return math.isfinite(x)
    return True


def _is_uptime(x):
    # uptime_seconds is a double; the reporter treats >= 1e12 (or negative) as
    # unreadable and emits null, so nothing larger is a legitimate value. The
    # comparison is done against the int/float directly (never a float cast), so
    # an arbitrary-size integer is simply out of range, not an OverflowError.
    return _is_finite_num(x) and 0.0 <= x < MAX_UPTIME_SECONDS


def _is_load(x):
    # loadavg fields are doubles; a router load never approaches 1e9 runnable
    # tasks, so anything at/above that is rejected rather than fed to derivation.
    return _is_finite_num(x) and 0.0 <= x < MAX_LOAD


def _is_boot_time(x):
    # An epoch-seconds value read from /proc/stat's `btime`. Same numeric domain
    # as uptime: a finite non-negative number below 1e12.
    return _is_finite_num(x) and 0.0 <= x < MAX_UPTIME_SECONDS


def _is_nonneg_int(x):
    return _is_int(x) and 0 <= x <= INT_LIMIT


def _fail(path, msg):
    raise SchemaInvalid("%s: %s" % (path, msg))


def _req(doc, key, path, kind, kinds):
    if key not in doc:
        _fail("%s.%s" % (path, key), "missing required field")
    v = doc[key]
    if not isinstance(v, kind):
        _fail("%s.%s" % (path, key), "expected %s, got %s" % (kinds, _short(v)))
    return v


def _opt_nullable(doc, key, path, pred, what):
    if key in doc and doc[key] is not None and not pred(doc[key]):
        _fail("%s.%s" % (path, key), "expected %s or null, got %s" % (what, _short(doc[key])))
    return doc.get(key)


def _reject_constant(name):
    """json.loads parse_constant hook: refuse NaN / Infinity / -Infinity."""
    raise ValueError("non-standard JSON constant %r is not allowed" % name)


def _reject_float(text):
    """json.loads parse_float hook: refuse a float literal that overflows to a
    non-finite double (e.g. ``1e999`` -> ``inf``).

    ``parse_constant`` only sees the literal TOKENS NaN / Infinity / -Infinity; a
    numeric literal that OVERFLOWS to infinity is produced by the number parser
    instead, so it needs this hook. Without it, ``{"extra": 1e999}`` parses to an
    ``inf`` hidden in an extra field and could later be re-serialised as the
    non-standard token ``Infinity``.
    """
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number %s is not allowed" % _short(text))
    return value


def _no_duplicate_keys(pairs):
    """json.loads object_pairs_hook: refuse a document with a repeated key.

    A duplicate key is ambiguous (two values for one field, and the LAST one
    silently wins in the default decoder) and no well-formed reporter emits one,
    so it is treated as malformed input rather than guessed at.
    """
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError("duplicate JSON key %s is not allowed" % _short(k))
        seen[k] = v
    return seen


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
        doc = json.loads(text, parse_constant=_reject_constant, parse_float=_reject_float,
                         object_pairs_hook=_no_duplicate_keys)
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
        _fail("schema", "expected %r, got %s" % (SCHEMA, _short(doc.get("schema"))))

    _req(doc, "tool", "", str, "string")
    _req(doc, "version", "", str, "string")
    _req(doc, "units", "", dict, "object")

    host = _req(doc, "host", "", dict, "object")
    _req(host, "sysname", "host", str, "string")
    _req(host, "release", "host", str, "string")
    _req(host, "machine", "host", str, "string")
    _opt_nullable(host, "uptime_seconds", "host", _is_uptime, "finite uptime in [0, 1e12) seconds")
    # boot_time is the EXPLICIT boot identity (/proc/stat `btime`, epoch seconds),
    # optional so a reporter built before it existed remains valid.
    _opt_nullable(host, "boot_time", "host", _is_boot_time, "finite boot epoch in [0, 1e12) seconds")

    # `cpu` and `loadavg` are allowed to be null: the reporter emits null when the
    # corresponding /proc file is unreadable, and one unreadable file must not
    # discard the whole sample. `count` / the load fields are checked only when the
    # section is actually present as an object.
    if "cpu" not in doc:
        _fail("cpu", "missing required field")
    cpu = doc["cpu"]
    if cpu is not None:
        if not isinstance(cpu, dict):
            _fail("cpu", "expected object or null, got %s" % (_short(cpu),))
        if "count" not in cpu:
            _fail("cpu.count", "missing required field")
        if not _is_nonneg_int(cpu["count"]):
            _fail("cpu.count", "expected a non-negative integer, got %s" % (_short(cpu["count"]),))

    if "loadavg" not in doc:
        _fail("loadavg", "missing required field")
    la = doc["loadavg"]
    if la is not None:
        if not isinstance(la, dict):
            _fail("loadavg", "expected object or null, got %s" % (_short(la),))
        for k in ("load1", "load5", "load15"):
            if k not in la:
                _fail("loadavg.%s" % k, "missing required field")
            if not _is_load(la[k]):
                _fail("loadavg.%s" % k,
                      "expected a finite load in [0, 1e9), got %s" % (_short(la[k]),))
        for k in ("procs_running", "procs_total"):
            if k not in la:
                _fail("loadavg.%s" % k, "missing required field")
            if not _is_nonneg_int(la[k]):
                _fail("loadavg.%s" % k,
                      "expected a non-negative integer, got %s" % (_short(la[k]),))

    mem = _req(doc, "memory_kb", "", dict, "object")
    for k in MEM_KEYS:
        if k not in mem:
            _fail("memory_kb.%s" % k, "missing required field")
        if mem[k] is not None and not _is_nonneg_int(mem[k]):
            _fail("memory_kb.%s" % k,
                  "expected a non-negative integer kB or null, got %s" % (_short(mem[k]),))

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
            # The key name is device-controlled: render it bounded.
            if not isinstance(vals, dict):
                _fail("cpu_stat.%s" % _short(name), "expected object")
            for f in CPU_FIELDS:
                if f not in vals:
                    _fail("cpu_stat.%s.%s" % (_short(name), f), "missing required counter")
                if not _is_nonneg_int(vals[f]):
                    _fail("cpu_stat.%s.%s" % (_short(name), f),
                          "expected a non-negative integer of jiffies, got %s"
                          % (_short(vals[f]),))

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
            _fail("%s.used_percent" % p, "must be within 0..100, got %s" % (_short(f["used_percent"]),))
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
                  "expected a non-negative integer, got %s" % (_short(storage[k]),))

    return doc


def parse_and_validate(raw):
    doc = parse(raw)
    validate(doc)
    return doc
