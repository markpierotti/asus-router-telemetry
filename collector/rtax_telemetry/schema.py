"""Validation of the reporter's JSON document.

Two distinct failures are kept apart, because they mean different things
(requirement 6):

    parse()     bytes -> dict     raises MalformedData   on a JSON syntax error
    validate()  dict  -> None     raises SchemaInvalid   on a wrong schema/key/type

`validate` checks the schema tag *and the required field types* before a sample
is ever accepted (requirement 5). It is deliberately strict about the fields the
collector actually consumes, and permissive about extra fields so the reporter
can grow without breaking the collector.
"""

from .errors import MalformedData, SchemaInvalid

SCHEMA = "router_health/2"

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
MEM_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree")


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x):
    return _is_int(x) or isinstance(x, float)


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


def parse(raw):
    """bytes/str -> dict, raising MalformedData on a JSON syntax error."""
    import json

    if isinstance(raw, (bytes, bytearray)):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise MalformedData("stdout is not valid UTF-8: %s" % e)
    else:
        text = raw
    try:
        doc = json.loads(text)
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
    _opt_nullable(host, "uptime_seconds", "host", _is_num, "number")

    cpu = _req(doc, "cpu", "", dict, "object")
    _req(cpu, "count", "cpu", int, "integer")
    if isinstance(cpu.get("count"), bool):
        _fail("cpu.count", "expected integer, got bool")

    la = _req(doc, "loadavg", "", dict, "object")
    for k in ("load1", "load5", "load15"):
        _req(la, k, "loadavg", (int, float), "number")
    for k in ("procs_running", "procs_total"):
        v = _req(la, k, "loadavg", int, "integer")
        if isinstance(v, bool):
            _fail("loadavg.%s" % k, "expected integer, got bool")

    mem = _req(doc, "memory_kb", "", dict, "object")
    for k in MEM_KEYS:
        if k not in mem:
            _fail("memory_kb.%s" % k, "missing required field")
        v = mem[k]
        if v is not None and not _is_int(v):
            _fail("memory_kb.%s" % k, "expected integer kB or null, got %r" % (v,))

    st = doc.get("cpu_stat")
    if st is not None:
        if not isinstance(st, dict):
            _fail("cpu_stat", "expected object or null")
        if "cpu" not in st:
            _fail("cpu_stat", "missing aggregate 'cpu' entry")
        for name, vals in st.items():
            if not isinstance(vals, dict):
                _fail("cpu_stat.%s" % name, "expected object")
            for f in CPU_FIELDS:
                if f not in vals:
                    _fail("cpu_stat.%s.%s" % (name, f), "missing required counter")
                if not _is_int(vals[f]):
                    _fail("cpu_stat.%s.%s" % (name, f), "expected integer jiffies, got %r" % (vals[f],))

    fss = _req(doc, "filesystems", "", list, "array")
    for i, f in enumerate(fss):
        p = "filesystems[%d]" % i
        if not isinstance(f, dict):
            _fail(p, "expected object")
        _req(f, "path", p, str, "string")
        _req(f, "available", p, bool, "boolean")
        for k in ("total_bytes", "free_bytes", "avail_bytes", "used_bytes"):
            _opt_nullable(f, k, p, _is_int, "integer bytes")
        _opt_nullable(f, "used_percent", p, _is_int, "integer")
        _opt_nullable(f, "inodes", p, _is_int, "integer")
        _opt_nullable(f, "inodes_free", p, _is_int, "integer")

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
        v = _req(storage, k, "storage", int, "integer")
        if isinstance(v, bool):
            _fail("storage.%s" % k, "expected integer, got bool")

    return doc


def parse_and_validate(raw):
    doc = parse(raw)
    validate(doc)
    return doc
