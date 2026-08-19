#!/bin/bash
# LiveTranslate -- double-click to start (spec section 10).
# Terminal equivalent:  ./LiveTranslate.command
cd "$(dirname "$0")" || exit 1
source scripts/bootstrap.sh
exec "$VENV/bin/python" -m livetranslate "$@"
