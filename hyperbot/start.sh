#!/usr/bin/env bash
# ===================================================================
#  hyperbot - one-click start for macOS / Linux.
#  Run:  ./start.sh      (or  bash start.sh )
#  Installs what is missing, then opens the dashboard in a browser.
# ===================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  hyperbot - Hyperliquid copy desk"
echo "  ================================"
echo

# --- find Python ---------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "  [X] Python 3 is not installed, or not on your PATH."
  echo "      macOS:  brew install python3"
  echo "      Linux:  sudo apt install python3 python3-pip"
  exit 1
fi
echo "  [1/3] Using Python: $($PY --version)"

# --- dependencies --------------------------------------------------
echo "  [2/3] Checking dependencies..."
if ! $PY -c "import httpx, websockets, yaml, aiohttp" >/dev/null 2>&1; then
  echo "        Installing (first run only, takes a minute)..."
  # Newer distros protect the system Python; fall back to --user, then to the
  # explicit override, so this works on Debian/Ubuntu and macOS alike.
  $PY -m pip install --quiet -r requirements.txt \
    || $PY -m pip install --quiet --user -r requirements.txt \
    || $PY -m pip install --quiet --break-system-packages -r requirements.txt \
    || { echo "  [X] Install failed. Run this by hand to see why:"; \
         echo "      $PY -m pip install -r requirements.txt"; exit 1; }
fi

# --- config --------------------------------------------------------
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "        Created config.yaml from the example."; }
[ -f .env ] || { cp .env.example .env; echo "        Created .env - add your account address there later."; }

# --- go ------------------------------------------------------------
echo "  [3/3] Starting the dashboard..."
echo
echo "  =========================================================="
echo "    Opening  http://localhost:8730"
echo "    This terminal must STAY OPEN - it is the server."
echo "    Press Ctrl+C to stop."
echo "  =========================================================="
echo
exec $PY run.py serve
