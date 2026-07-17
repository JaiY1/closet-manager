#!/usr/bin/env bash
# Package the local closet (DB, vector index, images, caches) into a single
# tarball so it can be moved onto a UAT deployment's mounted volume without
# re-adding garments through the UI. Run this from the project root.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_DIR=$(python3 -c "from config import DATA_DIR; print(DATA_DIR)")
OUT="closet-data-$(date +%Y%m%d-%H%M%S).tar.gz"

ITEMS=()
for name in closet.db chroma_db uploads .image_cache .tryon_cache.json; do
  [ -e "$DATA_DIR/$name" ] && ITEMS+=("$name")
done

if [ ${#ITEMS[@]} -eq 0 ]; then
  echo "Nothing found under $DATA_DIR — is the app running from the right directory?"
  exit 1
fi

tar czf "$OUT" -C "$DATA_DIR" "${ITEMS[@]}"
echo "Exported: ${ITEMS[*]}"
echo "-> $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "Next: copy this file onto your UAT server's volume and run:"
echo "  tar xzf $(basename "$OUT") -C \$DATA_DIR"
echo "See DEPLOY.md > 'Migrating your local closet to UAT' for platform-specific copy steps."
