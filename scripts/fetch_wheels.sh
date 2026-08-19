#!/bin/bash
# Resumable, integrity-checked, parallel wheel fetcher.
# pip restarts a stalled download from zero; curl -C - resumes. PyPI throttles
# hard per connection on this link, so several run at once. Every file is
# verified as a valid zip before being marked done, so a truncated wheel can
# never reach pip install.
set -u
URLS="$1"; DEST="$2"; JOBS="${3:-6}"
PY=/opt/homebrew/bin/python3.12
export DEST PY

fetch_one() {
  url="$1"; f="$DEST/$(basename "$url")"
  [ -f "$f.done" ] && return 0
  curl -sSL -C - --retry 15 --retry-delay 2 --retry-all-errors \
       --speed-limit 500 --speed-time 45 --max-time 1200 -o "$f" "$url"
  if [ -f "$f" ] && "$PY" -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) and zipfile.ZipFile(sys.argv[1]).testzip() is None else 1)" "$f" 2>/dev/null; then
    touch "$f.done"; return 0
  fi
  return 1
}
export -f fetch_one

total=$(grep -c . "$URLS")
for pass in $(seq 1 60); do
  grep . "$URLS" | xargs -P "$JOBS" -I{} bash -c 'fetch_one "$@"' _ {} 2>/dev/null
  done_n=$(ls "$DEST"/*.done 2>/dev/null | wc -l | tr -d ' ')
  echo "pass $pass: $done_n/$total complete"
  [ "$done_n" -ge "$total" ] && { echo "ALL WHEELS FETCHED"; exit 0; }
  sleep 2
done
echo "GAVE UP"; exit 1
