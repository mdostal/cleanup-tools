#!/bin/bash
# List duplicate files (same size + hash) so you can reclaim. Read-only (prints groups). Usage: ./dedupe.sh [dir]
DIR="${1:-$HOME/Downloads}"
find "$DIR" -type f ! -path '*/node_modules/*' -exec sh -c 'echo "$(stat -f%z "$1") $1"' _ {} \; 2>/dev/null \
 | sort -n | awk '{sz=$1; $1=""; f=substr($0,2); if(sz==p){print pf; print f} p=sz; pf=f}' \
 | sort -u | while read -r f; do echo "$(shasum "$f" 2>/dev/null | cut -c1-12)  $f"; done | sort | awk '{if($1==h)print ph"\n"$0; h=$1; ph=$0}'
