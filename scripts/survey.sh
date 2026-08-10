#!/bin/bash
# Snapshot what's eating space + where the clutter is. Read-only.
echo "=== DISK ==="; df -h / | sed -n '1p;/disk/p'
echo ""; echo "=== HOME top-level ==="; du -sh ~/Documents ~/Downloads ~/Desktop ~/Movies ~/Pictures ~/Library 2>/dev/null | sort -rh
echo ""; echo "=== Documents/work (biggest repos) ==="; du -sh ~/Documents/work/*/ 2>/dev/null | sort -rh | head -15
echo ""; echo "=== node_modules (regenerable) ==="; find ~/Documents ~/Desktop -type d -name node_modules -prune 2>/dev/null -exec du -sk {} + | awk '{s+=$1} END{printf "%.1f GB across %d dirs\n",s/1048576,NR}'
echo ""; echo "=== Downloads by type ==="; find ~/Downloads -maxdepth 1 -type f 2>/dev/null | sed 's/.*\.//' | tr A-Z a-z | sort | uniq -c | sort -rn | head -12
echo ""; echo "=== screenshots across home ==="; find ~/Desktop ~/Downloads ~/Documents ~/Pictures -maxdepth 3 -iname 'screenshot*' 2>/dev/null | wc -l
