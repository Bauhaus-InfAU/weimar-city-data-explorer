#!/usr/bin/env python3
"""
fetch_weimar_roads.py — build a static GeoJSON of the classified road network
(Klassifiziertes Straßennetz) for the Weimar AOI from its WFS.

TASK_05 Part 3: looked for a WFS twin of th_road_network (INSPIREtn service,
TN.RoadTransportNetwork.RoadLink) — that INSPIRE service itself has **no** WFS
(GetCapabilities on INSPIREtn/INSPIREtn_wfs both fail: "Unsupported/invalid
service type" / "No service ... available"). Its sibling th_classified_roads
(STRNETZ WMS, layer Klassifiz_StrNetz) DOES have a WFS twin, by the same
STRNETZ_SVZ / STRNETZ_SVZ_wfs pattern already used for th_traffic_volumes:

  WFS: https://www.geoproxy.geoportal-th.de/geoproxy/services/STRNETZ_wfs
       (WFS 1.1.0; 2.0.0 GetCapabilities also answers, GetFeature untested there)
  type: tlbv:Klassifiz_StrNetz  (also on this service, not used here:
        Euronetz_StrNetz, Netzknoten_StrNetz, Nullpunkte_StrNetz,
        KmLinien_StrNetz, StatLinien_StrNetz)

Geometry: plain gml:LineString (no curve segments to chain, unlike the ALKIS
parcels feed). CRS EPSG:25832, reprojected to 4326 for the web.

Attributes kept: name (e.g. "B 85"), klasse (A/B/L/K/G road class), nummer,
laenge_m (segment length), abschnitt (Abschnitt/Ast section id). This is the
**classified network only** (Autobahn/Bundes-/Landes-/Kreis-/Gemeindestraße) —
motor-vehicle roads, so it carries NO carriageway-vs-footway/cycleway
distinction; that need (TASK_05 Part 4) is left entirely to the OpenStreetMap
pedestrian layer, not this one.

Only 51 features intersect the Weimar AOI (this is the classified skeleton,
not every residential street — th_road_network/ATKIS on the WMS side is denser
but has no WFS), so this fetch needs no tiling/paging — one bbox GetFeature.

Licence: dl-de/by-2-0, © GDI-Th. Usage:
  python tools/fetch_weimar_roads.py
"""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402 (kept for consistency; roads are lines, so this is a no-op pass-through)

WFS = "https://www.geoproxy.geoportal-th.de/geoproxy/services/STRNETZ_wfs"
TYPENAME = "tlbv:Klassifiz_StrNetz"

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_roads.geojson"

# Weimar AOI (lon/lat, EPSG:4326) — DATA.md; reprojected to 25832 for the WFS BBOX filter.
AOI_LONLAT = (11.2365, 50.91673, 11.40122, 51.03776)  # west, south, east, north

NS = {
    "gml": "http://www.opengis.net/gml",
    "tlbv": "http://www.geoproxy.geoportal-th.de/tlbv",
}

TO_25832 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def aoi_bbox_25832() -> tuple[float, float, float, float]:
    w, s, e, n = AOI_LONLAT
    corners = [TO_25832.transform(x, y) for x, y in [(w, s), (e, n), (w, n), (e, s)]]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def get_features_gml() -> bytes:
    minx, miny, maxx, maxy = aoi_bbox_25832()
    qs = urllib.parse.urlencode({
        "SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature",
        "TYPENAME": TYPENAME, "SRSNAME": "urn:ogc:def:crs:EPSG::25832",
        "BBOX": f"{minx},{miny},{maxx},{maxy},urn:ogc:def:crs:EPSG::25832",
    })
    url = f"{WFS}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/roads"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def parse_roads(gml_bytes: bytes) -> list[dict]:
    features = []
    root = ET.fromstring(gml_bytes)
    for el in root.findall(f".//{{{NS['tlbv']}}}Klassifiz_StrNetz"):
        ls = el.find(".//gml:LineString/gml:posList", NS)
        if ls is None or not ls.text:
            continue
        nums = [float(v) for v in ls.text.split()]
        pts_en = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        lonlat = [[round(x, 7), round(y, 7)] for x, y in (TO_4326.transform(e, n) for e, n in pts_en)]

        def txt(tag):
            e = el.find(f"tlbv:{tag}", NS)
            return e.text if e is not None else None

        props = {
            "name": txt("name"),
            "klasse": txt("klasse"),
            "nummer": txt("nummer"),
            "laenge_m": float(txt("laenge")) if txt("laenge") else None,
            "abschnitt": txt("abschnitt"),
            "licence": "dl-de/by-2-0, © GDI-Th",
        }
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "LineString", "coordinates": lonlat},
        })
    return features


def main() -> int:
    print("fetching Klassifiz_StrNetz over the Weimar AOI bbox…")
    gml = get_features_gml()
    features = parse_roads(gml)
    print(f"parsed {len(features)} road segments")

    # LineStrings aren't touched by clean_features (polygon-only hygiene), but
    # run them through anyway for a consistent, logged pipeline across layers.
    kept, summary = clean_features(features, label="weimar_roads")

    fc = {"type": "FeatureCollection", "features": kept}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    import json
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT} — {len(kept)} segments, {OUT.stat().st_size / 1e3:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
