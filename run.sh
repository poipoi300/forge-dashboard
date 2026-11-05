#!/usr/bin/env bash
# Launch the forge dashboard: make sure uv and Bun exist, then hand over to
# the TUI runner, which installs dependencies, builds the UI, starts the API,
# and opens the browser. Arguments are forwarded (try --help).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v bun >/dev/null 2>&1; then
  echo "Installing Bun..."
  curl -fsSL https://bun.sh/install | bash
  export PATH="$HOME/.bun/bin:$PATH"
fi

status=0
uv run -m forge_dashboard.runner "$@" || status=$?
if [ "$status" -ne 0 ] && [ -t 0 ]; then
  read -rp "The runner exited with code $status. Press Enter to close. "
fi
exit "$status"
