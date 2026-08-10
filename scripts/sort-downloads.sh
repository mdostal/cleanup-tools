#!/bin/bash
# Stage a messy folder into _sorted/<type>/ buckets by extension. NON-destructive (mv within the folder).
# You then review each bucket and bulk-act. Usage: ./sort-downloads.sh [~/Downloads]  (--dry to preview)
DIR="${1:-$HOME/Downloads}"; DRY=""; [ "$2" = "--dry" ] && DRY=1
bucket(){ case "$1" in
  png|jpg|jpeg|heic|gif|webp|tiff) [[ "$2" == screenshot* ]] && echo screenshots || echo photos ;;
  dmg|pkg) echo installers ;; mp4|mov|m4v|avi|mkv) echo videos ;;
  pdf) echo pdfs ;; zip|tar|gz|7z|rar) echo archives ;;
  csv|json|xlsx|xml|tsv|numbers) echo data ;; doc|docx|ppt|pptx|txt|md|pages|key) echo docs ;;
  *) echo other ;; esac; }
find "$DIR" -maxdepth 1 -type f ! -name '.*' | while read -r f; do
  b=$(basename "$f"); ext="${b##*.}"; ext=$(echo "$ext" | tr A-Z a-z)
  d="$DIR/_sorted/$(bucket "$ext" "$(echo "$b"|tr A-Z a-z)")"
  if [ -n "$DRY" ]; then echo "$b -> $(basename "$d")/"; else mkdir -p "$d"; mv "$f" "$d/"; fi
done
[ -z "$DRY" ] && echo "staged into $DIR/_sorted/ — review each bucket, then bulk-act"
