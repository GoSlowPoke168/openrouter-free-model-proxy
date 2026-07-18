#!/usr/bin/env bash
# openrouter-free-model-proxy · run the proxy server
# Portable: resolves its own directory, so it works wherever the repo lives.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/venv/bin/python3" "$DIR/proxy.py"
