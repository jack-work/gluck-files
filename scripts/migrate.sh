#!/usr/bin/env bash
# Move the pre-Garage file tree into the `files` bucket, and prove it arrived.
#
# Runs from the workstation. Two deliberate choices:
#
#   * It talks to Garage over an SSH tunnel to loopback, not through
#     Cloudflare. The tunnel has a 100 MB body cap and a 100 s origin timeout,
#     and a migration is exactly the moment you do not want to discover them.
#     Credentials never leave the laptop either way.
#
#   * It verifies by round-trip and diff, not by eyeball or object count. A
#     count tells you nothing about a truncated file. We pull every object back
#     down and compare bytes against the source.
#
# The source tree is left ALONE. It is the rollback.

set -euo pipefail

REMOTE=${REMOTE:-spain@spain}
SRC=${SRC:-/var/lib/gluck-files}
BUCKET=${BUCKET:-files}
PORT=${PORT:-3900}
HUSH_CMD=${HUSH_CMD:-files}

staging=$(mktemp -d)
verify=$(mktemp -d)
cleanup() {
  rm -rf "$staging" "$verify"
  [[ -n ${tunnel_pid:-} ]] && kill "$tunnel_pid" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> staging $REMOTE:$SRC"
scp -q -r "$REMOTE:$SRC/." "$staging/"
src_count=$(find "$staging" -type f | wc -l)
src_bytes=$(du -sb "$staging" | cut -f1)
echo "    $src_count files, $src_bytes bytes"

echo "==> opening tunnel to $REMOTE:127.0.0.1:$PORT"
ssh -N -L "$PORT:127.0.0.1:$PORT" "$REMOTE" &
tunnel_pid=$!
for _ in $(seq 1 30); do
  (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && break
  sleep 1
done

endpoint="http://127.0.0.1:$PORT"
aws() { hush "$HUSH_CMD" aws --endpoint-url "$endpoint" "$@"; }

echo "==> uploading to s3://$BUCKET/"
aws s3 sync "$staging/" "s3://$BUCKET/"

echo "==> pulling every object back for verification"
aws s3 sync "s3://$BUCKET/" "$verify/"

echo "==> diffing round-trip against source"
if diff -r "$staging" "$verify"; then
  dst_count=$(find "$verify" -type f | wc -l)
  echo
  echo "    OK: $dst_count/$src_count files identical, byte for byte"
else
  echo
  echo "    MISMATCH: the bucket does not match the source tree. $SRC is untouched;" >&2
  echo "    fix the difference before anyone starts trusting the bucket." >&2
  exit 1
fi

echo
echo "$SRC left in place as the rollback copy. Nothing serves it."
