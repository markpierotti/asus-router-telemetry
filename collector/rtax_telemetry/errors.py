"""Exception taxonomy.

Every failure mode the collector must *distinguish* (requirement 6) has its own
type, so the outcome classifier never has to guess from a string:

  TransportAuthError        SSH authentication was refused
  TransportHostKeyError     the pinned host key did not match  (hard stop)
  TransportUnreachableError the management address could not be reached
  RemoteTimeout             the bounded reporter run was killed by the watchdog
  RemoteCommandError        the reporter ran but exited non-zero
  ArtifactMismatch          the artifact / known_hosts hash gate failed
  MalformedData             the reporter's stdout was not valid JSON
  SchemaInvalid             valid JSON, but the schema / a required type is wrong
  LockHeld                  another collector run already owns the data directory
"""


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
    """The bounded reporter run exceeded its watchdog and was killed."""


class RemoteCommandError(TransportError):
    """A remote command ran but returned non-zero."""

    def __init__(self, rc, message):
        super().__init__(message)
        self.rc = rc


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


class SchemaInvalid(TelemetryError):
    """JSON parsed, but the document is not a valid router_health document."""


class LockHeld(TelemetryError):
    """Another collector run already holds the data-directory lock."""
