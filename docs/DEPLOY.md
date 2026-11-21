# Deploying the static site

The build in `site/` is published to **Cloudflare Workers static assets** and served from
Cloudflare's edge. Nothing on this machine is in the request path: the local python server and the
tunnel are disabled, and the site still answers.

    https://homeless.sybilsolutions.ai        <- production host
    https://homeless-atlas.seroxdesign.workers.dev

## Publish a new build

```sh
python3 scripts/build_site.py     # atomic staging -> site swap
python3 scripts/crawl.py          # must print SITE OK
python3 scripts/verify.py         # must exit 0
./scripts/publish_cf.sh           # wrangler deploy + 5 live URL checks
```

`publish_cf.sh` reads a token from `~/.cache/cf-deploy-token` (an account API token, `cfat_…`, with Workers
Scripts:Edit; override with `CF_TOKEN_FILE`) and refuses to report success unless every
checked path returns 200.

## How the hostname is wired

| piece | value |
|---|---|
| worker | `homeless-atlas`, 41,929 static assets (Workers Paid plan, 100k file limit) |
| entry | `scripts/deploy-worker.js` — maps `/` to `index.html`, serves a 404 for unknown paths |
| binding | `assets.binding = "ASSETS"`, required: without it `env.ASSETS` is undefined and `/` plus every 404 path throw error 1101 |
| asset handling | `html_handling: "none"` — the build emits `.html` files and links to them explicitly |
| route | `homeless.sybilsolutions.ai/*` → worker `homeless-atlas` (zone sybilsolutions.ai) |
| dns | `homeless` AAAA `100::`, proxied — the placeholder a Worker route needs |

Cloudflare Pages was not usable for this build: it caps a deployment at 20,000 files and the atlas
is 41,929. Workers static assets on the paid plan allow 100,000.

## The old origin (removed 2026-11-20)

The local python server (`atlas-web.service`, 127.0.0.1:8123) and its tunnel (`atlas-tunnel.service`,
config `~/.cloudflared/atlas.yml`, tunnel 99f5057e) are no longer installed. The unit files and config
were moved to `~/.local/share/retired-atlas-origin/`. Nothing on this machine serves the site.

`cloudflared tunnel route dns` writes into whichever zone the default config names, so DNS for the
custom domain was created through the API; and only one connector may run per tunnel config — a
leftover `cloudflared run` keeps serving its old ingress and returns 404 for later hostnames.
