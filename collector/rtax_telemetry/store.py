"""Local storage: timestamped samples, an outcomes log, state, and retention.

Layout under the data directory (default: the project's ``data/`` dir):

    samples/YYYYmmddTHHMMSS.ffffffZ.json   one accepted sample per file
    outcomes.jsonl                          one JSON record per collection attempt
    state.json                              counters + incident + last attempt/sample
    .lock                                   flock held for the life of a run

Retention is bounded in BOTH dimensions (requirement 5 / the storage finding):

  * record counts  — the newest N samples and N outcome records are kept; and
  * byte budgets   — the retained samples and the outcomes log are each trimmed
                     until they fit their byte ceiling, so a device cannot fill the
                     disk by sending large-but-schema-valid documents.

A single sample is refused (``QuotaExceeded``) when it would exceed the per-write
budget, so a write failure is explicit rather than a silent truncation.

Temporary files use a unique suffix (pid + random), so two writers sharing a data
directory cannot collide on a fixed ``*.tmp`` name and lose records.

Every document written here — samples, the outcome log, state — is serialised as
STRICT JSON (``allow_nan=False``): a non-finite float (``inf``/``nan``) raises
instead of being written as the non-standard token ``Infinity``/``NaN``. Combined
with the parser's rejection of non-finite input, a stored document is always
valid, standard JSON.

The raw artifact bytes are NEVER stored — only the reporter's JSON output.
"""

import datetime
import json
import os
import secrets
from pathlib import Path

from .errors import QuotaExceeded

# Defaults; all are configurable through the constructor / CLI / config.json.
DEFAULT_RETAIN_SAMPLE_BYTES = 256 * 1024 * 1024     # 256 MiB of retained samples
DEFAULT_RETAIN_OUTCOME_BYTES = 32 * 1024 * 1024     # 32 MiB of outcomes
DEFAULT_MAX_SAMPLE_BYTES = 2 * 1024 * 1024          # 2 MiB for one sample write


def utc_iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def utc_stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ")


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
                 max_sample_bytes=DEFAULT_MAX_SAMPLE_BYTES):
        self.dir = Path(os.path.expanduser(str(data_dir)))
        self.samples_dir = self.dir / "samples"
        self.outcomes_path = self.dir / "outcomes.jsonl"
        self.state_path = self.dir / "state.json"
        self.lock_path = self.dir / ".lock"
        self.retain_samples = int(retain_samples)
        self.retain_outcomes = int(retain_outcomes)
        self.retain_sample_bytes = int(retain_sample_bytes)
        self.retain_outcome_bytes = int(retain_outcome_bytes)
        self.max_sample_bytes = int(max_sample_bytes)

    # ------------------------------------------------------------ lifecycle
    def ensure(self):
        self.samples_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- samples
    def sample_paths(self):
        if not self.samples_dir.is_dir():
            return []
        return sorted(self.samples_dir.glob("*.json"))

    def save_sample(self, obj, *, when=None):
        self.ensure()
        ts = time_now() if when is None else when
        payload = json.dumps(obj, separators=(",", ":"), sort_keys=False,
                             allow_nan=False).encode("utf-8")
        if len(payload) > self.max_sample_bytes:
            raise QuotaExceeded(
                "sample is %d bytes, over the %d-byte per-write budget"
                % (len(payload), self.max_sample_bytes))
        path = self.samples_dir / (utc_stamp(ts) + ".json")
        tmp = path.with_suffix(".json.%s.tmp" % _tmp_suffix())
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        self.prune_samples()
        return path

    def last_sample(self):
        paths = self.sample_paths()
        if not paths:
            return None
        try:
            with open(paths[-1]) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def sample_count(self):
        return len(self.sample_paths())

    # ------------------------------------------------------------- outcomes
    def append_outcome(self, rec):
        self.ensure()
        with open(self.outcomes_path, "a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), allow_nan=False) + "\n")
        self.prune_outcomes()

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

    # ---------------------------------------------------------------- state
    def load_state(self):
        if not self.state_path.is_file():
            return None
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_state(self, state):
        self.ensure()
        tmp = self.state_path.with_suffix(".json.%s.tmp" % _tmp_suffix())
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------- retention
    def prune_samples(self):
        """Trim oldest samples until BOTH the record count and byte budget hold."""
        paths = self.sample_paths()
        removed = 0
        excess = len(paths) - self.retain_samples
        for p in paths[:max(0, excess)]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        paths = self.sample_paths()
        total = sum(_size(p) for p in paths)
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

    def prune_outcomes(self):
        """Trim oldest outcome lines until count and byte budgets both hold.

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
        while i < len(lines) - self.retain_outcomes:
            total -= sizes[i]
            i += 1
        while total > self.retain_outcome_bytes and i < len(lines):
            total -= sizes[i]
            i += 1
        if i <= 0:
            return 0
        keep = lines[i:]
        tmp = self.outcomes_path.with_suffix(".jsonl.%s.tmp" % _tmp_suffix())
        with open(tmp, "w") as fh:
            fh.writelines(keep)
        os.replace(tmp, self.outcomes_path)
        return i

    def prune(self):
        return self.prune_samples() + self.prune_outcomes()


def time_now():
    import time
    return time.time()
