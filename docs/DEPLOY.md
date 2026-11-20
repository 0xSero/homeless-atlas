# Deploying the static site

The site is a folder of static files. Nothing about serving it is special.

## Hosting (this machine)

Two systemd user units, enabled, with `Linger=yes` so they survive logout and reboot:

| unit | what it runs |
|---|---|
| `atlas-web.service` | `python3 -m http.server 8123 --bind 127.0.0.1` from `site/` |
| `atlas-tunnel.service` | `cloudflared tunnel --config ~/.cloudflared/atlas.yml run atlas` |

The tunnel is its own (`atlas`, id `99f5057e-…`), deliberately separate from the `homelab`
tunnel, so adding this site can never change ingress for `homelabai.org`, `api.`, `voice.` or `ssh.`.

```sh
systemctl --user status atlas-web atlas-tunnel
systemctl --user restart atlas-web        # after a rebuild, if the swap confuses the server
journalctl --user -u atlas-tunnel -n 50
```

## Publish a new build

```sh
python3 scripts/build_site.py     # atomic staging -> site swap
python3 scripts/crawl.py          # must print SITE OK
python3 scripts/verify.py         # must exit 0
```

The server resolves `site/` per request, so a publish is picked up immediately. A request that
lands during the directory swap can 404 for a fraction of a second.

## Names

- `https://atlas.homelabai.org` — CNAME to the `atlas` tunnel, proxied by Cloudflare.
- `http://pop-os-1.tailadb2c1.ts.net:8123` — tailnet only, no Cloudflare in the path.

If a LAN resolver returns NXDOMAIN for the new hostname while public resolvers answer, that is the
router caching the miss from before the record existed. Nothing to fix on this host.
