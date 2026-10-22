#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
CONTROL="$ROOT/deploy/operator/operatorctl.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export TMP

fail() {
  echo "test_bash51_function_capture: $*" >&2
  exit 1
}

bash_version="${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"
[ "$bash_version" = "5.1" ] \
  || fail "this regression must run under Bash 5.1, found $BASH_VERSION"

awk '
  /^read_output_path\(\) \{/ { copy = 1 }
  copy { print }
  copy && /^}/ { exit }
' "$CONTROL" > "$TMP/read-output-path.sh"
[ -s "$TMP/read-output-path.sh" ] || fail "read_output_path is missing"

cat > "$TMP/unsafe.sh" <<'UNSAFE'
#!/usr/bin/env bash
set -euo pipefail

lifecycle_success() {
  local first second
  first="$(
    cat <<'PAYLOAD' | tr '[:lower:]' '[:upper:]'
first payload
PAYLOAD
  )" || return 1
  second="$(
    cat <<'PAYLOAD' | tr '[:lower:]' '[:upper:]' \
      2>/dev/null | tail -1
second payload
PAYLOAD
  )" || return 1
  [ "$first" = "FIRST PAYLOAD" ]
  [ "$second" = "SECOND PAYLOAD" ]
  printf '%s\n' "$TMP/success-output.json"
}

unsafe="$(lifecycle_success)"
printf '%s\n' "$unsafe"
UNSAFE

set +e
unsafe_output="$(LC_ALL=C "$BASH" "$TMP/unsafe.sh" 2> "$TMP/unsafe.stderr")"
unsafe_rc=$?
set -e
[ "$unsafe_rc" -eq 1 ] || [ "$unsafe_rc" -eq 2 ] \
  || fail "the production here-document pattern did not fail with a Bash parser status"
[ -z "$unsafe_output" ] || fail "unsafe command substitution produced output"
grep -Fq 'unexpected EOF while looking for matching' "$TMP/unsafe.stderr" \
  || fail "unsafe command substitution did not reproduce the EOF parser error"
grep -Fq 'here-document' "$TMP/unsafe.stderr" \
  || fail "unsafe command substitution did not reproduce the here-document parser error"

cat > "$TMP/indirect.sh" <<'INDIRECT'
#!/usr/bin/env bash
set -euo pipefail

lifecycle_success() {
  local first second
  first="$(
    cat <<'PAYLOAD' | tr '[:lower:]' '[:upper:]'
first payload
PAYLOAD
  )" || return 1
  second="$(
    cat <<'PAYLOAD' | tr '[:lower:]' '[:upper:]' \
      2>/dev/null | tail -1
second payload
PAYLOAD
  )" || return 1
  [ "$first" = "FIRST PAYLOAD" ]
  [ "$second" = "SECOND PAYLOAD" ]
  printf '%s\n' "$TMP/success-output.json"
}

invoke() {
  "$@"
}

unsafe="$(invoke lifecycle_success)"
printf '%s\n' "$unsafe"
INDIRECT

set +e
indirect_output="$(LC_ALL=C "$BASH" "$TMP/indirect.sh" 2> "$TMP/indirect.stderr")"
indirect_rc=$?
set -e
[ "$indirect_rc" -eq 1 ] || [ "$indirect_rc" -eq 2 ] \
  || fail "indirect function invocation did not fail with a Bash parser status"
[ -z "$indirect_output" ] || fail "indirect function invocation produced output"
grep -Fq 'unexpected EOF while looking for matching' "$TMP/indirect.stderr" \
  || fail "indirect function invocation did not reproduce the parser error"

cat > "$TMP/safe.sh" <<'SAFE'
#!/usr/bin/env bash
set -euo pipefail
source "$TMP/read-output-path.sh"

lifecycle_success() {
  local state probe="$TMP/safe-probe.txt"
  cat > "$probe" <<'PAYLOAD'
second payload
PAYLOAD
  state="$(
    cat "$probe" | tr '[:lower:]' '[:upper:]' 2>/dev/null | tail -1
  )" || return 1
  [ "$state" = "SECOND PAYLOAD" ]
  printf '%s\n' "$TMP/success-output.json"
}

lifecycle_failure() {
  local verb="$1"
  local output="$TMP/$verb.out"
  printf '%s\n' "{\"verb\":\"$verb\",\"passed\":false}" > "$output"
  printf '%s\n' "stderr-$verb" > "$output.stderr"
  printf '%s\n' '{"status":"completed"}' > "$output.transport.json"
  printf '%s\n' '2' > "$output.ssh-rc"
  return 2
}

captured=""
lifecycle_success > "$TMP/success.path"
read_output_path "$TMP/success.path" captured
[ "$captured" = "$TMP/success-output.json" ]

printf 'one\ntwo\n' > "$TMP/multiline.path"
if read_output_path "$TMP/multiline.path" invalid; then
  exit 1
else
  [ "$?" -eq 2 ]
fi

for verb in stage promote health status rollback; do
  rc=0
  lifecycle_failure "$verb" > "$TMP/$verb.path" \
    || rc=$?
  [ "$rc" -eq 2 ]
  for suffix in '' '.stderr' '.transport.json' '.ssh-rc'; do
    [ -f "$TMP/$verb.out$suffix" ]
  done
done
printf 'safe capture PASS\n'
SAFE

"$BASH" "$TMP/safe.sh" > "$TMP/safe.stdout" 2> "$TMP/safe.stderr" \
  || fail "safe file-backed capture failed: $(cat "$TMP/safe.stderr")"
grep -Fq 'safe capture PASS' "$TMP/safe.stdout" \
  || fail "safe file-backed capture did not complete"

echo "Bash $BASH_VERSION function capture regression: PASS"
