#!/usr/bin/env bash
# One command to run the SFG transfer tracker.
#   ./run.sh          set up (first time only) and start on http://127.0.0.1:5050
#   ./run.sh reset    rebuild the database with fresh demo data, then start
#   ./run.sh test     run the automated checks
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.10 or newer is needed."; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing Flask..."
  "$PY" -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
fi

case "${1:-}" in
  reset) .venv/bin/python -m tracker.seed ;;
  test)
    .venv/bin/pip install -q --disable-pip-version-check -r requirements-dev.txt
    exec .venv/bin/python -m pytest -q tests ;;
esac

echo "SFG Transfer Tracker running at http://127.0.0.1:${PORT:-5050}  (Ctrl+C to stop)"
exec .venv/bin/python app.py
