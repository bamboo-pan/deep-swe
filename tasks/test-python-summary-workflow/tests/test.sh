#!/bin/bash
set -uo pipefail
trap 'if [ ! -f /logs/verifier/reward.json ] && [ ! -f /logs/verifier/reward.txt ]; then mkdir -p /logs/verifier; echo -1 > /logs/verifier/reward.txt; fi' EXIT
cd /app || { mkdir -p /logs/verifier; exit 6; }

python3 /tests/grader.py prepare || exit $?
[ -f /logs/verifier/reward.json ] && exit 0

export RUN_LOG=/logs/verifier/run.log
: > "$RUN_LOG"
set +e
python -m pytest -q tests/test_stats_existing.py tests/test_task_summary.py \
  --junitxml=/logs/verifier/pytest.xml 2>&1 | tee -a "$RUN_LOG"
pytest_status=${PIPESTATUS[0]}
set -e
echo "[verifier] pytest exit status: $pytest_status" | tee -a "$RUN_LOG"

echo "===== raw suite output: run.log ====="
cat "$RUN_LOG"
echo "===== grade ====="
python3 /tests/grader.py grade
echo "[verifier] reward.json=$(cat /logs/verifier/reward.json 2>/dev/null)"

mkdir -p /logs/verifier/reports
for file in /logs/verifier/*; do
  case "${file##*/}" in
    reward.json|reward.txt|ctrf.json|run.log|test-stdout.txt|reports) continue ;;
  esac
  [ -f "$file" ] && mv -f "$file" /logs/verifier/reports/
done
