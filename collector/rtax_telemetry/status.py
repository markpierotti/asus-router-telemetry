"""The status command: last attempt, last valid sample, sample age, incident."""

import datetime

from .core import OUTCOME_TEXT


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _age(ts, now):
    if ts is None:
        return "unknown"
    secs = max(0, int(now - ts.timestamp()))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)


def sample_age_seconds(store, now):
    last = store.last_sample()
    if not last:
        return None
    ts = _parse_iso(last.get("collected_at"))
    if ts is None:
        return None
    return max(0, int(now - ts.timestamp()))


def render_status(store, state, *, now=None, host=None, transport_name="ssh"):
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    state = state or {}
    lines = []
    lines.append("RT-AX56U telemetry — status")
    lines.append("  data dir            : %s" % store.dir)
    lines.append("  transport           : %s%s" % (
        transport_name, ("   host " + host) if host else ""))
    lines.append("  retention (configured): samples=%d  outcomes=%d" % (
        store.retain_samples, store.retain_outcomes))
    lines.append("  on disk             : samples=%d  outcomes=%d" % (
        store.sample_count(), len(store.outcomes())))
    lines.append("")

    la = state.get("last_attempt")
    if la:
        text = OUTCOME_TEXT.get(la.get("outcome"), la.get("outcome"))
        lines.append("  last attempt        : %s  outcome=%s (%s)  dur=%ss" % (
            la.get("at"), la.get("outcome"), text, la.get("duration_s")))
        if la.get("detail"):
            lines.append("                        detail: %s" % la["detail"])
        if la.get("remote_dir"):
            lines.append("                        remote dir: %s (ownership=%s)" % (
                la["remote_dir"], la.get("remote_ownership")))
        if la.get("cleanup_ok") is False:
            lines.append("                        cleanup: NOT CONFIRMED: %s"
                         % la.get("cleanup_detail"))
    else:
        lines.append("  last attempt        : (none)")

    lv = state.get("last_valid_sample")
    if lv:
        ts = _parse_iso(lv.get("at"))
        lines.append("  last valid sample   : %s  age=%s" % (lv.get("at"), _age(ts, now)))
        lines.append("                        path: %s" % lv.get("path"))
    else:
        lines.append("  last valid sample   : (none)")

    inc = state.get("incident")
    if inc:
        lines.append("  collection incident : OPEN since %s" % inc.get("opened_at"))
        lines.append("                        reason=%s (%s)" % (
            inc.get("reason_outcome"), inc.get("reason", "")))
        lines.append("                        consecutive_failures=%s" % inc.get("consecutive_failures"))
    else:
        lines.append("  collection incident : none")
        resolved = state.get("last_resolved_incident")
        if resolved:
            lines.append("                        last resolved %s (opened %s)" % (
                resolved.get("resolved_at"), resolved.get("opened_at")))

    th = state.get("thresholds") or {"open_after": 3, "resolve_after": 2}
    lines.append("  counters            : consecutive_failures=%s consecutive_successes=%s" % (
        state.get("consecutive_failures", 0), state.get("consecutive_successes", 0)))
    lines.append("  thresholds          : open after %s failures, resolve after %s successes" % (
        th.get("open_after"), th.get("resolve_after")))

    # Remote directories whose removal could NOT be confirmed. They are recorded
    # precisely so a human can reconcile them read-only; nothing deletes them
    # automatically.
    try:
        pending = store.pending_cleanup()
    except Exception:                                   # noqa: BLE001
        pending = []
    if pending:
        lines.append("  unconfirmed cleanup : %d entr%s in %s (manual reconciliation; "
                     "never deleted automatically)" % (
                         len(pending), "y" if len(pending) == 1 else "ies",
                         store.pending_cleanup_path.name))
        for rec in pending[-5:]:
            lines.append("                        %s  %s  ownership=%s" % (
                rec.get("at"), rec.get("remote_dir"), rec.get("remote_ownership")))
    return "\n".join(lines)
