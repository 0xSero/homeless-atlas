#!/usr/bin/env bash
# Publish the built site to Cloudflare Workers (static assets).
# Requires the deploy token at ~/.cache/cf-deploy-token (Workers Scripts:Edit, Workers Routes:Edit,
# DNS:Edit on sybilsolutions.ai). The site is served from Cloudflare's edge; nothing local is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
TOKEN_FILE="${CF_TOKEN_FILE:-$HOME/.cache/cf-deploy-token}"
[ -r "$TOKEN_FILE" ] || { echo "no deploy token at $TOKEN_FILE" >&2; exit 1; }
export CLOUDFLARE_API_TOKEN="$(cat "$TOKEN_FILE")"
export CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-e33ebf7e4962ed4bc7a7f97293e9f398}"

[ -d site ] || { echo "no site/ — run scripts/build_site.py first" >&2; exit 1; }
echo "files: $(find site -type f | wc -l)  size: $(du -sh site | cut -f1)"

wrangler deploy 2>&1 | tail -4

for p in / /index.html /map.html /state/CA.html /data/map-states.geojson; do
  code=$(curl -s -o /dev/null -m 30 -w '%{http_code}' "https://homeless.sybilsolutions.ai$p" || echo 000)
  printf '  %-28s %s\n' "$p" "$code"
  [ "$code" = "200" ] || { echo "publish check failed on $p" >&2; exit 1; }
done
echo "PUBLISHED https://homeless.sybilsolutions.ai"
