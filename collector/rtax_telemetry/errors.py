"""Exception taxonomy.

Every failure mode the collector must *distinguish* (requirement 6) has its own
type, so the outcome classifier never has to guess from a string:

  TransportAuthError        SSH authentication was refused
  TransportHostKeyError     the pinned host key did not match  (hard stop)
  TransportUnreachableError the management address could not be reached
  RemoteTimeout             the bounded remote command exceeded its deadline
  RemoteCommandError        a remote command ran but exited non-zero
  ProvisioningError         the session could not be provisioned (no sample ran)
  ControlMasterLost         the multiplexed control master disappeared
  ArtifactMismatch          the artifact / known_hosts hash gate failed
  MalformedData             the reporter's stdout was not valid JSON
  SchemaInvalid             valid JSON, but the schema / a required type is wrong
  StorageError              a local evidence write could not be completed
  LockHeld                  another collector run already owns the data directory

`sanitize_detail()` is the SINGLE choke point for untrusted device text that
reaches a human terminal or a persisted record. An authenticated-but-hostile
endpoint controls the bytes of its own stdout/stderr, so raw device text must
never be handed to a terminal (control-sequence injection) or written to disk
(evidence-budget exhaustion) unbounded.
"""

# Maximum length of a rendered `detail` string. Anything longer is truncated with
# an explicit marker, so an oversized detail cannot evict earlier evidence from
# the bounded outcome log.
DETAIL_LIMIT = 500


class TelemetryError(Exception):
    """Base class for every error this package raises deliberately."""


class TransportError(TelemetryError):
    """Anything that goes wrong while talking to the router over ssh."""


class TransportAuthError(TransportError):
    """SSH authentication failed (permission denied)."""


class TransportHostKeyError(TransportError):
    """The router's host key did not match the pinned known_hosts. Hard stop."""


class TransportUnreachableError(TransportError):
    """The management address could not be reached.

    NOTE: this does NOT prove the router is down — a firewall drop, a wrong
    address, a link problem or a disabled sshd all produce the same result.
    """


class RemoteTimeout(TransportError):
    """A bounded remote command exceeded its deadline and was killed."""


class RemoteCommandError(TransportError):
    """A remote command ran but returned non-zero."""

    def __init__(self, rc, message):
        super().__init__(message)
        self.rc = rc


class ProvisioningError(TransportError):
    """The session could not be provisioned, so the reporter never ran.

    Distinct from RemoteCommandError/reporter_failure on purpose: a failure
    during the KAT, tool probe, mkdir, watchdog proof, transfer, artifact gate or
    control exec says nothing about the reporter.
    """


class ControlMasterLost(TransportError):
    """The multiplexed ssh control master is gone or no longer answers.

    A dedicated type so the fallback path can neither prompt for a password nor
    stall on a terminal read: a multiplexed client must NOT silently make a fresh
    direct connection (which would need interactive authentication).
    """


class OutputLimitExceeded(TransportError):
    """The device sent more stdout/stderr than the collector's byte budget allows.

    The child group is terminated and reaped before this is raised; it becomes a
    classified collection failure (`output_overflow`), never an unbounded buffer.
    """


class ArtifactMismatch(TransportError):
    """The local artifact or the pinned known_hosts failed its hash gate."""


class CleanupError(TransportError):
    """The remote temp directory this run created could not be removed.

    Surfaced deliberately: a run that cannot clean up after itself is reported as
    a separate failure, never silently swallowed.
    """


class MalformedData(TelemetryError):
    """The reporter's stdout was not parseable JSON."""


class QuotaExceeded(TelemetryError):
    """A local storage byte budget was exceeded (write or retention).

    Raised when a single sample would exceed the per-write budget, so the failure
    is surfaced explicitly instead of silently truncating or exhausting the disk.
    """


class StorageError(TelemetryError):
    """A local evidence write could not be completed (I/O error, full disk).

    Converted from ``OSError`` at the storage layer so the classifier never has to
    guess whether an OSError came from the evidence filesystem or from spawning
    ssh.
    """


class SchemaInvalid(TelemetryError):
    """JSON parsed, but the document is not a valid router_health document."""


class LockHeld(TelemetryError):
    """Another collector run already holds the data-directory lock."""


def _escape_untrusted(text):
    """Replace anything that is not printable ASCII with a visible escape."""
    parts = []
    for ch in text:
        o = ord(ch)
        if ch in "\r\n\t" or ch == "\x0b" or ch == "\x0c":
            parts.append(" ")
        elif o < 0x20 or o == 0x7F:
            parts.append("\\x%02x" % o)
        elif o > 0x7E:
            parts.append("\\u%04x" % o)
        else:
            parts.append(ch)
    return "".join(parts)


def sanitize_detail(text, limit=DETAIL_LIMIT):
    """Render untrusted text as a bounded, terminal-safe single line.

    Guarantees, for arbitrary device bytes:
      * no byte below 0x20 and no 0x7F reaches the terminal (no control-sequence
        injection, no lone surrogate that would make ``print`` raise);
      * non-ASCII is escaped, so the result is pure printable ASCII;
      * the result is a single line and at most about ``limit`` characters.

    This is the only function allowed to prepare device text for display or for a
    persisted ``detail`` field.
    """
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    s = " ".join(_escape_untrusted(str(text)).split())
    if len(s) > limit:
        extra = len(s) - limit
        s = s[:limit] + "...[%d more chars]" % extra
    return s
