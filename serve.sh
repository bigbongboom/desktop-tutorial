#!/usr/bin/env bash
# Serve the dashboards locally.
#
#   ./serve.sh          -> http://localhost:8765/sweep/
#   ./serve.sh 9000     -> a port of your choosing
#
# Use this rather than double-clicking the file: opening it as file:// gives the
# page a null origin, and some browsers then refuse the exchange API calls that
# the live price feed depends on.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8765}"
URL="http://localhost:${PORT}/sweep/"

if command -v python3 >/dev/null 2>&1; then
  SERVE=(python3 -m http.server "$PORT")
elif command -v python >/dev/null 2>&1; then
  SERVE=(python -m SimpleHTTPServer "$PORT")
elif command -v npx >/dev/null 2>&1; then
  SERVE=(npx --yes http-server -p "$PORT")
else
  echo "Need python3, python, or npx to serve. Install one and retry." >&2
  exit 1
fi

echo "ETH Sweep Desk  ->  ${URL}"
echo "Signal desk     ->  http://localhost:${PORT}/"
echo "Ctrl-C to stop."
echo

# Open a browser if the platform offers an opener; never fail the run over it.
( sleep 1
  if command -v open        >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open  >/dev/null 2>&1; then xdg-open "$URL"
  fi ) >/dev/null 2>&1 &

exec "${SERVE[@]}"
