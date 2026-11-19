#!/usr/bin/env python3
"""Replace degenerate CoC geometry with real HUD CoC grantee-area boundaries.

The `coc_geometry` ingestor stored rings with two points (effectively a bounding box), which drew
rectangles on the map. HUD's own ArcGIS layer carries the real boundaries; the GeoJSON formatter on
that service truncates the rings, so we read Esri JSON (`f=json`) and convert the rings ourselves.

Run: python3 scripts/fix_coc_geometry.py [--max-points 400]
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest.common import db, fetch, now, source_id_for  # noqa: E402

SERVICE = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
           "Continuum_of_Care_Grantee_Areas/FeatureServer/0/query")
MAX_POINTS = 400
if "--max-points" in sys.argv:
    MAX_POINTS = int(sys.argv[sys.argv.index("--max-points") + 1])


def simplify(ring, max_points=MAX_POINTS):
    if len(ring) <= max_points:
        return ring
    stride = len(ring) / max_points
    out = [ring[int(i * stride)] for i in range(max_points)]
    if out[-1] != ring[-1]:
        out.append(ring[-1])
    return out


def rings_to_multipolygon(rings, precision=5):
    polys = []
    for ring in rings:
        pts = [[round(float(x), precision), round(float(y), precision)] for x, y in ring]
        if len(pts) < 4:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        polys.append([pts])
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys}


def main():
    conn = db()
    src = source_id_for(conn, SERVICE, "coc_geometry",
                        title="HUD CoC Grantee Areas (FeatureServer, Esri JSON)",
                        publisher="US Department of Housing and Urban Development",
                        license="public domain")
    got, updated, degenerate, seen = 0, 0, 0, set()
    offset = 0
    while True:
        # small pages + server-side generalisation: the full-resolution response is ~97 MB and
        # the JSON comes back truncated
        q = (f"{SERVICE}?where=1%3D1&outFields=COCNUM%2CCOCNAME&returnGeometry=true&outSR=4326"
             f"&geometryPrecision=4&maxAllowableOffset=0.01"
             f"&f=json&resultOffset={offset}&resultRecordCount=25")
        payload = json.loads(fetch(q, domain="coc_geometry"))
        feats = payload.get("features") or []
        if not feats:
            break
        for f in feats:
            coc = (f.get("attributes") or {}).get("COCNUM")
            geom = f.get("geometry") or {}
            rings = geom.get("rings") or []
            if not coc:
                continue
            mp = rings_to_multipolygon([simplify(r) for r in rings])
            got += 1
            if not mp:
                degenerate += 1
                continue
            cur = conn.execute("SELECT 1 FROM geo_coc WHERE coc_num=?", (coc,)).fetchone()
            if cur:
                conn.execute("UPDATE geo_coc SET geometry=?, source_id=? WHERE coc_num=?",
                             (json.dumps(mp), src, coc))
                updated += 1
            seen.add(coc)
        conn.commit()
        if not payload.get("exceededTransferLimit"):
            break
        offset += len(feats)
    total = conn.execute("SELECT COUNT(*) FROM geo_coc WHERE geometry IS NOT NULL").fetchone()[0]
    empty = conn.execute("""SELECT COUNT(*) FROM geo_coc WHERE geometry IS NOT NULL
                            AND length(geometry) < 120""").fetchone()[0]
    conn.execute("INSERT INTO ingest_run(domain,ts,rows,notes,ok) VALUES(?,?,?,?,1)",
                 ("coc_geometry_fix", now(), updated,
                  f"boundaries fetched={got} updated={updated} degenerate={degenerate} "
                  f"total_with_geom={total} still_tiny={empty}"))
    conn.commit()
    print(json.dumps({"fetched": got, "updated": updated, "degenerate": degenerate,
                      "with_geometry": total, "still_suspiciously_small": empty}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
