#!/usr/bin/env python3
"""
fetch_gtfs_stops.py — extract the public-transport stops inside the Weimar AOI
from the VMT (Verkehrsverbund Mittelthüringen) GTFS feed and write them as a
static GeoJSON.

Source: VMT open data
  https://www.vmt-thueringen.de/service/open-data/
  feed: https://www.vmt-thueringen.de/fileadmin/VMT_Redaktion/OPEN_DATA/VMT_GTFS.zip
  (routes, stops, schedules for all of Mittelthüringen incl. Weimar; DB long-
   distance not included — for that use gtfs.de / DELFI. No shapes.txt geometry.)

We only take `stops.txt` (~1.3 MB); the schedule tables (stop_times.txt is
~125 MB) are not needed for a stops layer. Stops are filtered to the AOI bbox
and, as a cross-check, kept if the DHID/IFOPT id starts with `de:16055:`
(municipality key of Weimar city).

Licence: CC BY 4.0 (VMT) — attribute "© VMT GmbH".

Stdlib only. Usage:  python tools/fetch_gtfs_stops.py
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
import zipfile
from pathlib import Path

FEED = "https://www.vmt-thueringen.de/fileadmin/VMT_Redaktion/OPEN_DATA/VMT_GTFS.zip"

# Weimar AOI (lon/lat) from DATA.md
W, S, E, N = 11.2365, 50.91673, 11.40122, 51.03776
WEIMAR_DHID_PREFIX = "de:16055:"

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = REPO_ROOT / "data" / "raw" / "vmt_gtfs"
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_transit_stops.geojson"


def main() -> int:
    req = urllib.request.Request(FEED, headers={"User-Agent": "weimar-digital-twin/gtfs"})
    print("downloading VMT_GTFS.zip (~15 MB)…", flush=True)
    with urllib.request.urlopen(req, timeout=180) as r:
        blob = r.read()
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "VMT_GTFS.zip").write_bytes(blob)

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        stops_txt = z.read("stops.txt").decode("utf-8-sig")
        feed_info = z.read("feed_info.txt").decode("utf-8-sig") if "feed_info.txt" in z.namelist() else ""

    version = ""
    if feed_info:
        fi = list(csv.DictReader(io.StringIO(feed_info)))
        if fi:
            version = fi[0].get("feed_version", "")

    feats = []
    for row in csv.DictReader(io.StringIO(stops_txt)):
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        in_bbox = (W <= lon <= E) and (S <= lat <= N)
        in_weimar = row.get("stop_id", "").startswith(WEIMAR_DHID_PREFIX)
        if not (in_bbox or in_weimar):
            continue
        props = {
            "stop_id": row.get("stop_id"),
            "name": row.get("stop_name"),
            "location_type": row.get("location_type") or "0",
            "parent_station": row.get("parent_station") or None,
            "wheelchair_boarding": row.get("wheelchair_boarding") or None,
            "platform_code": row.get("platform_code") or None,
        }
        props = {k: v for k, v in props.items() if v not in (None, "")}
        props["licence"] = "CC BY 4.0, © VMT GmbH"
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
        })

    fc = {
        "type": "FeatureCollection",
        "name": "weimar_transit_stops",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "gtfs_feed_version": version,
        "features": feats,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"{len(feats)} stops in AOI -> {OUT} ({OUT.stat().st_size/1024:.1f} KB)  feed {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
