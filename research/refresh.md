# Source freshness: knowing when to re-ingest

The atlas is re-ingested by hand, and a full pass over every domain takes hours of polite
1 request per second fetching. Most weeks nothing upstream has moved. Each publisher already
exposes a cheap signal of change, so we ask for that signal and nothing else, and re-ingest only
the domains whose signal moved.

## How it works

- `ingest/probes.py` holds `PROBES`, a registry of domain -> probes, and `fingerprint(probe)`,
  which reads one probe's signal and never raises (an outage is reported as `error`, never as
  a change).
- `data/freshness.json` (committed) holds the fingerprint accepted for each probe after the
  last good ingest, plus `last_ingest`, the latest successful `ingest_run.ts` for the domain
  when the database is present. Sorted keys, indent 1, so a diff shows which source moved.
- `scripts/refresh.py --check` fingerprints every probe and compares only the fields that mean
  "the data changed" (listed per kind in `COMPARE`; a probe can narrow them with `compare`).
  Status per probe: `CHANGED`, `same`, `new` (no accepted fingerprint yet) or `error`.
  Exit 0 when nothing changed, 10 when something changed, 2 on a crash or bad usage.
- After a good re-ingest, `--accept <domain>` stores the new fingerprints. `--run` does both
  for every CHANGED domain, one ingest at a time (the database allows one writer).
- `.github/workflows/source-watch.yml` runs `--check` every Monday at 13:00 UTC and keeps one
  open issue titled "Upstream data changed" up to date with a table of what moved. A change is
  news, not a failure: the job fails only when the checker crashes. It needs issues enabled on
  the repository it runs in (the `baney75` fork has them disabled; `0xSero/homeless-atlas` has
  them on).

Probe kinds: `http` (HEAD, or a ranged `bytes=0-0` GET when HEAD is refused: Last-Modified,
ETag, length), `socrata` (`/api/views/<id>.json`: rowsUpdatedAt, viewLastModified), `arcgis`
(`<layer>?f=json`: editingInfo.lastEditDate and dataLastEditDate), `json` (one field of a JSON
endpoint), `next_vintage` (does next year's URL answer 200?) and `listing` (sha256 of the sorted
file links on an index page; the links themselves are stored when there are 40 or fewer, so the
CHANGED line names the new file).

## Probes and what each detects

All 33 URLs answered from this machine on 2026-11-20 (run of `python3 -m ingest.probes`, then
the seeding `--check` / `--accept`). HUD USER answers the project UA with an AWS WAF challenge
(HTTP 202, empty body), so its probes send the same browser UA the `hud_pit` and `hic`
ingestors already use (`ua: "browser"`).

| domain | probe | kind | detects | result 2026-11-20 |
|---|---|---|---|---|
| hud_pit | pit_workbook | http | HUD reissues 2007-2025-PIT-Counts-by-CoC.xlsb | 200, 8,859,426 bytes, LM 2026-05-12 |
| hud_pit | pit_next_year | next_vintage | 2007-2026-PIT-Counts-by-CoC.xlsb appears | 2026: 404 (not yet) |
| hud_pit | ahar_2025_files | listing | a file added to or removed from the 2025 AHAR data page | 200, 9 files |
| hud_pit | coc_grantee_layer | arcgis | CoC grantee areas layer edited | lastEditDate 1755631411617 |
| hud_pit | gazetteer_next | next_vintage | 2025 Gazetteer published (domain uses 2024) | 2025: 200, already published |
| hic | hic_workbook | http | HIC by CoC workbook reissued | 200, 2,598,803 bytes |
| hic | hic_projects_csv | http | project-level HIC CSV reissued | 200, 11,334,278 bytes |
| hic | hic_next_year | next_vintage | 2007-2026-HIC-Counts-by-CoC.xlsx appears | 2026: 404 (not yet) |
| health | nchs_state, vsrr_state, vsrr_county, brfss | socrata | data.cdc.gov xbxb-epbu, xkb8-kh2a, gb4e-yj24, dttw-5yxu rows updated | all 200 |
| health | sf_dph | socrata | data.sfgov.org jxrr-bmra rows updated | 200 |
| health | ct_ocme | socrata | data.ct.gov rybz-nyjw rows updated | 200 |
| coc_geometry | coc_geo_type_layer | arcgis | HUD CoC_Geo_Type layer edited | lastEditDate 1738592383660 |
| usaspending | awards_last_updated | json | `/api/v2/awards/last_updated/` date moves | 11/20/2026 |
| places | sub_est2024 | http | Census reissues sub-est2024.csv | 200, 7,087,080 bytes |
| places | popest_next | next_vintage | popest datasets/2020-2025/ exists | 2025: 200, already published |
| places | gazetteer_next | next_vintage | 2024 Gazetteer exists (domain uses 2023) | 2024: 200, already published |
| sf | budget, budget_fte, supplier_contracts | socrata | data.sfgov.org xdgd-c79v, 4zfx-f2ts, cqi5-hm2d rows updated | all 200 |
| uk | rough_sleeping_page | listing | a new .ods/.xlsx/.csv on gov.uk "tables on rough sleeping" | 200, 2 files |
| uk | live_tables_page | listing | a new file on gov.uk "live tables on homelessness" | 200, 56 files |
| uk | ni_bulletin_tables | http | NI homelessness bulletin tables reissued | 200, 1,507,427 bytes |
| canada | statcan_98100252 | http | StatCan table 98-10-0252 zip reissued | 200, 48,589,259 bytes |
| canada | bc_count_2024_25 | http | BC 2024-25 count report reissued | 200, 15,648,978 bytes |
| australia | abs_cube_state | http | ABS 2049.0 cube 1 reissued | 200, 182,646 bytes |
| australia | rogs_homelessness_csv | http | RoGS section 19 dataset reissued | 200, 1,133,782 bytes |
| county | acs5_next | next_vintage | ACS 5-year summary file for 2025 published | 2025: 401 (not yet; this is how the Census server answers a missing directory) |
| county | cdc_places_county | socrata | CDC PLACES county data swc5-untb rows updated | 200 |
| county | hrsa_hpsa_mental_health | http | HRSA mental health HPSA file changes size | 200, 25,689,007 bytes (ranged GET) |
| county | usaspending_last_updated | json | USASpending award data date moves | 11/20/2026 |

Notes on the signals:

- **`next_vintage` already true** (hud_pit and places gazetteers, places popest) is real news on
  day one: those domains are a vintage behind what Census has published. Once accepted the
  probe reads `same`; when the domain moves to the new vintage, bump `current` in
  `ingest/probes.py` and accept again.
- **USASpending moves every day.** `usaspending` and `county` will show CHANGED on every weekly
  run because the award data date advances daily. That is accurate, but it means the weekly
  issue will almost always be open. If that is too noisy, narrow the probe with
  `"compare": []` or move it to a monthly comparison.
- **HRSA regenerates its HPSA file daily**: Last-Modified moves every day and differs between
  its servers (12:02 and 12:19 on the same day), and its HEAD answers a bogus 20-byte length.
  The probe uses a ranged GET and compares only the file size (`"compare": ["content_length"]`).
- **The AHAR 2025 page's own Last-Modified is today's date** (it is generated), so it is probed
  as a `listing`, which only changes when a file link changes.

## Domains with no reliable probe, and why

| domain | why not |
|---|---|
| uk (Scotland) | gov.scot answers every request (project UA and browser UA) with an AWS WAF challenge, HTTP 202 and an empty body. The `uk` ingestor's cached copy stands; check the Scotland publication page by hand each September. |
| canada (Everyone Counts pages) | housing-infrastructure.canada.ca omits an intermediate certificate; `canada.py` downloads it at run time to verify. A probe that skipped verification would be wrong, and one that downloads a certificate is not cheap. The StatCan and BC probes cover the domain's other sources. |
| budgets, award_places | derived from the `award` table plus USASpending queries; the `usaspending` probe is their signal. |
| docs, laws, media, crime, facility, nonprofit, org_contacts | search APIs and many small publishers (GovInfo, Wikidata, Wikipedia, YouTube, ProPublica, IRS e-file batches, dozens of city ArcGIS layers); no single endpoint says "something new". These are swept on a schedule, not on a change signal. |
| local_sources | its inputs are the research files in `research/county/`; a pull request is the change signal. |

## Commands

```bash
python3 scripts/refresh.py                       # same as --check: table, exit 0 or 10
python3 scripts/refresh.py --check --json watch.json --markdown watch.md
python3 scripts/refresh.py --domains hud_pit,hic # any mode, limited to these domains
python3 scripts/refresh.py --accept hud_pit      # after a good: python3 -m ingest.run hud_pit
python3 scripts/refresh.py --accept              # accept everything as it is now
python3 scripts/refresh.py --run                 # re-ingest CHANGED domains one at a time, accept each that exits 0
python3 scripts/refresh.py --state /tmp/f.json   # compare against another state file (testing)
python3 -m ingest.probes [domain ...]            # raw fingerprints, no comparison
git add data/freshness.json                      # commit the accepted fingerprints
```

## Unattended refresh on a small box

`--watch` runs the loop forever on a machine with 500 MB to 1 GB of RAM:

```bash
python3 scripts/refresh.py --watch --interval 21600 --max-rss-mb 900 --then-build \
    [--publish-cmd ./scripts/publish_cf.sh]
```

Each cycle: `--check`; re-ingest each CHANGED domain as its own child process, strictly one at
a time, accepting its fingerprints only when it exits 0; then, only if at least one re-ingest
succeeded and `--then-build` is set, `python3 scripts/build_site.py` and `python3
scripts/crawl.py --quiet`; then `--publish-cmd` only when build and crawl both exit 0. Nothing is
published unless you pass `--publish-cmd`. One JSON line per cycle goes to `logs/refresh.jsonl`
(probes, changed domains, errors, each child's exit code, seconds and peak RSS).

Memory: every child gets `RLIMIT_AS` set to `--max-rss-mb` where the OS honours it (Linux), and
is also polled with `ps -o rss= -p <pid>` every 5 s and killed (SIGTERM, then SIGKILL after
15 s) with a logged error when its RSS passes the cap, because macOS ignores `RLIMIT_AS`.
Verified on this Mac: a child that touched 414 MB under a 100 MB cap was killed at the first
poll (rc -15), and a small child ran normally.

`touch STOP` in the repo root ends the loop between steps, the same convention as
`scripts/loop.sh`; no new ingest starts once STOP exists. Remove STOP before starting again.

systemd user unit (`~/.config/systemd/user/atlas-refresh.service`; enable with
`systemctl --user enable --now atlas-refresh` and `loginctl enable-linger $USER`):

```ini
[Unit]
Description=homeless-atlas source refresh loop
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/homeless-atlas
ExecStart=/usr/bin/python3 scripts/refresh.py --watch --interval 21600 --max-rss-mb 900 --then-build
Restart=on-failure
RestartSec=300
MemoryMax=1G
MemoryHigh=950M
Nice=10

[Install]
WantedBy=default.target
```

`MemoryMax` caps the whole service (loop plus its one child); the loop's own `--max-rss-mb 900`
kills a runaway child first, with a log line, before the kernel has to.

launchd agent for a Mac (`~/Library/LaunchAgents/org.homeless-atlas.refresh.plist`; load with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.homeless-atlas.refresh.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.homeless-atlas.refresh</string>
  <key>WorkingDirectory</key><string>/Users/YOU/projects/homeless-atlas</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>scripts/refresh.py</string>
    <string>--watch</string>
    <string>--interval</string><string>21600</string>
    <string>--max-rss-mb</string><string>900</string>
    <string>--then-build</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>HardResourceLimits</key>
  <dict><key>ResidentSetSize</key><integer>1073741824</integer></dict>
  <key>SoftResourceLimits</key>
  <dict><key>ResidentSetSize</key><integer>1073741824</integer></dict>
  <key>StandardOutPath</key><string>/Users/YOU/projects/homeless-atlas/logs/refresh.out</string>
  <key>StandardErrorPath</key><string>/Users/YOU/projects/homeless-atlas/logs/refresh.err</string>
</dict>
</plist>
```

Current macOS does not enforce `ResidentSetSize`, so on a Mac the 1 GB limit is advisory and the
real cap is the loop's own 5-second RSS poll (`--max-rss-mb`). `KeepAlive` with
`SuccessfulExit=false` restarts the loop after a crash but not after a clean STOP.
