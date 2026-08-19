#!/bin/bash
# Chunked, range-based fetch for a "url|expected_size" list.
# Appends only on an exact chunk-length match, so a CDN that answers a resume
# request with 200 instead of 206 can never silently restart or duplicate data.
set -u
LIST="$1"; DEST="$2"
PY=/opt/homebrew/bin/python3.12
CHUNK=$((16*1024*1024))
while IFS='|' read -r url expected; do
  [ -z "$url" ] && continue
  f="$DEST/$(basename "$url")"
  [ -f "$f.done" ] && continue
  fail=0
  while :; do
    sz=$(stat -f%z "$f" 2>/dev/null || echo 0)
    if [ "$sz" -ge "$expected" ]; then break; fi
    end=$((sz + CHUNK - 1)); [ "$end" -ge "$expected" ] && end=$((expected - 1))
    want=$((end - sz + 1))
    curl -sSL -r "${sz}-${end}" --retry 15 --retry-delay 3 --retry-all-errors \
         --speed-limit 500 --speed-time 45 --max-time 900 -o "$f.tmp" "$url"
    got=$(stat -f%z "$f.tmp" 2>/dev/null || echo 0)
    if [ "$got" -eq "$want" ]; then
      cat "$f.tmp" >> "$f"; fail=0
      echo "$(basename $url): $(( (sz+got)*100/expected ))%"
    else
      fail=$((fail+1)); echo "$(basename $url): chunk $sz got $got/$want (fail $fail)"
      [ "$fail" -ge 30 ] && break
      sleep 3
    fi
  done
  rm -f "$f.tmp"
  if "$PY" -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) and zipfile.ZipFile(sys.argv[1]).testzip() is None else 1)" "$f" 2>/dev/null; then
    touch "$f.done"; echo "$(basename $url): VERIFIED"
  else
    echo "$(basename $url): CORRUPT"; rm -f "$f"
  fi
done < "$LIST"
echo "RANGED FETCH DONE"
