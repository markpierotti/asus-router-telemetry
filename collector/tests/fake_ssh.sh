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
#   timeout                reporter run killed (rc 137)
#   reporter_fail          reporter exits non-zero
#   selftest_fail          watchdog self-test reports an UNBOUNDED job (exit 0)
#   selftest_crash         self-test reports a CRASH status (139) with no timer fire
#   selftest_nofired       self-test reports status 137 but NO timer-fire evidence
#   tools_fail             required-tool probe exits non-zero
#   tools_missing          required-tool probe reports a missing tool
#   tools_malformed        required-tool probe returns an unrecognised marker
#   cleanup_fail           remote dir removal fails (REMOVAL_FAILED, rc 1)
#   timeout_cleanup_fail   reporter killed AND removal fails
set -u

KAT="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
mode="${FAKE_SSH_MODE:-ok}"
args=("$@")

# master shutdown: `-O exit`
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-O" ] && [ "${args[$((i+1))]:-}" = "exit" ]; then exit 0; fi
done

last="${args[${#args[@]}-1]}"
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
    exit 0 ;;
esac
cmd="$last"

# emulate an unreachable/auth-failing host for the WHOLE invocation
if [ "$mode" = "unreachable" ]; then
  echo "ssh: connect to host 192.0.2.1 port 22: Connection refused" 1>&2
  exit 255
fi
if [ "$mode" = "authfail" ]; then
  echo "routeradmin@192.0.2.1: Permission denied (password)." 1>&2
  exit 255
fi

case "$cmd" in
  *"printf abc"*)       echo "SHA256(stdin)= $KAT"; exit 0 ;;
  *"for t in dd"*)
      case "$mode" in
        tools_fail)      echo "probe blew up" 1>&2; exit 2 ;;
        tools_missing)   printf 'TOOLS_MISSING: sha256sum od\n'; exit 0 ;;
        tools_malformed) printf 'WOBBLE\n'; exit 0 ;;
        *)               printf 'TOOLS_OK\n'; exit 0 ;;
      esac ;;
  *"mkdir -m 700"*)     exit 0 ;;
  *"cat > "*)
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
        *)              sleep "$bound"; echo "selftest_exit=137"; echo "selftest_wd_fired=1"; exit 0 ;;
      esac ;;
  *"control-exec-ok"*)  echo control-exec-ok; exit 0 ;;
  *"out.json"*)
      case "$mode" in
        timeout|timeout_cleanup_fail) exit 137 ;;
        reporter_fail)                echo "reporter boom" 1>&2; exit 1 ;;
        *)                            cat "${FAKE_SSH_DOC:?FAKE_SSH_DOC not set}"; exit 0 ;;
      esac ;;
  *"rm -rf"*)
      case "$mode" in
        cleanup_fail|timeout_cleanup_fail) echo REMOVAL_FAILED 1>&2; exit 1 ;;
        *)                                 echo REMOVED; exit 0 ;;
      esac ;;
esac
exit 0
