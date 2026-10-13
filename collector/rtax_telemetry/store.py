"""Local storage: timestamped samples, an outcomes log, state, and retention.

Layout under the data directory (default: the project's ``data/`` dir):

    samples/YYYYmmddTHHMMSS.ffffffZ.json   one accepted sample per file
    outcomes.jsonl                          one JSON record per collection attempt
    pending_cleanup.jsonl                   directories whose removal is UNCONFIRMED
    state.json                              counters + incident + last attempt/sample
    .lock                                   flock held for the life of a run

Retention is bounded in BOTH dimensions (requirement 5 / the storage finding):

  * record counts  — the newest N samples and N outcome records are kept; and
  * byte budgets   — the retained samples and the outcomes log are each trimmed
                     until they fit their byte ceiling, so a device cannot fill the
                     disk by sending large-but-schema-valid documents.

A single sample is refused (``QuotaExceeded``) when it would exceed the per-write
budget, so a write failure is explicit rather than a silent truncation.

``pending_cleanup.jsonl`` is deliberately NOT pruned and NOT acted on
automatically: it records a remote directory this run may have left behind, with
the evidence that supports (or fails to support) ownership, so a later human-run
read-only reconciliation can decide. Nothing here deletes a directory.

Every document written here — samples, the outcome log, state, pending cleanup — is
serialised as STRICT JSON (``allow_nan=False``): a non-finite float (``inf``/``nan``)
raises instead of being written as the non-standard token ``Infinity``/``NaN``.
Combined with the parser's rejection of non-finite input, a stored document is
always valid, standard JSON.

Durability and containment, per finding:

  * Files are created 0600 and the data/samples directories 0700, so evidence does
    not depend on the operator's umask.
  * Sample and state writes go through a unique temporary file and ``os.replace``,
    with ``fsync`` on the file AND on the directory, so a crash cannot leave a
    half-written document in place of a good one.
  * A torn trailing line in an append-only log is terminated before the next record
    is appended; otherwise the next record would be concatenated onto the fragment
    and BOTH records would be unreadable.
  * A damaged ``state.json`` is QUARANTINED (renamed aside) rather than silently
    reset, so the counters cannot restart without a trace, and a ``state.json`` of
    the wrong shape can never reach the fold logic as a ``TypeError``.
  * ``OSError`` from the evidence filesystem is converted to ``StorageError`` at
    this boundary, so the classifier never has to guess whether an error came from
    the disk or from spawning ssh.

The raw artifact bytes are NEVER stored — only the reporter's JSON output.
"""

import datetime
import json
import os
import secrets
import time
from pathlib import Path

from .errors import QuotaExceeded, StorageError

# Defaults; all are configurable through the constructor / CLI / config.json.
DEFAULT_RETAIN_SAMPLE_BYTES = 256 * 1024 * 1024     # 256 MiB of retained samples
DEFAULT_RETAIN_OUTCOME_BYTES = 32 * 1024 * 1024     # 32 MiB of outcomes
DEFAULT_MAX_SAMPLE_BYTES = 2 * 1024 * 1024          # 2 MiB for one sample write

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def utc_iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def utc_stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ")


def from_iso(s):
    """Parse a stored UTC timestamp back to epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, TypeError, OSError):
        return None


def _tmp_suffix():
    """A per-writer unique suffix so two writers cannot share a temp filename."""
    return "%d.%s" % (os.getpid(), secrets.token_hex(4))


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


class Store:
    def __init__(self, data_dir, *, retain_samples=1440, retain_outcomes=5000,
                 retain_sample_bytes=DEFAULT_RETAIN_SAMPLE_BYTES,
                 retain_outcome_bytes=DEFAULT_RETAIN_OUTCOME_BYTES,
                 max_sample_bytes=DEFAULT_MAX_SAMPLE_BYTES, log=None):
        # The data directory is resolved to an ABSOLUTE path at construction. Sample
        # file names are wall-clock timestamps and were previously persisted exactly
        # as the operator spelled the data dir, so a RELATIVE `--data-dir` recorded a
        # relative sample path: the state pointer could then only be reopened from the
        # same working directory, and from anywhere else the predecessor silently
        # became `first`. `resolve_sample_path()` below keeps OLDER relative records
        # readable too - by resolving them inside THIS store, never against the
        # working directory.
        self.dir = Path(os.path.abspath(os.path.expanduser(str(data_dir))))
        self.samples_dir = self.dir / "samples"
        self.outcomes_path = self.dir / "outcomes.jsonl"
        self.pending_cleanup_path = self.dir / "pending_cleanup.jsonl"
        self.state_path = self.dir / "state.json"
        self.lock_path = self.dir / ".lock"
        self.retain_samples = int(retain_samples)
        self.retain_outcomes = int(retain_outcomes)
        self.retain_sample_bytes = int(retain_sample_bytes)
        self.retain_outcome_bytes = int(retain_outcome_bytes)
        self.max_sample_bytes = int(max_sample_bytes)
        self.log = log or (lambda *a: None)

    # ------------------------------------------------------------ lifecycle
    def ensure(self):
        for d in (self.dir, self.samples_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise StorageError("cannot create the data directory %s: %s" % (d, e))
            try:
                os.chmod(str(d), _DIR_MODE)
            except OSError:
                pass

    # -------------------------------------------------------- write helpers
    def _fsync_dir(self):
        """Flush the directory entry so a rename survives a crash."""
        try:
            dfd = os.open(str(self.dir), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            try:
                os.close(dfd)
            except OSError:
                pass

    def _atomic_write(self, path, payload, mode=_FILE_MODE):
        """Write payload to path via a unique temp file + fsync + atomic replace."""
        tmp = path.with_name(path.name + ".%s.tmp" % _tmp_suffix())
        try:
            with open(tmp, "wb") as fh:
                try:
                    os.chmod(str(tmp), mode)
                except OSError:
                    pass
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
            self._fsync_dir()
        except OSError as e:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise StorageError("could not write %s: %s" % (path, e))

    def _repair_torn_tail(self, path):
        """Terminate a torn trailing line before appending to an append-only log.

        A writer killed mid-write can leave a partial trailing line. Without this,
        the NEXT record is concatenated onto the fragment and BOTH become unreadable
        (the joined line is not valid JSON). Returns True when it repaired.
        """
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) == b"\n":
                    return False
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n")
            self.log("append log %s had a torn trailing line; terminated it before appending"
                     % path.name)
            return True
        except OSError:
            return False

    def _append_jsonl(self, path, rec):
        payload = json.dumps(rec, separators=(",", ":"), allow_nan=False)
        existed = path.exists()
        try:
            self._repair_torn_tail(path)
            with open(path, "a", encoding="utf-8") as fh:
                if not existed:
                    try:
                        os.chmod(str(path), _FILE_MODE)
                    except OSError:
                        pass
                fh.write(payload + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise StorageError("could not append to %s: %s" % (path, e))

    # -------------------------------------------------------------- samples
    def sample_paths(self):
        if not self.samples_dir.is_dir():
            return []
        return sorted(self.samples_dir.glob("*.json"))

    def save_sample(self, obj, *, when=None):
        self.ensure()
        ts = time_now() if when is None else when
        # allow_nan=False: a non-finite float raises ValueError here rather than
        # being written as the non-standard token Infinity/NaN.
        payload = json.dumps(obj, separators=(",", ":"), sort_keys=False,
                             allow_nan=False).encode("utf-8")
        if len(payload) > self.max_sample_bytes:
            raise QuotaExceeded(
                "sample is %d bytes, over the %d-byte per-write budget"
                % (len(payload), self.max_sample_bytes))
        path = self.samples_dir / (utc_stamp(ts) + ".json")
        self._atomic_write(path, payload)
        self.prune_samples(newest=path, protect_newest=True)
        return path

    def last_sample(self):
        """The most recent accepted sample, or None.

        When state NAMES a last accepted sample (``state.last_valid_sample.path``)
        that pointer is AUTHORITATIVE: the sample it names is returned, or None when
        that file cannot be located or read. It is never replaced by "the
        lexicographically last file": sample FILE NAMES are wall-clock timestamps,
        so filename order picks the wrong sample after a clock step and lets a
        future-dated file win - and substituting one for a state-selected sample
        that has gone missing would present a different sample as the last accepted.

        Filename order is used ONLY when state names no sample at all (no
        ``state.json`` yet, or one that was quarantined): there is then no selection
        to preserve. That fallback serves the ``status`` display; the collector's
        own predecessor lookup never uses it.
        """
        recorded, located = self._state_sample_pointer()
        candidate = located if recorded else self.last_sample_path()
        if not candidate:
            return None
        try:
            with open(candidate) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def resolve_sample_path(self, path):
        """Locate a RECORDED sample path. Deterministic; the CWD is never consulted.

        Two kinds of record exist and each has exactly ONE meaning:

          * an ABSOLUTE path - everything written since the data directory became
            absolute at construction - names one file whatever the working directory
            is. It is honoured VERBATIM: never re-anchored, and never replaced by a
            same-named file somewhere else. If that file is gone the answer is None.
          * a RELATIVE path - written by older versions, which persisted the data
            directory exactly as the operator spelled it, e.g.
            ``data-acceptance-1.5.2.l40vi7bh/samples/2026...json`` - belongs to the
            store whose state names it. It resolves to the recorded FILE NAME inside
            THIS store's own samples directory, and nowhere else.

        The relative string is deliberately NOT tried against the current working
        directory. Doing that first, as the previous revision did, let a file at the
        same relative path under whatever directory the collector happened to be
        started from OVERRIDE the state-selected sample; with the same transport and
        host the source guard cannot tell the two apart, so the result was a CPU
        comparison against another dataset rather than a null first-sample value.

        The recorded file name is preserved in both cases. Nothing here ever chooses
        a sample by file-name order, and nothing is rewritten: a pointer whose file
        is missing resolves to None and the caller reports it.

        Returns an existing regular file (as an absolute string) or None.
        """
        if not path:
            return None
        recorded = str(path)
        if os.path.isabs(recorded):
            candidate = recorded
        else:
            name = os.path.basename(recorded)
            if not name:
                return None
            candidate = str(self.samples_dir / name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        return None

    def _state_sample_pointer(self):
        """``(recorded, located)`` for ``state.last_valid_sample.path``.

        ``recorded`` is the pointer exactly as state holds it, or None when state
        names no sample; ``located`` is where that sample is on disk, or None. The
        two are kept apart because "state names no sample" and "state names a sample
        that cannot be found" are different facts.
        """
        try:
            state = self.load_state()
        except Exception:                               # noqa: BLE001
            return None, None
        meta = state.get("last_valid_sample") if isinstance(state, dict) else None
        if isinstance(meta, dict) and meta.get("path"):
            return meta["path"], self.resolve_sample_path(meta["path"])
        return None, None

    def _state_sample_path(self):
        return self._state_sample_pointer()[1]

    def last_sample_path(self):
        """The path of the newest sample file, or None (no read of its contents)."""
        paths = self.sample_paths()
        return paths[-1] if paths else None

    def sample_count(self):
        return len(self.sample_paths())

    # ------------------------------------------------------------- outcomes
    def append_outcome(self, rec):
        self.ensure()
        self._append_jsonl(self.outcomes_path, rec)
        self.prune_outcomes(keep_last=True)

    def outcomes(self, limit=None):
        if not self.outcomes_path.is_file():
            return []
        rows = []
        with open(self.outcomes_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        if limit is not None:
            return rows[-limit:]
        return rows

    # -------------------------------------------------------- pending cleanup
    def append_pending_cleanup(self, rec):
        """Durably record a remote directory whose removal is NOT confirmed.

        Append-only, never pruned and never acted on automatically: it names the
        exact path plus how ownership was (or was not) established, so a later
        human-run read-only reconciliation can decide. Automatic deletion of an
        uncertain directory is deliberately not implemented.
        """
        self.ensure()
        self._append_jsonl(self.pending_cleanup_path, rec)

    def pending_cleanup(self):
        if not self.pending_cleanup_path.is_file():
            return []
        rows = []
        with open(self.pending_cleanup_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    # ---------------------------------------------------------------- state
    def load_state(self):
        if not self.state_path.is_file():
            return None
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError) as e:
            self._quarantine_state("unreadable (%s)" % e)
            return None
        if not isinstance(state, dict) or not isinstance(
                state.get("consecutive_failures"), int):
            self._quarantine_state(
                "not a usable state document (%s)" % type(state).__name__)
            return None
        return state

    def _quarantine_state(self, why):
        """Move a damaged state.json aside instead of silently resetting it.

        A silent reset loses the failure/incident counters AND destroys the evidence
        that the state was damaged; worse, a state of the wrong SHAPE previously
        reached the fold logic and raised TypeError. The file is renamed aside so it
        can be inspected and the failure is reported.
        """
        dest = self.state_path.with_name(
            self.state_path.name + ".corrupt." + utc_stamp(time_now()))
        try:
            os.replace(str(self.state_path), str(dest))
        except OSError as e:
            self.log("state.json was %s and could not be quarantined (%s)" % (why, e))
            return
        self.log("state.json was %s - quarantined to %s; counters restart from zero"
                 % (why, dest.name))

    def write_state(self, state):
        self.ensure()
        try:
            payload = json.dumps(state, indent=2, sort_keys=True,
                                 allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, OverflowError) as e:
            raise StorageError("state could not be serialised as strict JSON: %s" % e)
        self._atomic_write(self.state_path, payload)

    # ------------------------------------------------------------- retention
    def _prune_order(self, newest=None):
        """Sample paths in the order retention removes them.

        Sample names are wall-clock stamps, so name order is age order only while
        the clock moves forward. ``newest`` is the sample ``save_sample`` has just
        written, or for a standalone ``prune`` the last accepted sample that state
        names: whatever its name says, every other sample is older. Samples named
        LATER than it were therefore written before the clock stepped back; they go
        first, then the samples named earlier, and ``newest`` itself last. With a
        forward clock nothing is named later, and this is plain name order.

        After a backward step SMALLER than the span of retained samples the names
        interleave and the order among the older samples is approximate. The sample
        just written is still the last candidate, which is what stops a successful
        attempt from deleting its own evidence.
        """
        paths = self.sample_paths()
        if newest is None or newest not in paths:
            return paths
        return ([p for p in paths if p.name > newest.name]
                + [p for p in paths if p.name < newest.name] + [newest])

    def prune_samples(self, newest=None, protect_newest=False):
        """Trim oldest samples until BOTH the record count and byte budget hold.

        ``protect_newest`` is set by ``save_sample``: the file it has just written is
        then never a candidate, whatever the limits say, so a successful attempt
        cannot delete its own evidence. A standalone ``prune`` leaves it unset - it
        only orders by ``newest`` - so an explicit purge still removes everything.
        """
        order = self._prune_order(newest)
        keep = newest if (protect_newest and newest in order) else None
        paths = [p for p in order if p != keep]
        removed = 0
        excess = len(order) - self.retain_samples
        for p in paths[:max(0, excess)]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        paths = [p for p in paths if p.exists()]
        total = sum(_size(p) for p in paths) + (_size(keep) if keep else 0)
        i = 0
        while total > self.retain_sample_bytes and i < len(paths):
            try:
                total -= _size(paths[i])
                paths[i].unlink()
                removed += 1
            except OSError:
                pass
            i += 1
        return removed

    def prune_outcomes(self, keep_last=False):
        """Trim oldest outcome lines until count and byte budgets both hold.

        ``keep_last`` is set by ``append_outcome``: the line it has just written is
        then never trimmed, whatever the limits say.

        The rewrite uses a unique temporary name, so a concurrent append in another
        process cannot share (and then lose) a fixed ``*.tmp`` file.
        """
        if not self.outcomes_path.is_file():
            return 0
        with open(self.outcomes_path) as fh:
            lines = [ln for ln in fh if ln.strip()]
        sizes = [len(ln.encode("utf-8")) for ln in lines]
        total = sum(sizes)
        i = 0
        stop = len(lines) - (1 if keep_last and lines else 0)
        while i < len(lines) - self.retain_outcomes and i < stop:
            total -= sizes[i]
            i += 1
        while total > self.retain_outcome_bytes and i < stop:
            total -= sizes[i]
            i += 1
        if i <= 0:
            return 0
        keep = lines[i:]
        self._atomic_write(self.outcomes_path, "".join(keep).encode("utf-8"))
        return i

    def _own_sample(self, located):
        """This store's entry for the file at ``located``, or None.

        State records the sample by the spelling of the data directory in use when
        it was written; the same directory reached through a symlink spells every
        path differently. The match is therefore by file identity, against the
        entry of the same name in THIS store's samples directory - so a same-named
        file anywhere else is not this store's sample, and nothing outside the
        store is ever returned.
        """
        if not located:
            return None
        own = self.samples_dir / os.path.basename(located)
        try:
            return own if os.path.samefile(own, located) else None
        except OSError:
            return None

    def prune(self):
        """Standalone retention. The last accepted sample that state names is the
        newest whatever its file name says, so it is the last sample to go."""
        newest = self._own_sample(self._state_sample_path())
        return self.prune_samples(newest=newest) + self.prune_outcomes()


def time_now():
    return time.time()
