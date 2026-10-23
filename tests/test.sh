#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="$LOG_DIR/reward.txt"
STATUS_FILE="$LOG_DIR/verification-status.txt"
TEST_LOG="$LOG_DIR/test-stdout.txt"
REPORT="$LOG_DIR/pytest-results.xml"
SCORE_REPORT="$LOG_DIR/scoring-report.json"
WHEEL_DIR=/tests/wheels
completed=0

mkdir -p "$LOG_DIR"

write_reward() {
  local value="$1"
  chmod 0644 /logs/verifier/reward.txt 2>/dev/null || true
  printf '%s\n' "$value" > /logs/verifier/reward.txt
  chmod 0444 /logs/verifier/reward.txt
}

write_status() {
  chmod 0644 "$STATUS_FILE" 2>/dev/null || true
  printf '%s\n' "$1" > "$STATUS_FILE"
  chmod 0444 "$STATUS_FILE"
}

on_exit() {
  if [ "$completed" -ne 1 ]; then
    write_status "INCOMPLETE: verifier exited before scoring completed"
    write_reward "0.000"
  fi
}

on_signal() {
  write_status "INCOMPLETE: verifier interrupted before scoring completed"
  write_reward "0.000"
  completed=1
  exit 0
}

write_reward "0.000"
write_status "INCOMPLETE: pytest has not completed"
trap on_exit EXIT
trap on_signal INT TERM HUP

rm -f "$REPORT" "$TEST_LOG" "$SCORE_REPORT"

if ! python3 -m pip install --disable-pip-version-check --no-index \
  --find-links "$WHEEL_DIR" pytest==8.4.1 >/dev/null 2>&1; then
  write_status "INCOMPLETE: verifier dependency installation failed"
  write_reward "0.000"
  completed=1
  exit 0
fi

set +e
timeout --signal=TERM --kill-after=8s 110s \
  env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python3 -u -m pytest -v -c /dev/null -p no:cacheprovider \
  --junit-prefix=test_outputs --junitxml="$REPORT" \
  /tests/test_outputs.py > "$TEST_LOG" 2>&1
RC=$?
set -e

cat "$TEST_LOG" 2>/dev/null || true

if [ "$RC" -ne 0 ] && [ "$RC" -ne 1 ]; then
  write_status "INCOMPLETE: pytest did not complete; runner exited with code $RC"
  write_reward "0.000"
elif [ ! -s "$REPORT" ]; then
  write_status "INCOMPLETE: pytest completion report missing or empty"
  write_reward "0.000"
elif ! python3 /tests/score.py \
  --config /tests/scoring.json \
  --junit "$REPORT" \
  --report-out "$SCORE_REPORT" \
  --reward-out "$REWARD_FILE"; then
  write_status "INCOMPLETE: weighted scorer rejected the JUnit report"
  write_reward "0.000"
else
  REWARD=$(cat "$REWARD_FILE")
  case "$REWARD" in
    0.[0-9][0-9][0-9]|1.000) ;;
    *)
      write_status "INCOMPLETE: weighted scorer produced an invalid reward"
      write_reward "0.000"
      completed=1
      exit 0
      ;;
  esac
  chmod 0444 "$REWARD_FILE"
  write_status "COMPLETED: weighted scoring finished with reward=$REWARD"
fi

completed=1
exit 0
