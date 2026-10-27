#!/usr/bin/env bash
# The Linux health gate that guards the platform admission open (start verification and the
# plain start).  One bounded waiting loop around the deployment's own full health.sh (HTTP
# included): a failing or late probe never opens; every attempt's output is preserved, and every
# invocation gets its own evidence run directory (money/inspectable after the fact).
#   usage: health_gate.sh <evidence-dir> [deadline-seconds] [probe-timeout-seconds] [interval-seconds]
# The trailing arguments are the explicit internal path used by tests; the lifecycle calls the
# gate with the evidence directory only, so the fixed policy below cannot be changed by any
# inherited environment (no config surface, no policy environment variables).
set -uo pipefail
EVIDENCE_DIR="${1:?usage: health_gate.sh <evidence-dir> [deadline-seconds] [probe-timeout-seconds] [interval-seconds]}"
DEADLINE_S="${2:-180}"
PER_PROBE_S="${3:-60}"
INTERVAL_S="${4:-5}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$HERE/health.sh"

mkdir -p "$EVIDENCE_DIR"
RUN_DIR="$(mktemp -d "$EVIDENCE_DIR/run-XXXXXXXX")"
SUMMARY="$RUN_DIR/summary.txt"
mkdir -p "$RUN_DIR/attempts"

# Monotonic time in milliseconds, in bash 64-bit arithmetic: /proc/uptime never jumps backwards
# and mawk's %d saturates at 32 bits, so no awk ever touches the millisecond arithmetic.
uptime_ms() {
  local int frac
  int="${1%%.*}"
  frac="${1#*.}"
  [ "$frac" = "$1" ] && frac=""
  case ${#frac} in
    0) frac=000 ;;
    1) frac="${frac}00" ;;
    2) frac="${frac}0" ;;
    3) : ;;
    *) frac="${frac:0:3}" ;;
  esac
  printf '%d' $(( int * 1000 + 10#$frac ))
}
now_ms() { local up _; read -r up _ < /proc/uptime; uptime_ms "$up"; }
to_ms() { uptime_ms "$1"; }
min_s() { awk -v a="$1" -v b="$2" 'BEGIN{ x = (a < b) ? a : b; if (x < 0.001) x = 0.001; printf "%.3f", x }'; }

start_ms="$(now_ms)"
deadline_ms=$((start_ms + $(to_ms "$DEADLINE_S")))
attempt=0
while :; do
  attempt=$((attempt + 1))
  now="$(now_ms)"
  if [ "$now" -ge "$deadline_ms" ]; then
    echo "linux_health_gate=FAIL run=$RUN_DIR attempts=$((attempt - 1)) elapsed_ms=$((now - start_ms)) reason=deadline-reached" \
      | tee -a "$SUMMARY" >&2
    exit 1
  fi
  now="$(now_ms)"  # fresh: the start overhead is already spent from the same budget
  remaining_ms=$((deadline_ms - now))
  remaining_s="$(awk -v ms="$remaining_ms" 'BEGIN{printf "%.3f", ms / 1000}')"
  # The polite stop and the forced finish share the one remaining budget: kill_after is small
  # and bounded, and the probe timeout is what remains after reserving it (probe + kill <= rem).
  kill_after_s="$(awk -v r="$remaining_s" 'BEGIN{ x = r / 4; if (x > 1) x = 1; if (x < 0.05) x = 0.05; printf "%.3f", x }')"
  probe_s="$(awk -v per="$PER_PROBE_S" -v rem="$remaining_s" -v k="$kill_after_s" 'BEGIN{ t = rem - k; if (t > per) t = per; if (t < 0.001) t = 0.001; printf "%.3f", t }')"
  out="$RUN_DIR/attempts/attempt-$attempt.out"
  rc=0
  OPERATOR_STATE="${OPERATOR_STATE:-}" timeout -k "$kill_after_s" "$probe_s" bash "$PROBE" >"$out" 2>&1 || rc=$?
  if [ "$rc" = 0 ]; then
    now2="$(now_ms)"
    if [ "$now2" -gt "$deadline_ms" ]; then
      # A success after the deadline must never open the admission (nothing is backfilled).
      echo "linux_health_gate=FAIL run=$RUN_DIR attempts=$attempt elapsed_ms=$((now2 - start_ms)) reason=success-after-deadline" \
        | tee -a "$SUMMARY" >&2
      exit 1
    fi
    echo "linux_health_gate=PASS run=$RUN_DIR attempts=$attempt elapsed_ms=$((now2 - start_ms))" | tee -a "$SUMMARY"
    exit 0
  fi
  reason="probe-failed"
  [ "$rc" = 124 ] && reason="probe-timeout"
  [ "$rc" = 137 ] && reason="probe-killed"
  echo "attempt=$attempt rc=$rc reason=$reason at_ms=$(( $(now_ms) - start_ms )) out=$out" >>"$SUMMARY"
  now="$(now_ms)"
  if [ "$now" -ge "$deadline_ms" ]; then
    echo "linux_health_gate=FAIL run=$RUN_DIR attempts=$attempt elapsed_ms=$((now - start_ms)) reason=deadline-reached (last=$reason)" \
      | tee -a "$SUMMARY" >&2
    exit 1
  fi
  sleep_s="$(min_s "$INTERVAL_S" "$(awk -v ms="$((deadline_ms - now))" 'BEGIN{printf "%.3f", ms / 1000}')")"
  sleep "$sleep_s"
done
