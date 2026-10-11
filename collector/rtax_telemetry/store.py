"""Local storage: timestamped samples, an outcomes log, state, and retention.

Layout under the data directory (default: the project's ``data/`` dir):

    samples/YYYYmmddTHHMMSS.ffffffZ.json   one accepted sample per file
    outcomes.jsonl                          one JSON record per collection attempt
    state.json                              counters + incident + last attempt/sample
    .lock                                   flock held for the life of a run

Retention is bounded and configurable (requirement 5): the newest N samples and
the newest N outcome records are kept, older ones are pruned.

The raw artifact bytes are NEVER stored — only the reporter's JSON output.
"""

import datetime
import json
import os
from pathlib import Path


def utc_iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def utc_stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ")


class Store:
    def __init__(self, data_dir, *, retain_samples=1440, retain_outcomes=5000):
        self.dir = Path(os.path.expanduser(str(data_dir)))
        self.samples_dir = self.dir / "samples"
        self.outcomes_path = self.dir / "outcomes.jsonl"
        self.state_path = self.dir / "state.json"
        self.lock_path = self.dir / ".lock"
        self.retain_samples = int(retain_samples)
        self.retain_outcomes = int(retain_outcomes)

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
        path = self.samples_dir / (utc_stamp(ts) + ".json")
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(obj, fh, separators=(",", ":"), sort_keys=False)
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
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
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
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------- retention
    def prune_samples(self):
        paths = self.sample_paths()
        excess = len(paths) - self.retain_samples
        removed = 0
        for p in paths[:max(0, excess)]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def prune_outcomes(self):
        if not self.outcomes_path.is_file():
            return 0
        with open(self.outcomes_path) as fh:
            lines = [ln for ln in fh if ln.strip()]
        excess = len(lines) - self.retain_outcomes
        if excess <= 0:
            return 0
        keep = lines[excess:]
        tmp = self.outcomes_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w") as fh:
            fh.writelines(keep)
        os.replace(tmp, self.outcomes_path)
        return excess

    def prune(self):
        return self.prune_samples() + self.prune_outcomes()


def time_now():
    import time
    return time.time()
