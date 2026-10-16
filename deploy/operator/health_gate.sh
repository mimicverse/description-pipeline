#!/usr/bin/env bash
# The Linux health gate that guards the platform admission open (promote's verify step and the
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

# Monotonic time in milliseconds: /proc/uptime never jumps backwards.
now_ms() { awk '{printf "%d", $1 * 1000}' /proc/uptime; }
to_ms() { awk -v s="$1" 'BEGIN{printf "%d", s * 1000}'; }
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
  remaining_ms=$((deadline_ms - now))
  remaining_s="$(awk -v ms="$remaining_ms" 'BEGIN{printf "%.3f", ms / 1000}')"
  # The single probe may never outrun the remaining total budget, and a probe that ignores the
  # polite stop signal is force-finished within the same budget (kill-after below).
  probe_s="$(min_s "$PER_PROBE_S" "$remaining_s")"
  kill_after_s="$(awk -v r="$remaining_s" 'BEGIN{ x = r < 5 ? r : 5; if (x < 0.05) x = 0.05; printf "%.3f", x }')"
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
