#!/usr/bin/env python3
"""
fetch_weimar_bplan.py — build a static GeoJSON of Weimar's development plans
(Bebauungspläne & Satzungen) from the city's open-data Shapefile.

Source: Stadt Weimar via GDI-Th (GeoMIS record fc807d6c-e7fc-4f91-9443-431566b3706e)
  Shapefile : https://www.geoproxy.geoportal-th.de/download-service/opendata/kommunal/ko_we_bp.zip
  (also live as WMS ko_we/INSPIREplu -> LU.SpatialPlan, outlines only, and as the
   statewide layer komm_bp in kommunal/komm_wms)

The shapefile carries the plan perimeters plus useful attributes: plan name /
id, type, legal status, the authority, and a direct link to the plan PDF. We
reproject EPSG:25832 -> EPSG:4326 and emit plain GeoJSON for Cesium.

Licence: dl-de/by-2-0, © GDI-Th / Stadt Weimar.

Deps: pyshp, pyproj.  Usage:  python tools/fetch_weimar_bplan.py
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pyproj
import shapefile  # pyshp

from geojson_clean import clean_features  # mandatory ring hygiene for polygon layers

ZIP_URL = "https://www.geoproxy.geoportal-th.de/download-service/opendata/kommunal/ko_we_bp.zip"

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "stadt_weimar"
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_bplan.geojson"

TRANSFORM = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

# Keep the meaningful columns, rename to something legible.
FIELD_MAP = {
    "name": "name",
    "planID": "plan_id",
    "plantyp": "type",
    "rechtBesch": "legal_status",
    "plangeber": "authority",
    "gemeinde": "municipality",
    "urlpdf": "pdf",
    "CEWLink": "metadata",
    "VERSBAUNVO": "date_baunvo",
    "VERSBAUGB": "date_baugb",
}


def download() -> bytes:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "weimar-digital-twin/bplan"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    (RAW_DIR / "ko_we_bp.zip").write_bytes(data)
    return data


def clean(v):
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip()
    if isinstance(v, str):
        return v.strip()
    return v


def ring_to_lonlat(pts):
    return [[round(x, 7), round(y, 7)] for x, y in (TRANSFORM.transform(px, py) for px, py in pts)]


def shape_to_geometry(shape):
    """pyshp polygon parts -> GeoJSON Polygon / MultiPolygon (rings by orientation)."""
    parts = list(shape.parts) + [len(shape.points)]
    rings = [shape.points[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]

    def signed_area(r):
        return sum((r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]) for i in range(len(r) - 1)) / 2.0

    polygons, current = [], None
    for r in rings:
        if signed_area(r) < 0:  # clockwise = outer ring (shapefile convention)
            if current:
                polygons.append(current)
            current = [ring_to_lonlat(r)]
        else:  # counter-clockwise = hole
            if current is None:
                current = [ring_to_lonlat(r)]
            else:
                current.append(ring_to_lonlat(r))
    if current:
        polygons.append(current)

    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def main() -> int:
    raw = download()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        parts = {ext: z.read(f"ko_we_bp.{ext}") for ext in ("shp", "dbf", "shx")}

    # The .cpg claims UTF-8 but the .dbf is actually cp1252 ("Straße" etc.).
    reader = shapefile.Reader(
        shp=io.BytesIO(parts["shp"]),
        dbf=io.BytesIO(parts["dbf"]),
        shx=io.BytesIO(parts["shx"]),
        encoding="cp1252",
        encodingErrors="replace",
    )
    field_names = [f[0] for f in reader.fields[1:]]

    features = []
    for sr in reader.shapeRecords():
        rec = dict(zip(field_names, sr.record))
        props = {out: clean(rec.get(src)) for src, out in FIELD_MAP.items() if rec.get(src) not in (None, "")}
        props["licence"] = "dl-de/by-2-0, © GDI-Th / Stadt Weimar"
        features.append({"type": "Feature", "properties": props, "geometry": shape_to_geometry(sr.shape)})

    features, _ = clean_features(features, label="bplan")

    fc = {
        "type": "FeatureCollection",
        "name": "weimar_bplan",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"{len(features)} plans -> {OUT} ({OUT.stat().st_size/1024:.1f} KB)")
    print(f"raw zip kept at {RAW_DIR / 'ko_we_bp.zip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
