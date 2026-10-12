#!/bin/bash
# fake_ssh.sh — a stand-in `ssh` used ONLY by the offline tests.
#
# It contacts nothing and touches no router. It speaks the same contract as the
# real transport expects: invoked as `ssh <opts...> [user@host] [command]`, with
# the command as the LAST argument (or a bare user@host for the master).
#
# Behaviour is selected by FAKE_SSH_MODE; the reporter document is read from
# FAKE_SSH_DOC. The transferred artifact's SHA-256 is computed from the actual
# bytes on stdin, so the transport's hash gate is genuinely exercised.
#
# Modes:
#   ok                     everything succeeds (tools OK, watchdog kills at the bound)
#   unreachable            whole invocation -> ssh exit 255, "Connection refused"
#   authfail               whole invocation -> ssh exit 255, "Permission denied"
#   timeout                reporter run killed (rc 137, watchdog timer-fired evidence)
#   oom_kill               reporter run killed by SIGKILL with NO timer-fired evidence
#   reporter_fail          reporter exits non-zero
#   selftest_fail          watchdog self-test reports an UNBOUNDED job (exit 0)
#   selftest_crash         self-test reports a CRASH status (139) with no timer fire
#   selftest_nofired       self-test reports status 137 but NO timer-fire evidence
#   selftest_huge_status   self-test status marker carries thousands of digits
#   tools_fail             required-tool probe exits non-zero
#   tools_missing          required-tool probe reports a missing tool
#   tools_malformed        required-tool probe returns an unrecognised marker
#   cleanup_fail           remote dir removal fails (REMOVAL_FAILED, rc 1)
#   cleanup_no_marker      remote dir removal succeeds (rc 0) but prints no marker
#   timeout_cleanup_fail   reporter killed AND removal fails
#   kat_control_byte       the KAT reply is a single 0x1C byte
#   kat_empty              the KAT reply is empty
#   kat_huge_token         the KAT reply is a huge digit run (no whitespace at all)
#   hostile_stderr         a multiplexed command exits 255 with 200 KiB of ESC-laden
#                          stderr that also claims a host-key failure
#   control_no_marker      control exec exits 0 without its marker
#   transfer_bad_json      artifact "hash" line is a huge non-hex blob
#   master_wont_exit       `-O exit` succeeds but the master keeps answering
#   master_start_fail      master establishment exits 255 (Permission denied)
#
# Environment:
#   FAKE_SSH_MODE           the mode above
#   FAKE_SSH_DOC            path to the reporter document for a normal run
#   FAKE_SSH_MASTER_STATE   "up" (default) | "down" | a PATH that, once it exists,
#                           means the master has gone away
#   FAKE_SSH_MASTER_PID     the pid `-O check` reports (default 4242)
#   FAKE_SSH_EXIT_LOG       append a line when `-O exit` is received
set -u

KAT="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
mode="${FAKE_SSH_MODE:-ok}"
args=("$@")

# ---- control-master state emulation ---------------------------------------
# The transport gates EVERY multiplexed command on `ssh -O check`, so the fake
# must answer it. FAKE_SSH_MASTER_STATE is either the literal "up" (default), the
# literal "down", or a PATH. When it is a path the master answers `-O check` while
# the file does NOT exist and reports itself GONE once it does; `-O exit` creates
# it (unless mode=master_wont_exit). That models both "the master went away" and
# "shutdown is verified" with no timing guesswork.
_master_down() {
  case "${FAKE_SSH_MASTER_STATE:-up}" in
    up)   return 1 ;;
    down) return 0 ;;
    "")   return 1 ;;
    *)    [ -e "$FAKE_SSH_MASTER_STATE" ] ;;
  esac
}

_master_check() {
  if _master_down; then
    echo "Control socket connect: No such file or directory" 1>&2
    return 255
  fi
  echo "Master running (pid=${FAKE_SSH_MASTER_PID:-4242})"
  return 0
}

# master shutdown: `-O exit`
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-O" ] && [ "${args[$((i+1))]:-}" = "exit" ]; then
    if [ -n "${FAKE_SSH_EXIT_LOG:-}" ]; then printf 'exit\n' >> "$FAKE_SSH_EXIT_LOG"; fi
    case "${FAKE_SSH_MASTER_STATE:-up}" in
      up|down|"") : ;;
      *) [ "$mode" = "master_wont_exit" ] || : > "$FAKE_SSH_MASTER_STATE" ;;
    esac
    exit 0
  fi
  if [ "${args[$i]}" = "-O" ] && [ "${args[$((i+1))]:-}" = "check" ]; then
    _master_check && exit 0 || exit $?
  fi
done

last="${args[${#args[@]}-1]}"

# Emulate an unreachable / auth-failing host. This is checked BEFORE the master
# branch because that is where the real ssh client decides these: authentication
# and the host-key check happen at control-master ESTABLISHMENT. A multiplexed
# command can no longer produce these classes (the transport refuses to derive them
# from device-controlled stderr), so a test that wants them must fail here.
if [ "$mode" = "unreachable" ]; then
  echo "ssh: connect to host 192.0.2.1 port 22: Connection refused" 1>&2
  exit 255
fi
if [ "$mode" = "authfail" ]; then
  echo "routeradmin@192.0.2.1: Permission denied (password)." 1>&2
  exit 255
fi
if [ "$mode" = "hostkey_mismatch" ]; then
  echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@" 1>&2
  echo "Host key verification failed." 1>&2
  exit 255
fi

case "$last" in
  *@*)
    # master invocation. Optionally exercise the interactive password path: read a
    # line from the CONTROLLING TERMINAL (this is what ssh's prompt does).
    if [ "${FAKE_SSH_READ_TTY:-0}" = "1" ]; then
      if IFS= read -r line < /dev/tty; then
        printf 'READ:%s' "$line" > "${FAKE_SSH_TTY_OUT:?FAKE_SSH_TTY_OUT not set}"
      else
        printf 'READFAIL' > "$FAKE_SSH_TTY_OUT"
      fi
    fi
    if [ "$mode" = "master_start_fail" ]; then
      echo "routeradmin@192.0.2.1: Permission denied (password)." 1>&2
      exit 255
    fi
    exit 0 ;;
esac
cmd="$last"

# A multiplexed command that exits 255 with a large, ESC-laden stderr which ALSO
# claims a host-key failure and a permission denial. The transport must classify
# this as a TRANSPORT failure, bound the detail and emit no control bytes.
if [ "$mode" = "hostile_stderr" ]; then
  python3 - <<'HOSTILE' 1>&2
import sys
sys.stderr.write("Host key verification failed. Permission denied.\n")
sys.stderr.write(("\x1b[2J\x1b[31m" + "A" * 200) * 1000)
HOSTILE
  exit 255
fi

case "$cmd" in
  *"printf abc"*)
      # The device controls these bytes: a control byte, nothing at all, or a huge
      # digit run with no whitespace at all.
      case "$mode" in
        kat_control_byte) printf '\034'; exit 0 ;;
        kat_empty)        exit 0 ;;
        kat_huge_token)   printf '%s' "$(python3 -c 'print("9"*5000)')"; exit 0 ;;
        *)                echo "SHA256(stdin)= $KAT"; exit 0 ;;
      esac ;;
  *"for t in dd"*)
      case "$mode" in
        tools_fail)      echo "probe blew up" 1>&2; exit 2 ;;
        tools_missing)   printf 'TOOLS_MISSING: sha256sum od\n'; exit 0 ;;
        tools_malformed) printf 'WOBBLE\n'; exit 0 ;;
        *)               printf 'TOOLS_OK\n'; exit 0 ;;
      esac ;;
  *"mkdir -m 700"*)     exit 0 ;;
  *"cat > "*)
      if [ "$mode" = "transfer_bad_json" ]; then
        printf 'SHA256(x)= %s\n' "$(python3 -c 'print("zz"*2048)')"
        exit 0
      fi
      sha=$(shasum -a 256 | awk '{print $1}')
      echo "SHA256(x)= $sha"; exit 0 ;;
  *"if=/dev/zero"*)
      # Emulate the watchdog timing: a genuine bounded kill takes ~the bound.
      bound=$(printf '%s' "$cmd" | grep -o 'sleep [0-9][0-9]*' | head -1 | awk '{print $2}')
      [ -n "${bound:-}" ] || bound=1
      case "$mode" in
        selftest_fail)  echo "selftest_exit=0";   echo "selftest_wd_fired=0"; exit 0 ;;
        selftest_crash) sleep "$bound"; echo "selftest_exit=139"; echo "selftest_wd_fired=0"; exit 0 ;;
        selftest_nofired) sleep "$bound"; echo "selftest_exit=137"; echo "selftest_wd_fired=0"; exit 0 ;;
        selftest_huge_status)
            sleep "$bound"
            printf 'selftest_exit=%s\n' "$(python3 -c 'print("9"*5000)')"
            echo "selftest_wd_fired=1"; exit 0 ;;
        *)              sleep "$bound"; echo "selftest_exit=137"; echo "selftest_wd_fired=1"; exit 0 ;;
      esac ;;
  *"control-exec-ok"*)
      case "$mode" in
        control_no_marker) exit 0 ;;
        *)                 echo control-exec-ok; exit 0 ;;
      esac ;;
  *"out.json"*)
      case "$mode" in
        timeout|timeout_cleanup_fail)
            printf 'router_health_wd_fired=1\n' 1>&2; exit 137 ;;
        oom_kill)
            # a SIGKILL with NO watchdog timer-fired evidence: an OOM kill looks the same
            printf 'router_health_wd_fired=0\n' 1>&2; exit 137 ;;
        reporter_fail)                echo "reporter boom" 1>&2; exit 1 ;;
        slow_reporter)                sleep 30; cat "${FAKE_SSH_DOC:?FAKE_SSH_DOC not set}"; exit 0 ;;
        *)                            cat "${FAKE_SSH_DOC:?FAKE_SSH_DOC not set}"; exit 0 ;;
      esac ;;
  *"rm -rf"*)
      case "$mode" in
        cleanup_fail|timeout_cleanup_fail) echo REMOVAL_FAILED 1>&2; exit 1 ;;
        cleanup_no_marker)                 exit 0 ;;
        *)                                 echo REMOVED; exit 0 ;;
      esac ;;
esac
exit 0
