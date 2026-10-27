#!/usr/bin/env python3
"""TASK_14 QA: Hausdorff near-duplicate scan + coverage metric + visual seam check.

Re-uses the same parsing as tools/merge_weimar_atkis_verkehr.py so the feature set
matches the layers just written. Splits kept features into "old" (id first seen in
the 4 pre-TASK_14 exports) vs "added" (id first seen in the 4 new exports) for the
visual seam check.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pyproj
import numpy as np
from shapely.geometry import shape, box
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree
from shapely.prepared import prep
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "data/raw/atkis_verkehr_th"
BOUNDARY = REPO / "web/layers/data/weimar_boundary.geojson"
OUT = REPO / "output"

A = "{http://www.adv-online.de/namespaces/adv/gid/7.1/atkis_thema}"
GML = "{http://www.opengis.net/gml/3.2}"
FEATURE_TAG = A + "ATKIS_VERKEHR"
GEOM_TAG = A + "geom"
EOI_TAG = A + "eindeutigerObjektidentifikator"

TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
TO_25832 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
_to25832 = lambda geom: shp_transform(lambda x, y, z=None: TO_25832.transform(x, y), geom)

# pre-TASK_14 exports (TASK_07 + TASK_13 batch)
OLD_FILES = {
    "WFS_atkis_thema_ATKIS_VERKEHR_2026831_19479.xml",
    "WFS_atkis_thema_ATKIS_VERKEHR_202691_124657.xml",
    "WFS_atkis_thema_ATKIS_VERKEHR_202691_124831.xml",
    "WFS_atkis_thema_ATKIS_VERKEHR_202691_12501.xml",
}


def _floats(t): return [float(v) for v in t.split()]
def _ring(t):
    n = _floats(t)
    return [list(TO_4326.transform(n[i], n[i+1])) for i in range(0, len(n), 2)]
def _rr(r): return [[round(x, 7), round(y, 7)] for x, y in r]


def parse_geometry(el):
    poly = el.find(GML + "Polygon")
    if poly is not None:
        ext = poly.find(f"{GML}exterior/{GML}LinearRing/{GML}posList")
        if ext is None or not ext.text:
            return None
        rings = [_rr(_ring(ext.text))]
        for it in poly.findall(f"{GML}interior/{GML}LinearRing/{GML}posList"):
            if it.text:
                rings.append(_rr(_ring(it.text)))
        return {"type": "Polygon", "coordinates": rings}
    line = el.find(GML + "LineString")
    if line is not None:
        pl = line.find(GML + "posList")
        if pl is None or not pl.text:
            return None
        return {"type": "LineString", "coordinates": _rr(_ring(pl.text))}
    pt = el.find(GML + "Point")
    if pt is not None:
        pos = pt.find(GML + "pos")
        if pos is None or not pos.text:
            return None
        n = _floats(pos.text)
        x, y = TO_4326.transform(n[0], n[1])
        return {"type": "Point", "coordinates": [round(x, 7), round(y, 7)]}
    return None


def main():
    bfc = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    bpoly = shape(bfc["features"][0]["geometry"])
    bprep = prep(bpoly)
    bpoly_m = _to25832(bpoly)
    print(f"boundary area: {bpoly_m.area/1e6:.2f} km2\n")

    srcs = sorted(RAW.glob("WFS_atkis_thema_ATKIS_VERKEHR_*.xml"))
    groups = {}          # objektart -> list[(geom_dict, is_added)]
    seen = set()
    all_pts_m = []       # every vertex, EPSG:25832, for coverage
    added_lines_str, old_lines_str = [], []  # AX_Strassenachse geoms (lon/lat) for plot

    for src in srcs:
        is_added = src.name not in OLD_FILES
        with open(src, "rb") as f:
            for _, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != FEATURE_TAG:
                    continue
                key = elem.findtext(EOI_TAG) or elem.get(GML + "id")
                if key in seen:
                    elem.clear(); continue
                seen.add(key)
                objektart = elem.findtext(A + "ObjektartText") or "UNKNOWN"
                gel = elem.find(GEOM_TAG)
                geom = parse_geometry(gel) if gel is not None else None
                elem.clear()
                if geom is None:
                    continue
                try:
                    shp = shape(geom)
                except Exception:
                    continue
                if not bprep.intersects(shp):
                    continue
                groups.setdefault(objektart, []).append((geom, is_added))
                gm = _to25832(shp)
                xs, ys = gm.coords.xy if geom["type"] != "Polygon" else gm.exterior.coords.xy
                all_pts_m.extend(zip(np.asarray(xs), np.asarray(ys)))
                if objektart == "AX_Strassenachse":
                    (added_lines_str if is_added else old_lines_str).append(shp)

    total = sum(len(v) for v in groups.values())
    n_added = sum(1 for v in groups.values() for _, a in v if a)
    print(f"kept {total} features ({n_added} whose id first appears in a new export)\n")

    # ---- 1. Hausdorff near-duplicate scan --------------------------------------
    print("=== Hausdorff near-duplicate scan (EPSG:25832, pair dist < 0.5 m, "
          "symmetric Hausdorff < 1.0 m) ===")
    any_hd = False
    for objektart in sorted(groups):
        geoms_m = [_to25832(shape(g)) for g, _ in groups[objektart]]
        tree = STRtree(geoms_m)
        flagged = []
        for i, gi in enumerate(geoms_m):
            for j in tree.query(gi.buffer(0.5)):
                if j <= i:
                    continue
                gj = geoms_m[j]
                if gi.distance(gj) >= 0.5:
                    continue
                hd = max(gi.hausdorff_distance(gj), gj.hausdorff_distance(gi))
                if hd < 1.0:
                    flagged.append((i, j, round(hd, 3)))
        tag = "OK" if not flagged else f"!! {len(flagged)} PAIR(S)"
        print(f"  {objektart:<30} {len(geoms_m):5d} feat   {tag}")
        for i, j, hd in flagged[:10]:
            print(f"      idx {i} <-> {j}  hausdorff={hd} m")
        any_hd |= bool(flagged)
    print("  => " + ("PROBLEM PAIRS FOUND — see above" if any_hd
                      else "clean, 0 near-duplicate pairs in any layer") + "\n")

    # ---- 2. Coverage metric --------------------------------------------------
    print("=== Coverage (share of city polygon within 250 m of any feature, "
          "500 m grid) ===")
    pts = np.array(all_pts_m)
    minx, miny, maxx, maxy = bpoly_m.bounds
    fminx, fminy = pts[:, 0].min(), pts[:, 1].min()
    fmaxx, fmaxy = pts[:, 0].max(), pts[:, 1].max()
    print(f"  feature vertex bbox: {(fmaxx-fminx)/1000:.1f} x {(fmaxy-fminy)/1000:.1f} km")
    from shapely.geometry import MultiPoint
    hull_m = MultiPoint([tuple(p) for p in pts]).convex_hull
    print(f"  feature convex hull: {hull_m.area/1e6:.1f} km2")

    step = 500.0
    radius = 250.0
    # grid cell centres inside the boundary
    gx = np.arange(minx + step/2, maxx, step)
    gy = np.arange(miny + step/2, maxy, step)
    GX, GY = np.meshgrid(gx, gy)
    cells = np.column_stack([GX.ravel(), GY.ravel()])
    inside = np.array([bpoly_m.contains(box(cx-step/2, cy-step/2, cx+step/2, cy+step/2).centroid)
                       for cx, cy in cells])
    cells_in = cells[inside]
    # nearest feature-vertex distance per cell centre (KD-tree)
    from scipy.spatial import cKDTree
    kt = cKDTree(pts)
    d, _ = kt.query(cells_in, k=1)
    covered = (d <= radius).sum()
    frac = covered / len(cells_in)
    print(f"  grid cells inside boundary: {len(cells_in)}  "
          f"covered (<= {radius:.0f} m): {covered}  => {frac*100:.1f}% "
          f"(~{frac*bpoly_m.area/1e6:.1f} km2 of {bpoly_m.area/1e6:.1f} km2)\n")

    # ---- 3. Visual seam check ---------------------------------------------------
    fig, ax = plt.subplots(figsize=(11, 12))
    bx, by = bpoly.exterior.xy
    ax.plot(bx, by, color="black", lw=1.2, zorder=5, label="admin boundary")
    for ln in old_lines_str:
        xs, ys = ln.coords.xy
        ax.plot(xs, ys, color="#b0b0b0", lw=0.6, zorder=2)
    for ln in added_lines_str:
        xs, ys = ln.coords.xy
        ax.plot(xs, ys, color="#d1361b", lw=0.7, zorder=3)
    ax.plot([], [], color="#b0b0b0", lw=1.5,
            label=f"AX_Strassenachse — old ({len(old_lines_str)})")
    ax.plot([], [], color="#d1361b", lw=1.5,
            label=f"AX_Strassenachse — added ({len(added_lines_str)})")
    ax.set_aspect("equal")
    ax.set_title("TASK_14 — ATKIS Strassenachse: existing (grey) vs newly added (red)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    p = OUT / "task14_strassenachse_seams.png"
    fig.savefig(p, dpi=110)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
