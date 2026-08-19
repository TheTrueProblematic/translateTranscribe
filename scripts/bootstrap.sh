#!/bin/bash
# Shared startup for LiveTranslate.command and ARSLiveTranslate.command.
# First run creates the virtualenv and installs dependencies; later runs skip
# straight to starting. Kept in one file so the two modes cannot drift apart.
set -u
VENV=".venv"
STAMP="$VENV/.deps-installed"

# Apple Silicon only. The default python3 on this machine may well be an Intel
# build, which cannot run MLX, so pick an arm64 interpreter explicitly.
pick_python() {
  for p in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 \
           /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
           /opt/homebrew/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1; then
      if "$p" -c 'import sys,platform; sys.exit(0 if platform.machine()=="arm64" and sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null; then
        echo "$p"; return 0
      fi
    fi
  done
  return 1
}

if [ ! -d "$VENV" ]; then
  PY=$(pick_python) || {
    echo "LiveTranslate needs an arm64 Python 3.10 or newer."
    echo "Install one with:  brew install python@3.12"
    read -r -p "Press Return to close."; exit 1
  }
  echo "First run: creating the environment (a few minutes)..."
  "$PY" -m venv "$VENV" || { echo "Could not create $VENV."; read -r -p "Press Return to close."; exit 1; }
fi

if [ ! -f "$STAMP" ]; then
  echo "First run: installing dependencies..."
  "$VENV/bin/python" -m pip install --upgrade pip --quiet
  if "$VENV/bin/python" -m pip install --quiet --timeout 60 --retries 20 -r requirements.txt; then
    touch "$STAMP"
  else
    echo
    echo "Dependency installation failed. Check the network and run this again."
    read -r -p "Press Return to close."; exit 1
  fi
fi
