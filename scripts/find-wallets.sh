#!/bin/bash
# Find crypto-wallet artifacts on YOUR machine. Prints candidate PATHS only — never the contents/keys.
# Review the paths yourself. Usage: ./find-wallets.sh [~/  default search root]
ROOT="${1:-$HOME}"
echo "=== by filename ==="
find "$ROOT" -type f 2>/dev/null \( \
  -iname 'wallet.dat' -o -iname '*.wallet' -o -iname 'keystore*' -o -iname 'UTC--*' \
  -o -iname '*.kdbx' -o -iname 'electrum*' -o -iname '*mnemonic*' -o -iname '*seed*phrase*' \
  -o -iname '*recovery*phrase*' -o -iname '*.keychain' -o -iname 'default_wallet' \
  -o -iname 'metamask*' -o -iname 'exodus*' -o -iname 'atomic*wallet*' \) \
  ! -path '*/node_modules/*' ! -path '*/Library/Caches/*' | head -200
echo ""; echo "=== by content (seed phrases / private keys / eth keystore) — paths only ==="
grep -rlIE '(\b[a-z]+ ){11,23}[a-z]+\b|xprv[0-9A-Za-z]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"crypto"\s*:\s*\{[^}]*"cipher"' \
  "$ROOT/Documents" "$ROOT/Desktop" "$ROOT/Downloads" 2>/dev/null \
  --include='*.txt' --include='*.json' --include='*.md' --include='*.rtf' --include='*.csv' \
  --exclude-dir=node_modules --exclude-dir=.git | head -100
echo ""; echo "NOTE: these are CANDIDATES. Open them yourself. This script never prints or sends key material."
