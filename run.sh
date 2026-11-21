#!/usr/bin/env bash
# Check for uv and Bun, then launch the TUI runner. Put --install-tools first
# to opt into downloading missing tools. Runner arguments follow (try --help).
set -euo pipefail
cd "$(dirname "$0")"

install_tools=false
if [[ "${1:-}" == "--install-tools" ]]; then
  install_tools=true
  shift
fi

ensure_tool() {
  local name=$1 installer=$2 shell=$3 bin_dir=$4
  if command -v "$name" >/dev/null 2>&1; then return; fi
  if [[ "$install_tools" == false ]]; then
    printf '%s is required but missing from PATH. Install it with your package manager.\n' "$name" >&2
    printf 'On Arch, add the %s package to your arcus module.\n' "$name" >&2
    printf 'To use the upstream installer, run ./run.sh --install-tools.\n' >&2
    exit 1
  fi

  echo "Installing $name..."
  curl -fsSL "$installer" | "$shell"
  export PATH="$HOME/$bin_dir:$PATH"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf '%s is still missing from PATH after running its installer.\n' "$name" >&2
    exit 1
  fi
}

ensure_tool uv https://astral.sh/uv/install.sh sh .local/bin
ensure_tool bun https://bun.sh/install bash .bun/bin

status=0
uv run -m forge_dashboard.runner "$@" || status=$?
if [ "$status" -ne 0 ] && [ -t 0 ]; then
  read -rp "The runner exited with code $status. Press Enter to close. "
fi
exit "$status"
