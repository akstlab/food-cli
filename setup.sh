#!/usr/bin/env bash
# One-shot setup for food-cli. Idempotent - safe to re-run to update.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it with:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> Creating virtualenv and installing dependencies"
uv sync

echo
echo "==> Done. The 'food' command lives in .venv/bin/food"
echo
echo "Run it either way:"
echo "  uv run food --help"
echo "  source .venv/bin/activate && food --help"
echo
if uv run food auth status 2>/dev/null | grep -q '"authorized": true'; then
  echo "==> Already signed in to Swiggy."
else
  echo "==> Next: sign in (you authenticate in your own browser)"
  echo "     uv run food auth url --server food"
fi
