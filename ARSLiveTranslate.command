#!/bin/bash
# LiveTranslate -- ARS training mode. Double-click to start.
#
# Identical to LiveTranslate.command except it loads config.ars.toml, which
# layers the ARS session vocabulary (SHOTOVER, ATOM, M2, PilotDisplay,
# Earthscape, IMU, gimbal, aircraft...) on top of the normal settings.
#
# Terminal equivalent:  ./ARSLiveTranslate.command
cd "$(dirname "$0")" || exit 1
source scripts/bootstrap.sh
exec "$VENV/bin/python" -m livetranslate --config config.ars.toml "$@"
