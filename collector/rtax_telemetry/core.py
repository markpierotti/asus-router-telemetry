"""The collector core: one collection attempt, outcome classification, incidents.

`Collector.collect_once()` performs exactly one attempt and returns a record.
Distinct failure modes map to distinct outcomes (requirement 6); a failure that
looks like a lost connection drops the session so the next attempt reconnects,
while a sample that merely fails *validation* (the reporter ran fine) keeps it.

Incident rules (requirement 9): `open_after` consecutive failed attempts open a
collection incident; `resolve_after` consecutive successful attempts clear it.
Incidents are local state only — nothing is notified, nothing is remediated.
"""

import time

from . import COLLECTOR_VERSION, SAMPLE_SCHEMA, STATE_SCHEMA
from . import cpu, schema
from .errors import (
    ArtifactMismatch,
    MalformedData,
    RemoteCommandError,
    RemoteTimeout,
    SchemaInvalid,
    TelemetryError,
    TransportAuthError,
    TransportError,
    TransportHostKeyError,
    TransportUnreachableError,
)
from .store import utc_iso

OUTCOME_SUCCESS = "success"
OUTCOME_MALFORMED = "malformed_data"
OUTCOME_SCHEMA = "schema_invalid"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_REPORTER_FAILURE = "reporter_failure"
OUTCOME_AUTH = "ssh_auth_failure"
OUTCOME_HOSTKEY = "ssh_hostkey_mismatch"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_ARTIFACT = "artifact_mismatch"
OUTCOME_TRANSPORT = "transport_error"

# A one-line human meaning for each outcome, used by the status command.
OUTCOME_TEXT = {
    OUTCOME_SUCCESS: "sample accepted",
    OUTCOME_MALFORMED: "reporter output was not valid JSON",
    OUTCOME_SCHEMA: "reporter output failed schema/field validation",
    OUTCOME_TIMEOUT: "reporter run exceeded its bound and was killed",
    OUTCOME_REPORTER_FAILURE: "reporter exited non-zero",
    OUTCOME_AUTH: "ssh authentication was refused",
    OUTCOME_HOSTKEY: "router host key did not match the pinned key",
    OUTCOME_UNREACHABLE: "management address unreachable (does NOT prove the router is down)",
    OUTCOME_ARTIFACT: "artifact/known_hosts hash gate failed",
    OUTCOME_TRANSPORT: "ssh/transport error",
}

FAILURE_OUTCOMES = (
    OUTCOME_MALFORMED, OUTCOME_SCHEMA, OUTCOME_TIMEOUT, OUTCOME_REPORTER_FAILURE,
    OUTCOME_AUTH, OUTCOME_HOSTKEY, OUTCOME_UNREACHABLE, OUTCOME_ARTIFACT, OUTCOME_TRANSPORT,
)


def default_state(open_after=3, resolve_after=2):
    return {
        "schema": STATE_SCHEMA,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "incident": None,
        "last_resolved_incident": None,
        "last_attempt": None,
        "last_valid_sample": None,
        "thresholds": {"open_after": open_after, "resolve_after": resolve_after},
    }


def update_state(state, attempt, sample_meta=None, *, open_after=3, resolve_after=2):
    """Fold one attempt into the running state. Pure; returns the state."""
    success = attempt["outcome"] == OUTCOME_SUCCESS
    if success:
        state["consecutive_successes"] = state.get("consecutive_successes", 0) + 1
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["consecutive_successes"] = 0

    state["thresholds"] = {"open_after": open_after, "resolve_after": resolve_after}
    state["last_attempt"] = {
        "at": attempt["at"],
        "outcome": attempt["outcome"],
        "detail": attempt.get("detail", ""),
        "duration_s": attempt.get("duration_s"),
        "cleanup_ok": attempt.get("cleanup_ok", True),
        "cleanup_detail": attempt.get("cleanup_detail"),
    }
    if sample_meta is not None:
        state["last_valid_sample"] = sample_meta

    incident = state.get("incident")
    if not success and incident is None and state["consecutive_failures"] >= open_after:
        state["incident"] = {
            "opened_at": attempt["at"],
            "reason_outcome": attempt["outcome"],
            "reason": attempt.get("detail", ""),
            "consecutive_failures": state["consecutive_failures"],
        }
    elif success and incident is not None and state["consecutive_successes"] >= resolve_after:
        state["last_resolved_incident"] = {
            "opened_at": incident.get("opened_at"),
            "resolved_at": attempt["at"],
            "reason_outcome": incident.get("reason_outcome"),
        }
        state["incident"] = None
    return state


class Collector:
    def __init__(self, transport, store, *, open_after=3, resolve_after=2,
                 artifact_sha=None, host_label=None, log=None,
                 clock=time.time, monotonic=time.monotonic):
        self.transport = transport
        self.store = store
        self.open_after = open_after
        self.resolve_after = resolve_after
        self.artifact_sha = artifact_sha
        self.host_label = host_label
        self.log = log or (lambda *a: None)
        self.clock = clock
        self.monotonic = monotonic
        self._session = None
        self.cleanup_error = None       # set when a teardown fails; surfaced by the CLI

    # ------------------------------------------------------------- sessions
    def _ensure_session(self):
        if self._session is None:
            self._session = self.transport.start()
        return self._session

    def _drop_session(self):
        session, self._session = self._session, None
        if session is None:
            return
        try:
            self.transport.stop(session)
        except Exception as e:                      # noqa: BLE001
            self.cleanup_error = str(e)
            self.log("cleanup: %s" % e)

    def close(self):
        self._drop_session()
        if self.cleanup_error:
            self._note_cleanup_failure()

    def _note_cleanup_failure(self):
        """Persist the teardown failure into the last attempt so `status` shows it."""
        state = self.store.load_state()
        if not state:
            return
        la = state.get("last_attempt")
        if not la:
            return
        la["cleanup_ok"] = False
        la["cleanup_detail"] = self.cleanup_error
        self.store.write_state(state)

    # ------------------------------------------------------------- attempt
    def collect_once(self):
        t0 = self.clock()
        m0 = self.monotonic()
        outcome = None
        detail = ""
        sample_path = None
        sample_meta = None
        self.cleanup_error = None       # per-attempt: reset so it reflects THIS attempt

        try:
            session = self._ensure_session()
            raw = self.transport.collect_sample(session)
            doc = schema.parse(raw)                     # -> MalformedData
            schema.validate(doc)                        # -> SchemaInvalid
            sample = self._build_sample(doc, m0, t0)
            path = self.store.save_sample(sample, when=t0)
            sample_path = path
            outcome = OUTCOME_SUCCESS
            detail = self._success_detail(sample)
            sample_meta = {
                "at": sample["collected_at"],
                "path": str(path),
                "host_uptime_seconds": doc["host"].get("uptime_seconds"),
                "monotonic": m0,
            }
        except MalformedData as e:
            outcome, detail = OUTCOME_MALFORMED, str(e)
        except SchemaInvalid as e:
            outcome, detail = OUTCOME_SCHEMA, str(e)
        except TransportAuthError as e:
            outcome, detail = OUTCOME_AUTH, str(e)
            self._drop_session()
        except TransportHostKeyError as e:
            outcome, detail = OUTCOME_HOSTKEY, str(e)
            self._drop_session()
        except TransportUnreachableError as e:
            outcome = OUTCOME_UNREACHABLE
            detail = str(e) + " (this alone does not prove the router is down)"
            self._drop_session()
        except RemoteTimeout as e:
            outcome, detail = OUTCOME_TIMEOUT, str(e)
            self._drop_session()
        except RemoteCommandError as e:
            outcome, detail = OUTCOME_REPORTER_FAILURE, str(e)
            self._drop_session()
        except ArtifactMismatch as e:
            outcome, detail = OUTCOME_ARTIFACT, str(e)
            self._drop_session()
        except TransportError as e:
            outcome, detail = OUTCOME_TRANSPORT, str(e)
            self._drop_session()
        except TelemetryError as e:
            outcome, detail = OUTCOME_TRANSPORT, str(e)
            self._drop_session()

        duration = round(self.monotonic() - m0, 3)
        attempt = {
            "at": utc_iso(t0),
            "monotonic": m0,
            "duration_s": duration,
            "outcome": outcome,
            "detail": detail,
            "sample_path": str(sample_path) if sample_path else None,
            "cleanup_ok": self.cleanup_error is None,
            "cleanup_detail": self.cleanup_error,
        }
        state = self.store.load_state() or default_state(self.open_after, self.resolve_after)
        update_state(state, attempt, sample_meta,
                     open_after=self.open_after, resolve_after=self.resolve_after)
        self.store.write_state(state)
        self.store.append_outcome(attempt)
        return attempt

    # -------------------------------------------------------------- derived
    def _build_sample(self, doc, m0, t0):
        prev = self.store.last_sample()
        prev_rep = prev.get("reporter") if isinstance(prev, dict) else None
        reboot = cpu.detect_reboot(prev_rep, doc) if prev_rep else False
        interval = None
        if prev_rep and isinstance(prev.get("monotonic"), (int, float)):
            interval = round(m0 - prev["monotonic"], 3)
        prev_stat = prev_rep.get("cpu_stat") if prev_rep else None
        util = cpu.compare(prev_stat, doc.get("cpu_stat"), reboot=reboot,
                           interval_seconds=interval)
        return {
            "schema": SAMPLE_SCHEMA,
            "collected_at": utc_iso(t0),
            "monotonic": m0,
            "collector_version": COLLECTOR_VERSION,
            "artifact_sha256": self.artifact_sha,
            "source": {"transport": getattr(self.transport, "name", "?"),
                       "host": self.host_label},
            "outcome": OUTCOME_SUCCESS,
            "derived": {"cpu_utilisation": util},
            "reporter": doc,
        }

    @staticmethod
    def _success_detail(sample):
        doc = sample["reporter"]
        util = sample["derived"]["cpu_utilisation"]
        agg = (util.get("aggregate") or {})
        bits = ["cpu_util_state=%s" % util.get("state")]
        if agg.get("util_percent") is not None:
            bits.append("cpu_util=%s%%" % agg["util_percent"])
        mem = doc.get("memory_kb", {})
        if mem.get("MemTotal") is not None and mem.get("MemAvailable") is not None:
            bits.append("mem_free=%skB/%skB" % (mem.get("MemFree"), mem.get("MemTotal")))
        la = doc.get("loadavg", {})
        if la.get("load1") is not None:
            bits.append("load1=%s" % la.get("load1"))
        return "; ".join(bits)
