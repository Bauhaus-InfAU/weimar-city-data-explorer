#!/usr/bin/env python3
"""
fetch_weimar_boundary.py — build a static GeoJSON of Weimar's administrative
boundary from the Thuringia "Digitale Verwaltungsgrenzen" WFS.

Source: GDI-Th
  WFS  : https://www.geoproxy.geoportal-th.de/geoproxy/services/GRENZUEB_wfs  (v1.1.0)
  layer: tlvermgeo:th_etrs89utm32_gmd  ("Gemeinden" / municipalities)
  Weimar city = kreisfreie Stadt, Gemeinde_ID = 55000

The service's `application/json` output is a broken GML-to-JSON dump, so we ask
for GML 3.1.1, parse the polygon rings ourselves and reproject EPSG:25832 ->
EPSG:4326. Result is written as a plain GeoJSON FeatureCollection that Cesium's
GeoJsonDataSource can load directly — no runtime WFS / CORS dependency.

Licence: dl-de/by-2-0, © GDI-Th.

Deps: pyproj (already available). Usage:  python tools/fetch_weimar_boundary.py
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

import pyproj

from geojson_clean import clean_features  # mandatory ring hygiene for polygon layers

WFS = "https://www.geoproxy.geoportal-th.de/geoproxy/services/GRENZUEB_wfs"
TYPENAME = "tlvermgeo:th_etrs89utm32_gmd"
GEMEINDE_ID = "55000"  # Weimar, kreisfreie Stadt

OUT = Path(__file__).resolve().parents[1] / "web" / "layers" / "data" / "weimar_boundary.geojson"

FILTER = (
    '<Filter xmlns="http://www.opengis.net/ogc">'
    "<PropertyIsEqualTo><PropertyName>Gemeinde_ID</PropertyName>"
    f"<Literal>{GEMEINDE_ID}</Literal></PropertyIsEqualTo></Filter>"
)

TRANSFORM = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def get_gml() -> str:
    qs = urllib.parse.urlencode({
        "SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature",
        "typeName": TYPENAME, "srsName": "urn:ogc:def:crs:EPSG::25832",
        "Filter": FILTER,
    })
    req = urllib.request.Request(f"{WFS}?{qs}", headers={"User-Agent": "weimar-digital-twin/boundary"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8")


def ring_to_lonlat(poslist: str) -> list[list[float]]:
    n = [float(v) for v in poslist.split()]
    pts = []
    for i in range(0, len(n), 2):
        lon, lat = TRANSFORM.transform(n[i], n[i + 1])
        pts.append([round(lon, 7), round(lat, 7)])
    return pts


def parse_polygons(gml: str) -> list[list[list[list[float]]]]:
    """Return a list of GeoJSON polygons ([ [exterior], [hole], ... ])."""
    polys = []
    for pm in re.finditer(r"<gml:Polygon\b.*?</gml:Polygon>", gml, re.S):
        block = pm.group(0)
        ext = re.search(r"<gml:exterior>.*?<gml:posList[^>]*>([^<]+)</gml:posList>.*?</gml:exterior>", block, re.S)
        if not ext:
            continue
        rings = [ring_to_lonlat(ext.group(1))]
        for hole in re.finditer(r"<gml:interior>.*?<gml:posList[^>]*>([^<]+)</gml:posList>.*?</gml:interior>", block, re.S):
            rings.append(ring_to_lonlat(hole.group(1)))
        polys.append(rings)
    return polys


def props(gml: str) -> dict:
    def val(tag):
        m = re.search(rf"<[\w.]*:{tag}>([^<]*)</[\w.]*:{tag}>", gml)
        return m.group(1) if m else None
    return {
        "name": val("Gemeinde") or "Weimar",
        "gemeinde_id": val("Gemeinde_ID"),
        "landkreis": val("Landkreis"),
        "bundesland_id": val("Bundesland_ID"),
        "source": "GDI-Th GRENZUEB_wfs / tlvermgeo:th_etrs89utm32_gmd",
        "licence": "dl-de/by-2-0, © GDI-Th",
    }


def main() -> int:
    gml = get_gml()
    matched = re.search(r'numberOfFeatures="(\d+)"', gml)
    print(f"features returned: {matched.group(1) if matched else '?'}")
    polys = parse_polygons(gml)
    if not polys:
        raise SystemExit("no polygon geometry found in WFS response")

    total_pts = sum(len(r) for p in polys for r in p)
    if len(polys) == 1:
        geom = {"type": "Polygon", "coordinates": polys[0]}
    else:
        geom = {"type": "MultiPolygon", "coordinates": [polys]}

    features, _ = clean_features(
        [{"type": "Feature", "properties": props(gml), "geometry": geom}], label="boundary"
    )
    if not features:
        raise SystemExit("boundary geometry collapsed after cleaning — check the WFS response")

    fc = {
        "type": "FeatureCollection",
        "name": "weimar_boundary",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"{len(polys)} polygon(s), {total_pts} vertices -> {OUT} ({OUT.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
