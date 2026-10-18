#!/usr/bin/env bash
# Copy Telegram / healthcheck settings from another project's .env into this project's .env.
# Values are never printed; only key names are shown.
#   scripts/import_telegram_env.sh /Users/soohan/Desktop/claude/test1/myStockApp-scanner-vm/.env
set -euo pipefail
src="${1:?usage: $0 /path/to/other/.env}"
cd "$(dirname "$0")/.."
dst=.env
[ -f "$dst" ] || cp .env.example "$dst"
chmod 600 "$dst"
added=0
while IFS= read -r line || [ -n "$line" ]; do
  line="${line#export }"
  key="${line%%=*}"
  [[ "$line" == *=* ]] || continue
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  [[ "$key" =~ (TELEGRAM|^TG_|BOT_TOKEN|CHAT_ID|HEALTHCHECK) ]] || continue
  if grep -qE "^${key}=.+" "$dst"; then
    echo "skip (already set): $key"; continue
  fi
  grep -vE "^${key}=" "$dst" > "$dst.tmp" || true
  printf '%s\n' "$line" >> "$dst.tmp"
  mv "$dst.tmp" "$dst"; chmod 600 "$dst"
  echo "imported: $key"; added=$((added+1))
done < "$src"
echo "done: $added key(s). Test with: docker compose -f docker/compose.yaml run --rm paper notify-test"
