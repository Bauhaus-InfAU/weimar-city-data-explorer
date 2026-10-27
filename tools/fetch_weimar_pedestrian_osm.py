#!/usr/bin/env python3
"""
fetch_weimar_pedestrian_osm.py — build a static GeoJSON of the pedestrian /
cycle network for the Weimar AOI from OpenStreetMap via Overpass.

TASK_05 Part 4. Separate resource from every official-source layer in this
project — licence is **ODbL**, attribution "© OpenStreetMap contributors",
not to be merged with any dl-de/by-2-0 derivative.

Query (Overpass QL, bbox = DATA.md Weimar AOI, lat/lon order as Overpass wants):
  - ways   highway ~ footway|path|pedestrian|steps|cycleway|track
           (the task's core list, + track: many rural foot/cycle links here
           are tagged highway=track with foot=yes rather than a footway class)
  - ways   foot ~ yes|designated, any highway class (catches sidewalks/shared
           paths mapped as a tag on a road rather than their own way)
  - nodes  highway=crossing (pedestrian crossings, point features)
  Result on this AOI: 7667 ways + 257 crossing nodes = 7924 elements (checked
  live against the public Overpass instance before writing this script).

Public Overpass instances (overpass-api.de and mirrors) intermittently return
504 "server busy" — transient, not a syntax problem (confirmed by retrying the
identical query successfully seconds later). Retried with backoff below, same
pattern as tools/download_dgm.py.

Geometry comes back inline via Overpass `out geom;` (no separate node-id
resolution needed). Attributes kept: highway, name, surface, foot, bicycle,
wheelchair, lit, ref, sidewalk, crossing.

Usage:
  python tools/fetch_weimar_pedestrian_osm.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402 (no-op for lines/points; kept for a consistent pipeline)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_pedestrian_osm.geojson"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Weimar AOI (lat/lon, EPSG:4326) — DATA.md "Область интереса (AOI) — Weimar"
W, S, E, N = 11.2365, 50.91673, 11.40122, 51.03776

QUERY = f"""
[out:json][timeout:180];
(
  way["highway"~"^(footway|path|pedestrian|steps|cycleway|track)$"]({S},{W},{N},{E});
  way["foot"~"^(yes|designated)$"]["highway"]({S},{W},{N},{E});
  node["highway"="crossing"]({S},{W},{N},{E});
);
out geom;
"""

KEEP_TAGS = ["highway", "name", "surface", "foot", "bicycle", "wheelchair", "lit", "ref", "sidewalk", "crossing"]


def fetch_overpass(query: str, retries: int = 4, pause: float = 15.0) -> dict:
    last_exc = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    endpoint, data=query.encode("utf-8"),
                    headers={"User-Agent": "weimar-digital-twin/pedestrian"},
                )
                with urllib.request.urlopen(req, timeout=220) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — includes HTTPError 504 "server busy"
                last_exc = exc
                print(f"  {endpoint} attempt {attempt}/{retries} failed ({exc}); retrying in {pause}s…",
                      file=sys.stderr)
                time.sleep(pause)
    raise RuntimeError(f"all Overpass endpoints failed: {last_exc}")


def props_from_tags(tags: dict) -> dict:
    props = {k: tags[k] for k in KEEP_TAGS if k in tags}
    props["licence"] = "ODbL, © OpenStreetMap contributors"
    return props


def elements_to_features(elements: list[dict]) -> list[dict]:
    features = []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "way":
            geom = el.get("geometry")
            if not geom or len(geom) < 2:
                continue
            coords = [[round(pt["lon"], 7), round(pt["lat"], 7)] for pt in geom]
            features.append({
                "type": "Feature",
                "properties": {**props_from_tags(tags), "osm_id": el["id"], "osm_type": "way"},
                "geometry": {"type": "LineString", "coordinates": coords},
            })
        elif el["type"] == "node":
            features.append({
                "type": "Feature",
                "properties": {**props_from_tags(tags), "osm_id": el["id"], "osm_type": "node"},
                "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]},
            })
    return features


def main() -> int:
    print("querying Overpass for the Weimar AOI pedestrian/cycle network…")
    data = fetch_overpass(QUERY)
    elements = data.get("elements", [])
    print(f"received {len(elements)} OSM elements")

    features = elements_to_features(elements)
    ways = sum(1 for f in features if f["geometry"]["type"] == "LineString")
    nodes = sum(1 for f in features if f["geometry"]["type"] == "Point")
    print(f"converted: {ways} ways (lines), {nodes} crossing nodes (points)")

    # Not polygons, so clean_features() is a pass-through here — run anyway to
    # keep every generated layer on the same mandatory hygiene pipeline.
    kept, summary = clean_features(features, label="weimar_pedestrian_osm")

    fc = {"type": "FeatureCollection", "features": kept}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT} — {len(kept)} features, {OUT.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
