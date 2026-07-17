#!/usr/bin/env bash
# Run this ON THE SERVER (inside the deployed container / a platform shell),
# after copying an export_data.sh tarball there. Extracts it into DATA_DIR
# (typically /data), overwriting any matching files already present.
set -euo pipefail

TARBALL="${1:-}"
if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
  echo "Usage: import_data.sh <path-to-closet-data-*.tar.gz>"
  exit 1
fi

TARGET="${DATA_DIR:-/data}"
mkdir -p "$TARGET"
tar xzf "$TARBALL" -C "$TARGET"
echo "Extracted $TARBALL into $TARGET"
echo "Restart the app so it picks up the restored DB/index."
