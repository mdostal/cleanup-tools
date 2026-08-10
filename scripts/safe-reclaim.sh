#!/bin/bash
# Delete regenerable/junk only. DRY-RUN by default. Run with --go to actually delete.
GO=""; [ "$1" = "--go" ] && GO=1
say(){ [ -n "$GO" ] && echo "DELETING: $1" || echo "would delete: $1"; }
# node_modules + build caches (rebuild with install/build)
find ~/Documents -type d \( -name node_modules -o -name .next -o -name .turbo -o -name dist \) -prune 2>/dev/null | while read -r d; do say "$d"; [ -n "$GO" ] && rm -rf "$d"; done
# junk
find ~/Documents ~/Desktop -maxdepth 3 \( -name '.DS_Store' -o -name '~$*' -o -name 'Thumbs.db' \) 2>/dev/null | while read -r f; do say "$f"; [ -n "$GO" ] && rm -f "$f"; done
for rb in ~/Desktop/'$RECYCLE.BIN' ~/Documents/'$RECYCLE.BIN'; do [ -e "$rb" ] && { say "$rb"; [ -n "$GO" ] && rm -rf "$rb"; }; done
# docker layers
if [ -n "$GO" ]; then docker system prune -af --volumes 2>/dev/null; else echo "would run: docker system prune -af --volumes"; fi
[ -z "$GO" ] && echo "--- DRY RUN. Re-run with --go to delete. ---"
