#!/usr/bin/env bash
# openrouter-free-model-proxy installer.
#
# One-liner (installs to ~/openrouter-free-model-proxy and runs it as a
# background systemd --user service):
#
#   curl -fsSL https://raw.githubusercontent.com/jeremyhou/openrouter-free-model-proxy/main/install.sh | bash
#
# Env overrides:
#   DEST=/path        where to install        (default: ~/openrouter-free-model-proxy)
#   REPO=<git url>    source repo             (default: the GitHub repo above)
#   NO_SERVICE=1      skip the systemd service (just set up the venv)
set -euo pipefail

REPO="${REPO:-https://github.com/jeremyhou/openrouter-free-model-proxy}"
DEST="${DEST:-$HOME/openrouter-free-model-proxy}"

command -v python3 >/dev/null || { echo "error: python3 is required"; exit 1; }
command -v git      >/dev/null || { echo "error: git is required";     exit 1; }

# 1. Fetch (clone fresh, or update in place if already there).
if [ -d "$DEST/.git" ]; then
  echo ">> updating $DEST"
  git -C "$DEST" pull --ff-only
else
  echo ">> cloning $REPO -> $DEST"
  git clone --depth 1 "$REPO" "$DEST"
fi

# 2. Virtualenv + deps.
cd "$DEST"
echo ">> creating venv + installing deps"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

# 3. Background service (unless opted out).
if [ "${NO_SERVICE:-0}" = "1" ]; then
  echo ">> NO_SERVICE=1 — skipping systemd service."
  echo ">> start it yourself with: $DEST/run_proxy.sh"
else
  echo ">> installing systemd --user service"
  ./install_service.sh
fi

echo
echo "Done. Proxy will listen on http://127.0.0.1:8787 (base_url .../v1)."
echo "Point any program there with model=\"auto\" and your own OpenRouter key."
