#!/usr/bin/env python3
"""
build_buildings_eave_height.py — CityGML LoD2 -> footprints with height WITHOUT the roof.

Reads the same Thuringia LoD2 tiles as parse_citygml_footprints.py
(`data/raw/citygml_th/LoD2/*.zip`) and writes

  web/layers/data/weimar_buildings_eave_height.geojson

One Feature per GroundSurface polygon of every Building / BuildingPart. Heights are
computed per unit (Building or BuildingPart) from its own surfaces, relative to the
lowest GroundSurface vertex:

  eaveHeight  : "wall top" height = area-weighted mean over the unit's RoofSurface
                polygons of: mean Z for flat surfaces (slope < FLAT_DEG), lowest Z
                (the eave line) for pitched ones. Flat roof -> = ridgeHeight.
                Gable end walls (which run up to the ridge) are ignored on purpose:
                only roof surfaces are looked at.
  ridgeHeight : highest vertex − ground (same idea as measuredHeight, per part).
  height      : = eaveHeight (what the platform/3DM extrude by). Units without roof
                surfaces fall back to measuredHeight / Z range (heightSource).
  roofType    : AdV roof code -> English name.
  storeys     : CityGML storeysAboveGround (when present).
  usage, useCategory, floors : joined by buildingId from weimar_building_footprints.geojson (ALKIS).
  atticFloorLikely : true when floors × STOREY_M clearly exceeds eaveHeight on a pitched
                roof, i.e. the floor count most likely includes a floor inside the roof.
  heightWithAttic  : eaveHeight + the floors that do not fit under the eave (rounded, × STOREY_M,
                     at least one), capped at ridgeHeight; = eaveHeight on all other buildings.

Licence: dl-de/by-2-0, © GDI-Th. CRS in EPSG:25832 (heights DHHN2016), out EPSG:4326.
Usage:  python tools/build_buildings_eave_height.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pyproj

from geojson_clean import clean_features

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "data" / "raw" / "citygml_th" / "LoD2"
ATTR_SRC = REPO_ROOT / "web" / "layers" / "data" / "weimar_building_footprints.geojson"
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_buildings_eave_height.geojson"

FLAT_DEG = 10.0      # roof surfaces flatter than this count as flat
STOREY_M = 3.0       # assumed storey height for the attic check
ATTIC_TOL_M = 1.0    # floors*STOREY_M must exceed eave by more than this to flag an attic floor

GML = "http://www.opengis.net/gml"
BLDG = "http://www.opengis.net/citygml/building/1.0"
ROOF_TYPES = {
    "1000": "flat", "2100": "shed", "2200": "split shed", "3100": "gable", "3200": "hip",
    "3300": "half-hip", "3400": "mansard", "3500": "pyramid", "3600": "cone", "3700": "dome",
    "3800": "sawtooth", "3900": "arched", "4000": "tower", "5000": "mixed", "9999": "other",
}

T = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def parse_poslist(text):
    v = text.split()
    return [(float(v[i]), float(v[i + 1]), float(v[i + 2])) for i in range(0, len(v) - 2, 3)]


def ring_lonlat(pts3):
    return [[round(lon, 7), round(lat, 7)] for lon, lat in (T.transform(e, n) for e, n, _z in pts3)]


def signed_area(ring):
    return sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) / 2.0


def as_ccw(ring):
    return ring if signed_area(ring) > 0 else ring[::-1]


def as_cw(ring):
    return ring if signed_area(ring) < 0 else ring[::-1]


def exterior_pts(poly):
    pl = poly.find(f"{{{GML}}}exterior//{{{GML}}}posList")
    return parse_poslist(pl.text) if pl is not None and pl.text else []


def newell(pts):
    nx = ny = nz = 0.0
    for i in range(len(pts) - 1):
        x1, y1, z1 = pts[i]
        x2, y2, z2 = pts[i + 1]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return nx, ny, nz


def own_surfaces(unit, kind):
    """Polygons of the unit's own bldg:boundedBy/<kind> (not those of nested BuildingParts)."""
    out = []
    for bb in unit.findall(f"{{{BLDG}}}boundedBy"):
        for s in bb.findall(f"{{{BLDG}}}{kind}"):
            out += list(s.iter(f"{{{GML}}}Polygon"))
    return out


def unit_heights(unit):
    """-> dict(ground, eave, ridge, measured, source) for one Building/BuildingPart, or None."""
    ground_polys = own_surfaces(unit, "GroundSurface")
    roof_polys = own_surfaces(unit, "RoofSurface")
    all_polys = ground_polys + roof_polys + own_surfaces(unit, "WallSurface")
    zs = [p[2] for poly in all_polys for p in exterior_pts(poly)]
    if not zs:
        return None
    gz = [p[2] for poly in ground_polys for p in exterior_pts(poly)]
    ground = min(gz) if gz else min(zs)
    ridge = max(zs) - ground
    mh = unit.find(f"{{{BLDG}}}measuredHeight")
    measured = None
    if mh is not None and mh.text:
        try:
            measured = float(mh.text) if float(mh.text) > 0 else None
        except ValueError:
            pass
    wsum = asum = 0.0
    for poly in roof_polys:
        pts = exterior_pts(poly)
        if len(pts) < 4:
            continue
        nx, ny, nz = newell(pts)
        n = math.sqrt(nx * nx + ny * ny + nz * nz)
        a_h = abs(nz) / 2.0                         # horizontally projected area
        if n == 0 or a_h < 1e-6:
            continue                                # vertical "roof" pieces carry no footprint
        slope = math.degrees(math.acos(min(1.0, abs(nz) / n)))
        ring = pts[:-1]
        top = (sum(p[2] for p in ring) / len(ring)) if slope < FLAT_DEG else min(p[2] for p in ring)
        wsum += a_h * (top - ground)
        asum += a_h
    if asum > 0:
        eave, source = min(max(wsum / asum, 0.0), ridge), "roof surfaces"
    else:
        eave, source = (measured if measured else ridge), "no roof surfaces: measuredHeight/Z range"
    return {"ground": ground, "eave": eave, "ridge": ridge, "measured": measured, "source": source}


def unit_features(unit, gid, roof_code, storeys):
    h = unit_heights(unit)
    if h is None or h["eave"] <= 0:
        return []
    rt = unit.find(f"{{{BLDG}}}roofType")
    roof_code = rt.text.strip() if rt is not None and rt.text else roof_code
    st = unit.find(f"{{{BLDG}}}storeysAboveGround")
    storeys = int(st.text) if st is not None and st.text and st.text.strip().isdigit() else storeys
    feats = []
    for poly in own_surfaces(unit, "GroundSurface"):
        ext = exterior_pts(poly)
        if len(ext) < 4:
            continue
        rings = [as_ccw(ring_lonlat(ext))]
        for inte in poly.findall(f"{{{GML}}}interior"):
            ipl = inte.find(f".//{{{GML}}}posList")
            if ipl is not None and ipl.text:
                rings.append(as_cw(ring_lonlat(parse_poslist(ipl.text))))
        props = {"buildingId": gid, "height": round(h["eave"], 2), "eaveHeight": round(h["eave"], 2),
                 "ridgeHeight": round(h["ridge"], 2), "heightSource": h["source"]}
        if roof_code:
            props["roofType"] = ROOF_TYPES.get(roof_code, roof_code)
        if storeys:
            props["storeys"] = storeys
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Polygon", "coordinates": rings}})
    return feats


def building_features(bldg):
    gid = bldg.get(f"{{{GML}}}id") or ""
    rt = bldg.find(f"{{{BLDG}}}roofType")
    roof_code = rt.text.strip() if rt is not None and rt.text else None
    st = bldg.find(f"{{{BLDG}}}storeysAboveGround")
    storeys = int(st.text) if st is not None and st.text and st.text.strip().isdigit() else None
    feats = unit_features(bldg, gid, roof_code, storeys)
    for part in bldg.iter(f"{{{BLDG}}}BuildingPart"):
        feats += unit_features(part, gid, roof_code, storeys)
    return feats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    zips = sorted(SRC_DIR.glob("*.zip"))[: args.limit or None]
    if not zips:
        raise SystemExit(f"no CityGML zips in {SRC_DIR}")

    # ALKIS attributes (usage, floors, useCategory) by buildingId
    attrs = {}
    for f in json.loads(ATTR_SRC.read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        a = attrs.setdefault(p.get("buildingId"), {})
        for k in ("usage", "useCategory", "floors"):
            if p.get(k) not in (None, "") and k not in a:
                a[k] = p[k]

    feats, seen, t0 = [], set(), time.time()
    for i, zp in enumerate(zips, 1):
        with zipfile.ZipFile(zp) as z:
            gml_name = next(n for n in z.namelist() if n.lower().endswith(".gml"))
            with z.open(gml_name) as fh:
                n_tile = 0
                for _ev, elem in ET.iterparse(fh, events=("end",)):
                    if elem.tag != f"{{{BLDG}}}Building":
                        continue
                    for feat in building_features(elem):
                        key = (feat["properties"]["buildingId"], tuple(feat["geometry"]["coordinates"][0][0]))
                        if key in seen:
                            continue
                        seen.add(key)
                        feats.append(feat)
                        n_tile += 1
                    elem.clear()
        print(f"  [{i:>2}/{len(zips)}] {zp.name:<32} +{n_tile}", flush=True)

    attic = 0
    for f in feats:
        p = f["properties"]
        p.update({k: v for k, v in attrs.get(p["buildingId"], {}).items()})
        fl = p.get("floors")
        try:
            fl = int(fl) if fl not in (None, "") else None
        except (TypeError, ValueError):
            fl = None
        pitched = p["ridgeHeight"] - p["eaveHeight"] > 1.0
        if fl and pitched and fl * STOREY_M > p["eaveHeight"] + ATTIC_TOL_M:
            extra = max(1, round((fl * STOREY_M - p["eaveHeight"]) / STOREY_M))
            p["atticFloorLikely"] = True
            p["heightWithAttic"] = round(min(p["ridgeHeight"], p["eaveHeight"] + extra * STOREY_M), 2)
            attic += 1
        else:
            p["heightWithAttic"] = p["eaveHeight"]
        p["licence"] = "dl-de/by-2-0, © GDI-Th"

    feats, _ = clean_features(feats, label="eave footprints")

    # summary
    eave = [f["properties"]["eaveHeight"] for f in feats]
    ridge = [f["properties"]["ridgeHeight"] for f in feats]
    diff = [r - e for r, e in zip(ridge, eave)]
    by_roof = {}
    for f in feats:
        p = f["properties"]
        by_roof.setdefault(p.get("roofType", "?"), []).append(p["ridgeHeight"] - p["eaveHeight"])
    print("-" * 60)
    print(f"{len(feats)} footprints from {len(zips)} tiles in {time.time() - t0:.0f}s")
    print(f"eave   median {statistics.median(eave):.2f} m, max {max(eave):.1f}")
    print(f"ridge  median {statistics.median(ridge):.2f} m, max {max(ridge):.1f}")
    print(f"ridge-eave median {statistics.median(diff):.2f} m; > 2 m: {sum(d > 2 for d in diff)}")
    for k, v in sorted(by_roof.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:<11} n={len(v):>6}  ridge-eave median {statistics.median(v):5.2f} m")
    print(f"atticFloorLikely: {sum(1 for f in feats if f['properties'].get('atticFloorLikely'))}")
    print(f"no roof surfaces: {sum(1 for f in feats if f['properties']['heightSource'] != 'roof surfaces')}")

    fc = {"type": "FeatureCollection", "name": "weimar_buildings_eave_height",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "source": "Thuringia 3D buildings LoD2 (CityGML) -> footprint + eave height (no roof); ALKIS usage/floors",
          "features": feats}
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"-> {OUT}  ({OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
