#!/usr/bin/env python3
"""
fetch_weimar_atkis_verkehr.py — split the *native* ATKIS "Verkehr" object
catalogue into one GeoJSON layer per object type (TASK_07).

Source
------
`data/raw/atkis_verkehr_th/WFS_atkis_thema_ATKIS_VERKEHR_*.xml` — a WFS 2.0
FeatureCollection (GML 3.2, EPSG:25832, 3564 `atkis_thema:ATKIS_VERKEHR`
members) the user downloaded **by hand** through the `OpenData Download Client`
on geoportal.thueringen.de (a Link11 JS-captcha blocks automated access to that
one page — see DATA.md / DATA_LOG.md). This is the real ATKIS
Basis-DLM object catalogue, **not** the trimmed INSPIRE "Verkehrsnetze"
harmonisation that `tools/fetch_weimar_roads_atkis.py` / `weimar_roads` use.

Unlike the INSPIRE road feed, every feature here carries `ObjektartText`, which
is a genuine *object-type* split (street axis vs. path vs. field-track vs.
railway vs. structure), not a single significance ranking. TASK_06/06b proved
the INSPIRE feed cannot be split by attribute; this feed can, natively.

What this script does
---------------------
1. Streams the XML, groups members by `ObjektartText`.
2. Reprojects each geometry EPSG:25832 -> EPSG:4326 (project convention).
3. Clips to the current Weimar admin boundary
   (`web/layers/data/weimar_boundary.geojson`) — keeps every feature whose
   4326 geometry intersects the boundary polygon, geometry left whole (same
   "keep-if-it-touches-the-AOI" rule the other vector layers use). The source
   bbox (~7.1 x 7.8 km) already sits inside the official AOI, so this drops
   very little — the exact drop count is printed so we can tell whether the
   manual download area needs to be re-drawn wider.
4. Runs every set through `tools/geojson_clean.py` (mandatory; a no-op for the
   LineString / Point sets, real repair for the Polygon sets).
5. Writes `web/layers/data/weimar_atkis_<objektart>.geojson`, one file per
   `ObjektartText` — **nothing merged, nothing grouped**. The user wants to see
   each type as its own sidebar layer first and decide later what to combine or
   drop.

All scalar (text-only) child elements of each member are copied verbatim into
`properties` (localname -> string), minus the geometry, so the original
`ObjektartText`, `Objektart` code, `Funktion`, `Widmung`, `Befahrbarkeit`,
`besondereVerkehrsbedeutung`, widths, names, etc. are all preserved for the
visual review.

`weimar_roads`, `weimar_pedestrian_osm` and every existing layer are left
untouched — the layers this script adds are purely additive, off by default.

Usage:
  python tools/fetch_weimar_atkis_verkehr.py
  python tools/fetch_weimar_atkis_verkehr.py --src path/to/other.xml
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
DEFAULT_SRC = next(iter(sorted(RAW_DIR.glob("WFS_atkis_thema_ATKIS_VERKEHR_*.xml"))), None)
BOUNDARY = REPO_ROOT / "web" / "layers" / "data" / "weimar_boundary.geojson"
OUT_DIR = REPO_ROOT / "web" / "layers" / "data"

A = "{http://www.adv-online.de/namespaces/adv/gid/7.1/atkis_thema}"
GML = "{http://www.opengis.net/gml/3.2}"
WFS = "{http://www.opengis.net/wfs/2.0}"

FEATURE_TAG = A + "ATKIS_VERKEHR"
GEOM_TAG = A + "geom"

TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

LICENCE = "dl-de/by-2-0, © GDI-Th"


def slug(objektart_text: str) -> str:
    """AX_WegPfadSteig -> wegpfadsteig ; AX_BauwerkImGewaesserbereich -> bauwerkimgewaesserbereich."""
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
    """Return a GeoJSON geometry dict in EPSG:4326, or None."""
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
    """Every text-only child (recursively, one level of nesting for gml:name) -> {localname: text}."""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC,
                    help="ATKIS_VERKEHR WFS/GML file (default: newest in data/raw/atkis_verkehr_th/)")
    args = ap.parse_args()

    if not args.src or not args.src.exists():
        print(f"source file not found: {args.src}", file=sys.stderr)
        return 1

    boundary_geom, boundary_prep = load_boundary()
    print(f"boundary: {BOUNDARY.name} ({boundary_geom.geom_type}, bounds {tuple(round(b, 5) for b in boundary_geom.bounds)})")

    groups: dict[str, list[dict]] = {}
    codes: dict[str, str] = {}
    n_total = n_nogeom = n_outside = 0

    with open(args.src, "rb") as f:
        for _, elem in ET.iterparse(f, events=("end",)):
            if elem.tag != FEATURE_TAG:
                continue
            n_total += 1
            objektart = elem.findtext(A + "ObjektartText") or "UNKNOWN"
            geom_el = elem.find(GEOM_TAG)
            geom = parse_geometry(geom_el) if geom_el is not None else None
            if geom is None:
                n_nogeom += 1
                elem.clear()
                continue

            try:
                shp = shape(geom)
            except Exception:
                n_nogeom += 1
                elem.clear()
                continue
            if not boundary_prep.intersects(shp):
                n_outside += 1
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
            elem.clear()

    print(f"\n{n_total} members parsed | {n_nogeom} without usable geometry | "
          f"{n_outside} outside the Weimar boundary | {n_total - n_nogeom - n_outside} kept\n")

    manifest = []
    for objektart in sorted(groups):
        feats = groups[objektart]
        gtypes = sorted({fe["geometry"]["type"] for fe in feats})
        kept, _ = clean_features(feats, label=objektart)
        out = OUT_DIR / f"weimar_atkis_{slug(objektart)}.geojson"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False),
                       encoding="utf-8")
        size_kb = out.stat().st_size / 1024
        print(f"  -> {out.name:<44} {len(kept):5d} feat  [{', '.join(gtypes)}]  {size_kb:.0f} KB")
        manifest.append((objektart, codes.get(objektart, ""), len(kept), gtypes, out.name))

    print("\nlayers.json stub (objektart | code | features | geom | file):")
    for objektart, code, cnt, gtypes, fname in manifest:
        print(f"  {objektart:<28} {code:<7} {cnt:5d}  {'/'.join(gtypes):<20} {fname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
