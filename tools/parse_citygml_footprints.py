#!/usr/bin/env python3
"""
parse_citygml_footprints.py — MVP CityGML LoD2 -> footprint + height GeoJSON.

Reads the Thuringia 3D-building tiles already downloaded to
`data/raw/citygml_th/LoD2/*.zip` (one CityGML `.gml` per zip) and writes a
lightweight GeoJSON:

  web/layers/data/weimar_buildings_footprint_height.geojson

One Feature per building ground surface (Polygon, with holes if the source has
`gml:interior` rings). Properties:
  - buildingId : the CityGML `gml:id`
  - height     : `bldg:measuredHeight` in metres if present and > 0,
                 otherwise (max Z − min Z) over all of the building's vertices.

This is Task 02 / B.1 Step 1 (footprint + height only). Step 2 — real LoD2 roof
solids — is a later task; not done here.

Design note: the CityGML parse happens ONCE here, on the platform side. The
future Grasshopper node just extrudes these polygons by `height` (polygon ->
solid is trivial); it never touches raw CityGML. (Analogous to how Elk hides
OSM parsing.)

Source licence: Datenlizenz Deutschland – Namensnennung 2.0, © GDI-Th.
CRS in: ETRS89 / UTM 32N (EPSG:25832), heights DHHN2016. CRS out: EPSG:4326.

Deps: pyproj (stdlib xml.etree for parsing). Usage:
  python tools/parse_citygml_footprints.py            # all LoD2 tiles
  python tools/parse_citygml_footprints.py --limit 3  # first 3 tiles (debug)
"""

from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pyproj

from geojson_clean import clean_features  # mandatory ring hygiene for polygon layers

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "data" / "raw" / "citygml_th" / "LoD2"
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_buildings_footprint_height.geojson"

GML = "http://www.opengis.net/gml"
BLDG = "http://www.opengis.net/citygml/building/1.0"

T = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def parse_poslist(text):
    """'E N Z E N Z …' -> list[(E, N, Z)]."""
    v = text.split()
    return [(float(v[i]), float(v[i + 1]), float(v[i + 2])) for i in range(0, len(v) - 2, 3)]


def ring_lonlat(pts3):
    return [[round(lon, 7), round(lat, 7)]
            for lon, lat in (T.transform(e, n) for e, n, _z in pts3)]


def signed_area(ring):
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
               for i in range(len(ring) - 1)) / 2.0


def as_ccw(ring):
    return ring if signed_area(ring) > 0 else ring[::-1]


def as_cw(ring):
    return ring if signed_area(ring) < 0 else ring[::-1]


def building_to_features(bldg):
    """Yield GeoJSON features for one <bldg:Building> (incl. its BuildingParts)."""
    gid = bldg.get(f"{{{GML}}}id") or ""

    # height: measuredHeight, else Z range over every posList in the building
    height = None
    mh = bldg.find(f".//{{{BLDG}}}measuredHeight")
    if mh is not None and mh.text:
        try:
            h = float(mh.text)
            if h > 0:
                height = round(h, 2)
        except ValueError:
            pass
    if height is None:
        zs = []
        for pl in bldg.iter(f"{{{GML}}}posList"):
            if pl.text:
                zs += [p[2] for p in parse_poslist(pl.text)]
        if zs:
            height = round(max(zs) - min(zs), 2)
    if not height or height <= 0:
        height = None  # unknown

    # footprint(s): every GroundSurface polygon; fall back to lowest-mean-Z poly
    ground = list(bldg.iter(f"{{{BLDG}}}GroundSurface"))
    polygons = []
    if ground:
        for gs in ground:
            for poly in gs.iter(f"{{{GML}}}Polygon"):
                polygons.append(poly)
    else:
        best, best_z = None, None
        for poly in bldg.iter(f"{{{GML}}}Polygon"):
            pl = poly.find(f".//{{{GML}}}posList")
            if pl is None or not pl.text:
                continue
            zmean = sum(p[2] for p in parse_poslist(pl.text)) / max(1, len(parse_poslist(pl.text)))
            if best_z is None or zmean < best_z:
                best, best_z = poly, zmean
        if best is not None:
            polygons.append(best)

    for poly in polygons:
        ext_el = poly.find(f"{{{GML}}}exterior")
        if ext_el is None:
            continue
        ext_pl = ext_el.find(f".//{{{GML}}}posList")
        if ext_pl is None or not ext_pl.text:
            continue
        rings = [as_ccw(ring_lonlat(parse_poslist(ext_pl.text)))]
        for inte in poly.findall(f"{{{GML}}}interior"):
            ipl = inte.find(f".//{{{GML}}}posList")
            if ipl is not None and ipl.text:
                rings.append(as_cw(ring_lonlat(parse_poslist(ipl.text))))
        props = {"buildingId": gid}
        if height is not None:
            props["height"] = height
        props["licence"] = "dl-de/by-2-0, © GDI-Th"
        yield {"type": "Feature", "properties": props,
               "geometry": {"type": "Polygon", "coordinates": rings}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N tiles")
    args = ap.parse_args()

    zips = sorted(SRC_DIR.glob("*.zip"))
    if args.limit:
        zips = zips[: args.limit]
    if not zips:
        raise SystemExit(f"no CityGML zips in {SRC_DIR} — run tools/download_citygml.py first")

    feats = []
    seen = set()
    no_height = 0
    t0 = time.time()
    for i, zp in enumerate(zips, 1):
        with zipfile.ZipFile(zp) as z:
            gml_name = next(n for n in z.namelist() if n.lower().endswith(".gml"))
            with z.open(gml_name) as fh:
                n_tile = 0
                for _ev, elem in ET.iterparse(fh, events=("end",)):
                    if elem.tag != f"{{{BLDG}}}Building":
                        continue
                    for feat in building_to_features(elem):
                        gid = feat["properties"]["buildingId"]
                        key = (gid, tuple(feat["geometry"]["coordinates"][0][0]))
                        if key in seen:
                            continue
                        seen.add(key)
                        if "height" not in feat["properties"]:
                            no_height += 1
                        feats.append(feat)
                        n_tile += 1
                    elem.clear()
        print(f"  [{i:>2}/{len(zips)}] {zp.name:<32} +{n_tile} footprints", flush=True)

    feats, _ = clean_features(feats, label="footprints")
    no_height = sum(1 for f in feats if "height" not in f["properties"])

    fc = {
        "type": "FeatureCollection",
        "name": "weimar_buildings_footprint_height",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "source": "Thuringia 3D buildings LoD2 (CityGML) -> footprint + measuredHeight",
        "features": feats,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print("-" * 60)
    print(f"{len(feats)} footprints ({no_height} without height) from {len(zips)} tiles "
          f"in {time.time() - t0:.0f}s")
    print(f"-> {OUT}  ({OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
