#!/bin/sh
# Full verification for pi graph. Run before pushing or cutting a release.
#
#   ./scripts/check.sh
#
# Every step is a hard gate: the first failure stops the run with a non-zero
# exit. Set PI_GRAPH_PYTHON to pick an interpreter.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if [ -n "${PI_GRAPH_PYTHON:-}" ]; then
  python_bin=$PI_GRAPH_PYTHON
elif [ -x .venv/bin/python ]; then
  python_bin=.venv/bin/python
else
  python_bin=python3
fi

pass=0
step() {
  printf '\n\033[1m==> %s\033[0m\n' "$1"
  shift
  "$@"
  pass=$((pass + 1))
}

"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  printf 'pi graph needs Python 3.10 or newer (%s)\n' \
    "$("$python_bin" -c 'import sys; print(sys.version.split()[0])')" >&2
  exit 1
}

step "Unit tests"            "$python_bin" -m unittest discover -s tests
step "Pi extension tests"    npm run --silent test:extension
step "Typecheck"             npm run --silent check
step "Validate every example" "$python_bin" scripts/run_example_suite.py --validate-only

step "Schema exposes the full node contract" sh -c \
  './bin/piw schema --json | grep -q "\"nodeKinds\""'

step "A workflow validates" sh -c \
  'PI_GRAPH_ROOTS="$PWD/examples/workflows" ./bin/piw validate examples/workflows/01-hello-command/steps.yaml >/dev/null'

step "A workflow runs end to end" sh -c \
  'PI_GRAPH_ROOTS="$PWD/examples/workflows" ./bin/piw run examples/workflows/01-hello-command/steps.yaml --input Ada >/dev/null'

# Regression guards for two bugs that shipped once and were invisible to the
# suite: a swallowed runner message, and a Studio that trusted the Host header.
step "A missing input fails with an actionable message" sh -c '
  output=$(PI_GRAPH_ROOTS="$PWD/examples/workflows" ./bin/piw run \
    examples/workflows/01-hello-command/steps.yaml --json 2>&1 || true)
  echo "$output" | grep -q "requires --input" || {
    echo "runner error message was swallowed" >&2; exit 1; }'

step "Studio rejects a rebound Host header" "$python_bin" -m unittest tests.test_workflow_ui

printf '\n\033[32mAll %s checks passed.\033[0m\n' "$pass"
