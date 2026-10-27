#!/usr/bin/env python3
"""
merge_weimar_atkis_verkehr.py — regenerate the `weimar_atkis_*` GeoJSON layers
from **every** manually-downloaded ATKIS "Verkehr" export, deduplicated by object
identity (TASK_13 Part B).

Background
---------
`tools/fetch_weimar_atkis_verkehr.py` (TASK_07) built one GeoJSON per
`ObjektartText` from a *single* hand-downloaded WFS/GML file covering only the
Weimar centre (~7 x 7.8 km). The user has since drawn several more rectangles in
the same `OpenData Download Client` (theme ATKIS Verkehr) and dropped the extra
`WFS_atkis_thema_ATKIS_VERKEHR_*.xml` files into `data/raw/atkis_verkehr_th/`.

This script parses **all** of them together, so coverage grows toward the full
AOI. It is otherwise identical to the TASK_07 parser:

1. Stream every source XML, group members by `ObjektartText`.
2. **Deduplicate by `eindeutigerObjektidentifikator`** (the ADV/AAA object id),
   NOT by geometry — the server does not clip features to the requested
   rectangle, so a feature straddling two exports appears whole in both with the
   same id; first occurrence wins. Features without that field fall back to
   `gml:id`.
3. Reproject each geometry EPSG:25832 -> EPSG:4326.
4. Clip to the current Weimar admin boundary (keep every feature whose geometry
   intersects the boundary polygon; geometry left whole).
5. Run every set through `tools/geojson_clean.py` (mandatory).
6. Overwrite `web/layers/data/weimar_atkis_<objektart>.geojson`, one file per
   `ObjektartText` — same names as TASK_07, now with wider coverage. No new
   sidebar grouping here; `layers.json` already lists these layers.
7. Extra QA step for TASK_13: after dedup, flag any two kept features in the same
   layer that have **different** ids but a byte-identical normalised geometry
   (direction-independent). Dedup-by-id cannot catch a real duplicate that was
   assigned two different ids in two exports; this surfaces that case for review
   instead of silently merging or dropping.

Usage:
  python tools/merge_weimar_atkis_verkehr.py
  python tools/merge_weimar_atkis_verkehr.py --src a.xml --src b.xml ...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pyproj
from shapely.geometry import shape
from shapely.prepared import prep

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "atkis_verkehr_th"
BOUNDARY = REPO_ROOT / "web" / "layers" / "data" / "weimar_boundary.geojson"
OUT_DIR = REPO_ROOT / "web" / "layers" / "data"

A = "{http://www.adv-online.de/namespaces/adv/gid/7.1/atkis_thema}"
GML = "{http://www.opengis.net/gml/3.2}"

FEATURE_TAG = A + "ATKIS_VERKEHR"
GEOM_TAG = A + "geom"
EOI_TAG = A + "eindeutigerObjektidentifikator"

TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

LICENCE = "dl-de/by-2-0, © GDI-Th"


def slug(objektart_text: str) -> str:
    s = re.sub(r"^AX_", "", objektart_text)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.split()]


def _ring_4326(poslist_text: str) -> list[list[float]]:
    nums = _floats(poslist_text)
    return [list(TO_4326.transform(nums[i], nums[i + 1])) for i in range(0, len(nums), 2)]


def _round_ring(ring: list[list[float]]) -> list[list[float]]:
    return [[round(x, 7), round(y, 7)] for x, y in ring]


def parse_geometry(geom_el: ET.Element):
    poly = geom_el.find(GML + "Polygon")
    if poly is not None:
        rings = []
        ext = poly.find(f"{GML}exterior/{GML}LinearRing/{GML}posList")
        if ext is None or not ext.text:
            return None
        rings.append(_round_ring(_ring_4326(ext.text)))
        for interior in poly.findall(f"{GML}interior/{GML}LinearRing/{GML}posList"):
            if interior.text:
                rings.append(_round_ring(_ring_4326(interior.text)))
        return {"type": "Polygon", "coordinates": rings}

    line = geom_el.find(GML + "LineString")
    if line is not None:
        pl = line.find(GML + "posList")
        if pl is None or not pl.text:
            return None
        return {"type": "LineString", "coordinates": _round_ring(_ring_4326(pl.text))}

    pt = geom_el.find(GML + "Point")
    if pt is not None:
        pos = pt.find(GML + "pos")
        if pos is None or not pos.text:
            return None
        n = _floats(pos.text)
        x, y = TO_4326.transform(n[0], n[1])
        return {"type": "Point", "coordinates": [round(x, 7), round(y, 7)]}

    return None


def scalar_props(member: ET.Element) -> dict:
    props = {}
    for child in member:
        if child.tag == GEOM_TAG:
            continue
        if len(child) == 0 and child.text and child.text.strip():
            key = child.tag.split("}", 1)[-1]
            props.setdefault(key, child.text.strip())
    props["licence"] = LICENCE
    return props


def load_boundary():
    fc = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    geom = shape(fc["features"][0]["geometry"])
    return geom, prep(geom)


def _geom_key(geom: dict):
    """Direction-independent hashable key for a geometry, coords rounded to ~1 cm."""
    def r(pt):
        return (round(pt[0], 7), round(pt[1], 7))

    t = geom["type"]
    c = geom["coordinates"]
    if t == "Point":
        return (t, r(c))
    if t == "LineString":
        pts = tuple(r(p) for p in c)
        return (t, min(pts, (pts[::-1])))
    if t == "Polygon":
        rings = []
        for ring in c:
            pts = tuple(sorted(r(p) for p in ring))
            rings.append(pts)
        return (t, tuple(sorted(rings)))
    return (t, json.dumps(c, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, action="append", default=None,
                    help="ATKIS_VERKEHR WFS/GML file (repeatable). "
                         "Default: every WFS_atkis_thema_ATKIS_VERKEHR_*.xml in data/raw/atkis_verkehr_th/")
    args = ap.parse_args()

    srcs = args.src or sorted(RAW_DIR.glob("WFS_atkis_thema_ATKIS_VERKEHR_*.xml"))
    srcs = [s for s in srcs if s.exists()]
    if not srcs:
        print("no source files found", file=sys.stderr)
        return 1
    print(f"sources ({len(srcs)}):")
    for s in srcs:
        print(f"  {s}")

    boundary_geom, boundary_prep = load_boundary()
    print(f"\nboundary: {BOUNDARY.name} ({boundary_geom.geom_type}, "
          f"bounds {tuple(round(b, 5) for b in boundary_geom.bounds)})\n")

    groups: dict[str, list[dict]] = {}
    codes: dict[str, str] = {}
    seen_ids: set[str] = set()
    n_total = n_dup = n_nogeom = n_outside = 0
    per_src_members: dict[str, int] = {}

    for src in srcs:
        m = 0
        with open(src, "rb") as f:
            for _, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != FEATURE_TAG:
                    continue
                m += 1
                n_total += 1

                key = elem.findtext(EOI_TAG) or elem.get(GML + "id")
                if key in seen_ids:
                    n_dup += 1
                    elem.clear()
                    continue

                objektart = elem.findtext(A + "ObjektartText") or "UNKNOWN"
                geom_el = elem.find(GEOM_TAG)
                geom = parse_geometry(geom_el) if geom_el is not None else None
                if geom is None:
                    n_nogeom += 1
                    seen_ids.add(key)
                    elem.clear()
                    continue

                try:
                    shp = shape(geom)
                except Exception:
                    n_nogeom += 1
                    seen_ids.add(key)
                    elem.clear()
                    continue
                if not boundary_prep.intersects(shp):
                    n_outside += 1
                    seen_ids.add(key)
                    elem.clear()
                    continue

                props = scalar_props(elem)
                props["ObjektartText"] = objektart
                gml_id = elem.get(GML + "id")
                if gml_id:
                    props.setdefault("gmlId", gml_id)
                codes.setdefault(objektart, props.get("Objektart", ""))
                groups.setdefault(objektart, []).append({
                    "type": "Feature",
                    "properties": props,
                    "geometry": geom,
                })
                seen_ids.add(key)
                elem.clear()
        per_src_members[src.name] = m
        print(f"  parsed {src.name}: {m} members")

    print(f"\n{n_total} members across {len(srcs)} files | {n_dup} duplicate id (skipped) | "
          f"{n_nogeom} without usable geometry | {n_outside} outside the Weimar boundary | "
          f"{sum(len(v) for v in groups.values())} unique kept before cleaning\n")

    manifest = []
    overlap_report: dict[str, list] = {}
    for objektart in sorted(groups):
        feats = groups[objektart]
        gtypes = sorted({fe["geometry"]["type"] for fe in feats})
        kept, _ = clean_features(feats, label=objektart)

        # TASK_13 QA: distinct id, identical geometry
        by_geom: dict = {}
        for fe in kept:
            gk = _geom_key(fe["geometry"])
            by_geom.setdefault(gk, []).append(
                fe["properties"].get("eindeutigerObjektidentifikator")
                or fe["properties"].get("gmlId"))
        collisions = [ids for ids in by_geom.values() if len(ids) > 1]
        if collisions:
            overlap_report[objektart] = collisions

        out = OUT_DIR / f"weimar_atkis_{slug(objektart)}.geojson"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False),
                       encoding="utf-8")
        size_kb = out.stat().st_size / 1024
        print(f"  -> {out.name:<44} {len(kept):5d} feat  [{', '.join(gtypes)}]  {size_kb:.0f} KB")
        manifest.append((objektart, codes.get(objektart, ""), len(kept), gtypes, out.name))

    print("\nlayers.json stub (objektart | code | features | geom | file):")
    for objektart, code, cnt, gtypes, fname in manifest:
        print(f"  {objektart:<28} {code:<7} {cnt:5d}  {'/'.join(gtypes):<20} {fname}")

    print("\nTASK_13 overlap check (distinct id, identical geometry):")
    if not overlap_report:
        print("  none — no geometry-identical feature pairs with different ids in any layer.")
    else:
        for objektart, collisions in overlap_report.items():
            print(f"  {objektart}: {len(collisions)} geometry group(s) with >1 id")
            for ids in collisions[:20]:
                print(f"    {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
