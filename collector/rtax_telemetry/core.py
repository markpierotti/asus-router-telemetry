"""The collector core: one collection attempt, outcome classification, incidents.

`Collector.collect_once()` performs exactly one attempt and returns a record.
Distinct failure modes map to distinct outcomes (requirement 6); a failure that
looks like a lost connection drops the session so the next attempt reconnects,
while a sample that merely fails *validation* (the reporter ran fine) keeps it.

Every attempt is contained: the classifier has a FINAL catch-all, so a device
response or a local fault (an unreadable artifact, a full evidence filesystem, a
faulty state file) becomes a recorded, classified outcome instead of an
unclassified crash that loses the attempt.

Outcome records carry the REMOTE STATE FACTS as well as the verdict:

    remote_dir          the exact remote path this attempt used (or None)
    remote_ownership    none | unknown | owned  — see transport.SSHSession
    cleanup_ok          false whenever removal is NOT CONFIRMED, including when a
                        provision failed part-way and its directory could not be
                        removed
    cleanup_detail      what could not be confirmed

Incident rules (requirement 9): `open_after` consecutive failed attempts open a
collection incident; `resolve_after` consecutive successful attempts clear it.
Incidents are local state only — nothing is notified, nothing is remediated.

HARD STOPS: a pinned host-key mismatch or an artifact-gate failure is not a
transient transport problem, so `is_hard_stop()` helps the loop stop rather than
retry forever against an endpoint whose identity is not established.
"""

import json
import time

from . import COLLECTOR_VERSION, SAMPLE_SCHEMA, STATE_SCHEMA
from . import cpu, schema
from .errors import (
    ArtifactMismatch,
    CleanupError,
    ControlMasterLost,
    MalformedData,
    OutputLimitExceeded,
    ProvisioningError,
    QuotaExceeded,
    RemoteCommandError,
    RemoteTimeout,
    SchemaInvalid,
    StorageError,
    TelemetryError,
    TransportAuthError,
    TransportError,
    TransportHostKeyError,
    TransportUnreachableError,
    sanitize_detail,
)
from .store import from_iso, utc_iso

OUTCOME_SUCCESS = "success"
OUTCOME_MALFORMED = "malformed_data"
OUTCOME_SCHEMA = "schema_invalid"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_REPORTER_FAILURE = "reporter_failure"
OUTCOME_PROVISION = "provisioning_failure"
OUTCOME_MASTER_LOST = "control_master_lost"
OUTCOME_AUTH = "ssh_auth_failure"
OUTCOME_HOSTKEY = "ssh_hostkey_mismatch"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_ARTIFACT = "artifact_mismatch"
OUTCOME_TRANSPORT = "transport_error"
OUTCOME_OVERFLOW = "output_overflow"
OUTCOME_STORAGE = "storage_error"
OUTCOME_INTERNAL = "internal_error"

# A one-line human meaning for each outcome, used by the status command.
OUTCOME_TEXT = {
    OUTCOME_SUCCESS: "sample accepted",
    OUTCOME_MALFORMED: "reporter output was not valid JSON",
    OUTCOME_SCHEMA: "reporter output failed schema/field validation",
    OUTCOME_TIMEOUT: "a bounded remote command exceeded its deadline",
    OUTCOME_REPORTER_FAILURE: "reporter exited non-zero, or was ended by a signal",
    OUTCOME_PROVISION: "the session could not be provisioned (the reporter never ran)",
    OUTCOME_MASTER_LOST: "the ssh control master went away; no fallback attempted",
    OUTCOME_AUTH: "ssh authentication was refused",
    OUTCOME_HOSTKEY: "router host key did not match the pinned key (HARD STOP)",
    OUTCOME_UNREACHABLE: "management address unreachable (does NOT prove the router is down)",
    OUTCOME_ARTIFACT: "artifact/known_hosts hash gate failed (HARD STOP)",
    OUTCOME_TRANSPORT: "ssh/transport error",
    OUTCOME_OVERFLOW: "device output exceeded the collector's byte budget",
    OUTCOME_STORAGE: "local evidence could not be written",
    OUTCOME_INTERNAL: "an unexpected internal error was contained and recorded",
}

FAILURE_OUTCOMES = (
    OUTCOME_MALFORMED, OUTCOME_SCHEMA, OUTCOME_TIMEOUT, OUTCOME_REPORTER_FAILURE,
    OUTCOME_PROVISION, OUTCOME_MASTER_LOST, OUTCOME_AUTH, OUTCOME_HOSTKEY,
    OUTCOME_UNREACHABLE, OUTCOME_ARTIFACT, OUTCOME_TRANSPORT, OUTCOME_OVERFLOW,
    OUTCOME_STORAGE, OUTCOME_INTERNAL,
)

# Outcomes that must STOP the loop rather than be retried: the endpoint's identity
# or the reviewed binary is not what was pinned, and retrying cannot fix that.
HARD_STOP_OUTCOMES = (OUTCOME_HOSTKEY, OUTCOME_ARTIFACT)


def is_hard_stop(outcome):
    return outcome in HARD_STOP_OUTCOMES


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
        "remote_dir": attempt.get("remote_dir"),
        "remote_ownership": attempt.get("remote_ownership"),
    }
    if sample_meta is not None:
        state["last_valid_sample"] = sample_meta

    incident = state.get("incident")
    if not isinstance(incident, dict):
        incident = None
        state["incident"] = None
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
        self._last_session = None       # kept so a teardown failure can name its path
        self.cleanup_error = None       # set when a teardown fails; surfaced by the CLI
        self.cleanup_interrupted = False  # teardown was cancelled; removal UNCONFIRMED
        self.lifecycle_error = None     # master shutdown could not be confirmed
        self.persistence_error = None   # set when evidence could NOT be written

    # ------------------------------------------------------------- sessions
    def _ensure_session(self):
        if self._session is None:
            self._session = self.transport.start()
        return self._session

    def _drop_session(self):
        session, self._session = self._session, None
        if session is None:
            return
        self._last_session = session
        try:
            self.transport.stop(session)
        except BaseException as e:                  # noqa: BLE001 - see below
            # `Exception` alone is not enough here. A CANCELLATION (KeyboardInterrupt
            # from Ctrl-C/SIGTERM/SIGHUP) or any other BaseException raised while
            # tearing down means the remote directory may still exist, and that has to
            # be recorded before the cancellation is re-propagated. Swallowing the
            # cancellation would lose the operator's intent; not recording it would
            # lose the cleanup uncertainty.
            #
            # An ordinary Exception (a failed removal, an unreachable device) is a
            # teardown FAILURE: it is recorded and the run continues to its normal
            # classification. Only a cancellation-class BaseException is re-raised.
            self.cleanup_error = sanitize_detail(
                e if isinstance(e, Exception) else
                ("teardown was INTERRUPTED by %s; removal of the remote directory is "
                 "UNCONFIRMED" % type(e).__name__))
            if not isinstance(e, Exception):
                self.cleanup_interrupted = True
            self.log("cleanup: %s" % self.cleanup_error)
            if not isinstance(e, Exception):
                raise
        finally:
            self._capture_transport_lifecycle()

    def _capture_transport_lifecycle(self):
        """Lift the transport's master-shutdown verdict into the collector.

        Called even when teardown raised, so an interrupted teardown still records
        what happened to the control master.
        """
        err = getattr(self.transport, "lifecycle_error", None)
        if err:
            self.lifecycle_error = sanitize_detail(err)
            self.log("master shutdown: %s" % self.lifecycle_error)

    def close(self):
        """Tear down and persist whatever could not be confirmed.

        The teardown record is written from a ``finally``, so it is written even when
        ``_drop_session`` re-raises a cancellation. Nothing here turns a cancellation
        or an unconfirmed teardown into success.
        """
        try:
            self._drop_session()
        finally:
            if self.cleanup_error or self.lifecycle_error:
                self._note_teardown_failure()

    def _note_teardown_failure(self):
        """Persist anything the teardown could NOT confirm — durably, in three places.

        A state patch alone is not durable evidence: the next attempt overwrites
        ``last_attempt``, so a SHUTDOWN cleanup failure could disappear from history
        entirely. The failure is therefore also appended to the append-only outcome
        log as an explicit ``teardown`` record, and — when a REMOTE DIRECTORY's
        removal is the thing that is unconfirmed — the exact path is written to
        ``pending_cleanup.jsonl`` so a later read-only reconciliation can find it.

        A master-shutdown failure alone does NOT create a pending-directory entry: the
        directory may be gone and what is unconfirmed is the *master*, so naming a
        directory for deletion would be wrong. It is recorded in the teardown record
        and the state instead.

        If any of those writes fails, the failure is REPORTED (``persistence_error``)
        rather than the record being assumed durable.
        """
        session = self._last_session
        remote_dir = getattr(session, "remote_dir", None)
        ownership = getattr(session, "ownership", "unknown")
        at = utc_iso(self.clock())
        problems = []
        cleanup_detail = self.cleanup_error
        lifecycle_detail = self.lifecycle_error
        master_shutdown = getattr(self.transport, "master_shutdown", None)

        try:
            self.store.append_outcome({
                "record": "teardown",
                "at": at,
                "outcome": "cleanup_failure" if cleanup_detail else "lifecycle_failure",
                "remote_dir": remote_dir,
                "remote_ownership": ownership,
                "cleanup_ok": False,
                "cleanup_detail": cleanup_detail,
                "cleanup_interrupted": bool(self.cleanup_interrupted),
                "master_shutdown": master_shutdown,
                "lifecycle_error": lifecycle_detail,
            })
        except Exception as e:                      # noqa: BLE001
            problems.append("teardown record not persisted (%s)" % e)

        try:
            state = self.store.load_state()
            la = state.get("last_attempt") if isinstance(state, dict) else None
            if isinstance(la, dict):
                la["cleanup_ok"] = False
                la["cleanup_detail"] = cleanup_detail or lifecycle_detail
                if lifecycle_detail:
                    la["master_shutdown"] = master_shutdown
                    la["lifecycle_error"] = lifecycle_detail
                self.store.write_state(state)
            else:
                problems.append("no last attempt in state to annotate")
        except Exception as e:                      # noqa: BLE001
            problems.append("state not updated (%s)" % e)

        if cleanup_detail:
            self._record_pending_cleanup(remote_dir, ownership, cleanup_detail)

        if problems:
            merged = "; ".join(problems)
            if self.persistence_error:
                merged = "%s; %s" % (self.persistence_error, merged)
            self.persistence_error = sanitize_detail(merged)
            self.log("EVIDENCE NOT PERSISTED: %s" % self.persistence_error)

    # ------------------------------------------------------------- attempt
    def collect_once(self):
        t0 = self.clock()
        m0 = self.monotonic()
        outcome = None
        detail = ""
        sample_path = None
        sample_meta = None
        remote_dir = None
        ownership = "none"
        provision_cleanup_ok = None
        provision_cleanup_detail = None
        caught = None
        self.cleanup_error = None       # per-attempt: reset so it reflects THIS attempt
        self.cleanup_interrupted = False

        try:
            session = self._ensure_session()
            remote_dir = getattr(session, "remote_dir", None)
            ownership = getattr(session, "ownership", "none")
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
                "host_boot_time": doc["host"].get("boot_time"),
                "monotonic": m0,
            }
        except MalformedData as e:
            outcome, detail = OUTCOME_MALFORMED, str(e)
        except SchemaInvalid as e:
            outcome, detail = OUTCOME_SCHEMA, str(e)
        except ProvisioningError as e:
            outcome, detail, caught = OUTCOME_PROVISION, str(e), e
            self._drop_session()
        except ControlMasterLost as e:
            outcome, detail, caught = OUTCOME_MASTER_LOST, str(e), e
            self._drop_session()
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
            outcome, detail, caught = OUTCOME_REPORTER_FAILURE, str(e), e
            self._drop_session()
        except ArtifactMismatch as e:
            outcome, detail, caught = OUTCOME_ARTIFACT, str(e), e
            self._drop_session()
        except OutputLimitExceeded as e:
            # the device sent more output than the byte budget allows: a classified
            # collection failure, not an unbounded buffer
            outcome, detail, caught = OUTCOME_OVERFLOW, str(e), e
            self._drop_session()
        except QuotaExceeded as e:
            # local storage refused the write; the reporter itself was fine, so the
            # session is kept for the next attempt
            outcome, detail = OUTCOME_STORAGE, str(e)
        except StorageError as e:
            outcome, detail, caught = OUTCOME_STORAGE, str(e), e
        except TransportError as e:
            outcome, detail, caught = OUTCOME_TRANSPORT, str(e), e
            self._drop_session()
        except TelemetryError as e:
            outcome, detail = OUTCOME_TRANSPORT, str(e)
            self._drop_session()
        except Exception as e:                          # noqa: BLE001 - containment
            # The FINAL catch-all. A device response or a local fault that the
            # taxonomy above did not name (a missing ssh binary, an unreadable
            # artifact, a serialisation fault, a damaged state document) is recorded
            # as an internal error and the attempt is still logged, rather than
            # escaping as an unclassified crash that loses it.
            outcome = OUTCOME_INTERNAL
            detail = "%s: %s" % (type(e).__name__, e)
            caught = e
            self._drop_session()

        # PROVISIONING FACTS ARE FOLDED IN FOR EVERY CLASSIFICATION, not only for
        # ProvisioningError. The transport attaches them to whichever exception ended
        # the provisioning phase — ControlMasterLost, ArtifactMismatch,
        # OutputLimitExceeded, StorageError, an internal error — and dropping them would
        # record `cleanup_ok: true` and lose the exact path of a directory that may
        # still exist on the router. The OUTCOME CLASS is untouched: only the
        # cleanup/ownership evidence is taken from the exception.
        if caught is not None:
            rd, own, ok, det = self._provision_facts(caught)
            if rd:
                remote_dir = rd
            if own is not None:
                ownership = own
            if ok is False:
                provision_cleanup_ok = False
                provision_cleanup_detail = det

        duration = round(self.monotonic() - m0, 3)
        cleanup_ok = self.cleanup_error is None
        cleanup_detail = self.cleanup_error
        if provision_cleanup_ok is False:
            cleanup_ok = False
            cleanup_detail = provision_cleanup_detail or cleanup_detail
            # A provisioning failure that could not reconcile its own remote
            # directory is exactly the case the recovery plan warns about: the
            # attempt record carries cleanup_ok=false, and the exact path is also
            # written to the durable pending-cleanup log.
            self._record_pending_cleanup(remote_dir, ownership, cleanup_detail)
        elif provision_cleanup_detail and cleanup_detail is None:
            cleanup_detail = provision_cleanup_detail
        attempt = {
            "at": utc_iso(t0),
            "monotonic": m0,
            "duration_s": duration,
            "outcome": outcome,
            "detail": sanitize_detail(detail),
            "sample_path": str(sample_path) if sample_path else None,
            "remote_dir": remote_dir,
            "remote_ownership": ownership,
            "cleanup_ok": cleanup_ok,
            "cleanup_detail": sanitize_detail(cleanup_detail) if cleanup_detail else None,
        }
        self._persist_attempt(attempt, sample_meta)
        return attempt

    @staticmethod
    def _provision_facts(e):
        """Facts about a FAILED PROVISION, taken from the exception the transport raised.

        The transport attaches these because a failed provision is exactly when a
        remote directory is most likely to be left behind. The single
        ``provision_facts`` dict is preferred so that the PRESENCE of the facts is
        unambiguous; the individual attributes are still honoured for test doubles
        written against the earlier shape. When neither is present the ownership is
        reported as ``none`` and no cleanup claim is made — never a silent
        ``cleanup_ok: true``.
        """
        facts = getattr(e, "provision_facts", None)
        if isinstance(facts, dict):
            # authoritative when present: `remote_ownership` may legitimately be
            # "none" here, and that IS the recorded fact
            return (facts.get("remote_dir"),
                    facts.get("remote_ownership"),
                    facts.get("cleanup_ok"),
                    facts.get("cleanup_detail"))
        if hasattr(e, "provision_cleanup_ok") or hasattr(e, "remote_ownership"):
            return (getattr(e, "remote_dir", None),
                    getattr(e, "remote_ownership", None),
                    getattr(e, "provision_cleanup_ok", None),
                    getattr(e, "provision_cleanup_detail", None))
        # NO facts at all (an exception raised after a successful provision, e.g. a
        # master lost mid-collection). Returning a default "none" here would OVERWRITE
        # the ownership the live session already reported, so the absence is returned
        # as None and the caller keeps what it has.
        return (None, None, None, None)

    def _record_pending_cleanup(self, remote_dir, ownership, detail):
        """Durably record a remote directory whose removal is NOT confirmed.

        Append-only evidence naming the exact path and how ownership was (or was
        not) established. NOTHING acts on this file automatically and nothing is
        ever deleted from it: automatic deletion of an uncertain directory is
        deliberately not implemented. A failure to write it is REPORTED.
        """
        if not remote_dir:
            return
        try:
            self.store.append_pending_cleanup({
                "at": utc_iso(self.clock()),
                "remote_dir": remote_dir,
                "remote_ownership": ownership,
                "reason": detail,
                "manual_reconciliation_required": True,
                "automatic_deletion": False,
            })
        except Exception as e:                          # noqa: BLE001
            self.persistence_error = sanitize_detail(
                "pending-cleanup evidence not persisted (%s)" % e)
            self.log("EVIDENCE NOT PERSISTED: %s" % self.persistence_error)

    def _persist_attempt(self, attempt, sample_meta):
        """Fold the attempt into state and append it to the outcome log.

        A failure to WRITE evidence is reported (and surfaces in the exit status)
        rather than silently swallowed: this collector never claims a record was made
        when the evidence filesystem refused it.
        """
        problems = []
        try:
            state = self.store.load_state() or default_state(self.open_after, self.resolve_after)
            update_state(state, attempt, sample_meta,
                         open_after=self.open_after, resolve_after=self.resolve_after)
            self.store.write_state(state)
        except Exception as e:                          # noqa: BLE001
            problems.append("state not persisted (%s: %s)" % (type(e).__name__, e))
        try:
            self.store.append_outcome(attempt)
        except Exception as e:                          # noqa: BLE001
            problems.append("outcome record not persisted (%s: %s)" % (type(e).__name__, e))
        if problems:
            self.persistence_error = sanitize_detail("; ".join(problems))
            self.log("EVIDENCE NOT PERSISTED: %s" % self.persistence_error)

    # -------------------------------------------------------------- derived
    def _predecessor(self):
        """The previous sample THIS run may legitimately compare the new one against.

        Located through ``state.last_valid_sample.path`` — NOT by taking the
        lexicographically last file name. File names are WALL-CLOCK timestamps, so
        listing them picks the wrong predecessor after a clock step, lets a
        future-dated file win, and makes retention prune the newest real samples
        first. The predecessor must also carry the SAME ``source`` (transport + host);
        samples from another device or from the mock transport must never be compared
        against a live sample.

        Returns ``(previous_sample_dict_or_None, state_meta_or_None)``.
        """
        try:
            state = self.store.load_state() or {}
        except Exception:                               # noqa: BLE001
            return None, None
        meta = state.get("last_valid_sample")
        if not isinstance(meta, dict) or not meta.get("path"):
            return None, None
        try:
            with open(meta["path"]) as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            return None, meta
        if not isinstance(prev, dict):
            return None, meta
        src = prev.get("source")
        current = {"transport": getattr(self.transport, "name", "?"),
                   "host": self.host_label}
        if isinstance(src, dict) and src != current:
            self.log("predecessor sample %s has source %r, not %r - treating this as a "
                     "FIRST sample" % (meta["path"], src, current))
            return None, meta
        return prev, meta

    def _build_sample(self, doc, m0, t0):
        prev, _meta = self._predecessor()
        if not isinstance(prev, dict):
            prev = None
        prev_rep = prev.get("reporter") if prev else None
        prev_at = from_iso(prev.get("collected_at")) if prev else None
        reboot = cpu.detect_reboot(prev_rep, doc, prev_at=prev_at, cur_at=t0) if prev_rep else False
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
        mem = doc.get("memory_kb") or {}
        if mem.get("MemTotal") is not None and mem.get("MemAvailable") is not None:
            bits.append("mem_free=%skB/%skB" % (mem.get("MemFree"), mem.get("MemTotal")))
        # loadavg may legitimately be null (an unreadable /proc/loadavg must not
        # discard the whole sample), so it is tolerated rather than assumed to exist.
        la = doc.get("loadavg") or {}
        if la.get("load1") is not None:
            bits.append("load1=%s" % la["load1"])
        return "; ".join(bits)
