#!/bin/bash
# Chunked, range-based fetch of Parakeet weights.
#   usage: fetch_model.sh <repo> <dir> <expected_bytes> <sha256>
#
# curl -C - is not safe here: the Hugging Face redirect lands on a signed CDN
# URL that expires mid-transfer, after which the CDN answers a resume request
# with 200 instead of 206 and curl silently restarts from byte zero (observed
# at 49% on this link). Explicit byte ranges appended only on an exact length
# match cannot do that.
set -u
REPO="$1"; DIR="$2"; EXPECTED="$3"; SHA="$4"
URL="https://huggingface.co/$REPO/resolve/main/model.safetensors"
F="$DIR/model.safetensors"
CHUNK=$((32*1024*1024))
mkdir -p "$DIR"
fail=0
while :; do
  sz=$(stat -f%z "$F" 2>/dev/null || echo 0)
  [ "$sz" -ge "$EXPECTED" ] && break
  end=$((sz + CHUNK - 1)); [ "$end" -ge "$EXPECTED" ] && end=$((EXPECTED - 1))
  want=$((end - sz + 1))
  curl -sSL -r "${sz}-${end}" --retry 15 --retry-delay 3 --retry-all-errors \
       --speed-limit 500 --speed-time 45 --max-time 900 -o "$F.tmp" "$URL"
  got=$(stat -f%z "$F.tmp" 2>/dev/null || echo 0)
  if [ "$got" -eq "$want" ]; then
    cat "$F.tmp" >> "$F"; fail=0
    printf "%d%% (%d MB)\n" $(( (sz+got)*100/EXPECTED )) $(( (sz+got)/1048576 ))
  else
    fail=$((fail+1)); echo "chunk at $sz got $got/$want (fail $fail)"
    [ "$fail" -ge 40 ] && { echo "GAVE UP"; exit 1; }
    sleep 3
  fi
done
rm -f "$F.tmp"
echo "verifying sha256..."
actual=$(shasum -a 256 "$F" | cut -d' ' -f1)
[ "$actual" = "$SHA" ] && echo "MODEL COMPLETE AND VERIFIED" || { echo "CHECKSUM MISMATCH: $actual"; exit 1; }
