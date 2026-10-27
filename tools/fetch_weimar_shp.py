#!/usr/bin/env python3
"""
fetch_weimar_shp.py — download one or more of Weimar's open-data Shapefiles and
convert each to a static GeoJSON for the web platform.

Source pattern (Stadt Weimar via GDI-Th):
  https://www.geoproxy.geoportal-th.de/download-service/opendata/kommunal/<name>.zip

Handles Point / MultiPoint / PolyLine / Polygon. Reprojects EPSG:25832 ->
EPSG:4326. The `.dbf` files claim UTF-8 in `.cpg` but are actually cp1252.
Empty attribute values are dropped. Raw zips are kept in data/raw/stadt_weimar/.

Licence: dl-de/by-2-0, © GDI-Th / Stadt Weimar.

Deps: pyshp, pyproj.

Usage:
  python tools/fetch_weimar_shp.py ko_we_schul ko_we_kita
  python tools/fetch_weimar_shp.py ko_we_schul --out schools
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pyproj
import shapefile  # pyshp

from geojson_clean import clean_features  # mandatory ring hygiene for polygon layers

BASE_URL = "https://www.geoproxy.geoportal-th.de/download-service/opendata/kommunal/"
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "stadt_weimar"
OUT_DIR = REPO_ROOT / "web" / "layers" / "data"

TRANSFORM = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

# pyshp shapeType -> (geojson single, geojson multi)
POINT_TYPES = {1, 11, 21}
LINE_TYPES = {3, 13, 23}
POLY_TYPES = {5, 15, 25}


def clean(v):
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("cp1252", "replace").strip()
    if isinstance(v, str):
        return v.strip()
    return v


def xy(pt):
    lon, lat = TRANSFORM.transform(pt[0], pt[1])
    return [round(lon, 7), round(lat, 7)]


def ring(pts):
    return [xy(p) for p in pts]


def signed_area(r):
    return sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1)) / 2.0


def to_geometry(shape):
    st = shape.shapeType
    pts = shape.points
    parts = list(shape.parts) + [len(pts)]
    segs = [pts[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]

    if st in POINT_TYPES:
        if len(pts) == 1:
            return {"type": "Point", "coordinates": xy(pts[0])}
        return {"type": "MultiPoint", "coordinates": [xy(p) for p in pts]}

    if st in LINE_TYPES:
        if len(segs) == 1:
            return {"type": "LineString", "coordinates": ring(segs[0])}
        return {"type": "MultiLineString", "coordinates": [ring(s) for s in segs]}

    if st in POLY_TYPES:
        polys, cur = [], None
        for s in segs:
            r = ring(s)
            if signed_area(r) < 0:  # outer ring (clockwise in shapefiles)
                if cur:
                    polys.append(cur)
                cur = [r]
            else:  # hole (counter-clockwise)
                if cur is None:
                    cur = [r]
                else:
                    cur.append(r)
        if cur:
            polys.append(cur)
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": polys[0]}
        return {"type": "MultiPolygon", "coordinates": polys}

    raise ValueError(f"unsupported shapeType {st}")


def convert(name: str, out_name: str | None) -> None:
    url = f"{BASE_URL}{name}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/shp"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{name}.zip").write_bytes(raw)

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        parts = {ext: z.read(f"{name}.{ext}") for ext in ("shp", "dbf", "shx")}

    reader = shapefile.Reader(
        shp=io.BytesIO(parts["shp"]), dbf=io.BytesIO(parts["dbf"]), shx=io.BytesIO(parts["shx"]),
        encoding="cp1252", encodingErrors="replace",
    )
    fields = [f[0] for f in reader.fields[1:]]

    feats = []
    for sr in reader.shapeRecords():
        props = {k: clean(v) for k, v in zip(fields, sr.record) if clean(v) not in (None, "")}
        props["licence"] = "dl-de/by-2-0, © GDI-Th / Stadt Weimar"
        feats.append({"type": "Feature", "properties": props, "geometry": to_geometry(sr.shape)})

    feats, _ = clean_features(feats, label=name)

    fc = {
        "type": "FeatureCollection",
        "name": out_name or name,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": feats,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / f"{out_name or name}.geojson"
    dest.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"{name}: {len(feats)} features ({reader.shapeTypeName}) -> {dest.name} ({dest.stat().st_size/1024:.1f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+", help="shapefile base name(s), e.g. ko_we_schul")
    ap.add_argument("--out", help="output basename (only valid with a single input)")
    args = ap.parse_args()
    if args.out and len(args.names) != 1:
        ap.error("--out needs exactly one input name")
    for n in args.names:
        convert(n, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
