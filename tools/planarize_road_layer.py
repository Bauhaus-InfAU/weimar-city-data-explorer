#!/usr/bin/env python3
"""
planarize_road_layer.py — TASK_21: per-file topology cleanup for the three
ATKIS road layers, each file treated completely independently.

Deliberately does NOT merge the three files together and does NOT compute
connectivity/graph components — see TASK_21's task file for why (that's
blocked on deciding which layers form which analysis network, likely
TASK_18's job).

Per file, in EPSG:25832 (metres):

1. **Endpoint snapping** — cluster every line's start/end point with any
   other line-end within `--tolerance` metres (default 1.5 m, i.e. the
   "~1-2 m" the task asks for) using a simple union-find over an STRtree
   neighbour search, then move every endpoint in a cluster of size > 1 to
   that cluster's centroid. Interior vertices are untouched. This closes
   small digitising gaps between lines that were meant to meet but don't
   quite touch, WITHIN this one file only.

2. **Planarization** — `shapely.ops.unary_union` over all (now-snapped)
   lines in the file, which nodes the network at every interior crossing
   (not just endpoints) and returns the geometry pre-split at those nodes.
   Each output part is matched back to its source feature (by checking
   which original snapped line it lies on, via an STRtree distance test on
   the part's midpoint) so the original ATKIS attributes are preserved on
   every split piece. A line split into N pieces by this step yields N
   output features that all carry the same original properties (including
   the same `eindeutigerObjektidentifikator`) — a deliberate, logged
   trade-off; nothing downstream currently assumes 1 feature per id within
   a single file.

3. `geojson_clean.py` (no-op for LineStrings, kept for pattern consistency).

4. Overwrite `web/layers/data/weimar_atkis_<type>.geojson`.

Usage:
  python tools/planarize_road_layer.py [--tolerance 1.5] [--only <type>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyproj
from shapely.geometry import shape, mapping, LineString, MultiLineString
from shapely.ops import unary_union, transform as shp_transform
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LAYER_DIR = REPO_ROOT / "web" / "layers" / "data"

TYPES = ["strassenachse", "fahrwegachse", "wegpfadsteig"]

TO_25832 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
_to25832 = lambda geom: shp_transform(lambda x, y, z=None: TO_25832.transform(x, y), geom)
_to4326 = lambda geom: shp_transform(lambda x, y, z=None: TO_4326.transform(x, y), geom)


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[ri] = rj


def snap_endpoints(lines_m: list[LineString], tolerance: float) -> tuple[list[LineString], int, int]:
    """Cluster line endpoints within `tolerance` m and move them to the cluster
    centroid. Returns (new_lines, n_clusters_moved, n_endpoints_moved)."""
    from shapely.geometry import Point

    endpoints = []  # (feature_idx, "start"|"end", (x, y))
    for fi, ln in enumerate(lines_m):
        coords = list(ln.coords)
        endpoints.append((fi, "start", coords[0]))
        endpoints.append((fi, "end", coords[-1]))

    pts = [Point(xy) for (_, _, xy) in endpoints]
    tree = STRtree(pts)
    uf = UnionFind(len(pts))
    for i, p in enumerate(pts):
        for j in tree.query(p.buffer(tolerance)):
            j = int(j)
            if j <= i:
                continue
            if p.distance(pts[j]) <= tolerance:
                uf.union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(len(pts)):
        clusters.setdefault(uf.find(i), []).append(i)

    new_coord = {}  # endpoint index -> (x, y)
    n_clusters_moved = 0
    n_endpoints_moved = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        xs = [endpoints[m][2][0] for m in members]
        ys = [endpoints[m][2][1] for m in members]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        moved_any = False
        for m in members:
            orig = endpoints[m][2]
            if orig != (cx, cy):
                new_coord[m] = (cx, cy)
                n_endpoints_moved += 1
                moved_any = True
        if moved_any:
            n_clusters_moved += 1

    new_lines: list[LineString] = []
    for fi, ln in enumerate(lines_m):
        coords = list(ln.coords)
        start_idx = fi * 2
        end_idx = fi * 2 + 1
        if start_idx in new_coord:
            coords[0] = new_coord[start_idx]
        if end_idx in new_coord:
            coords[-1] = new_coord[end_idx]
        new_lines.append(LineString(coords))

    return new_lines, n_clusters_moved, n_endpoints_moved


MIN_PART_LEN_M = 0.01  # drop degenerate zero-length slivers unary_union can emit
                       # at a shared vertex (two lines crossing exactly at an
                       # existing node) — not real segments, just noise.


def planarize(lines_m: list[LineString]) -> tuple[list[LineString], int]:
    """unary_union to node the network at interior crossings; split into parts."""
    merged = unary_union(lines_m)
    if merged.is_empty:
        return [], 0
    if isinstance(merged, LineString):
        parts = [merged]
    elif isinstance(merged, MultiLineString):
        parts = list(merged.geoms)
    else:
        # GeometryCollection (rare: e.g. touching produces a Point mixed in) — keep lines only
        parts = [g for g in getattr(merged, "geoms", []) if isinstance(g, LineString)]

    kept = [p for p in parts if p.length >= MIN_PART_LEN_M]
    n_degenerate = len(parts) - len(kept)
    return kept, n_degenerate


def match_parts_to_features(parts: list[LineString], lines_m: list[LineString],
                             features: list[dict], eps: float = 0.02) -> list[dict]:
    """Assign each split `part` the properties of the original feature it lies on."""
    tree = STRtree(lines_m)
    out = []
    unmatched = 0
    for part in parts:
        mid = part.interpolate(0.5, normalized=True)
        cand_idxs = tree.query(mid.buffer(eps))
        best_i, best_d = None, None
        for ci in cand_idxs:
            ci = int(ci)
            d = lines_m[ci].distance(mid)
            if best_d is None or d < best_d:
                best_i, best_d = ci, d
        if best_i is None or best_d is None or best_d > eps:
            # fall back: nearest of all lines (should not normally trigger)
            best_i = min(range(len(lines_m)), key=lambda k: lines_m[k].distance(mid))
            unmatched += 1
        props = dict(features[best_i]["properties"])
        out.append({
            "type": "Feature",
            "properties": props,
            "geometry": mapping(_to4326(part)),
        })
    if unmatched:
        print(f"     !! {unmatched} split part(s) needed the slow fallback match "
              f"(no original line within {eps} m of its midpoint)")
    return out


def process_file(slug: str, tolerance: float) -> None:
    path = LAYER_DIR / f"weimar_atkis_{slug}.geojson"
    fc = json.loads(path.read_text(encoding="utf-8"))
    features = fc["features"]
    n_before = len(features)

    lines_m = [_to25832(shape(f["geometry"])) for f in features]

    snapped_m, n_clusters, n_endpoints = snap_endpoints(lines_m, tolerance)

    parts, n_degenerate = planarize(snapped_m)
    new_features = match_parts_to_features(parts, snapped_m, features)

    kept, summary = clean_features(new_features, label=slug)

    n_after = len(kept)
    print(f"[{slug}]")
    print(f"  segments before:              {n_before}")
    print(f"  endpoint clusters snapped:     {n_clusters} (gaps closed, tolerance {tolerance} m)")
    print(f"  endpoints moved:               {n_endpoints}")
    print(f"  degenerate slivers dropped:    {n_degenerate} (< {MIN_PART_LEN_M} m, node-on-node artifacts)")
    print(f"  segments after planarization:  {n_after}")
    print(f"  nodes added (segment growth):  {n_after - n_before}")

    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  -> wrote {path.relative_to(REPO_ROOT)}: {n_after} features\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=float, default=1.5,
                     help="endpoint-snap tolerance in metres (default 1.5)")
    ap.add_argument("--only", choices=TYPES, default=None,
                     help="process a single layer instead of all three")
    args = ap.parse_args()

    types = [args.only] if args.only else TYPES
    print(f"=== TASK_21 — per-file planarize (endpoint snap {args.tolerance} m + unary_union) ===\n")
    for slug in types:
        process_file(slug, args.tolerance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
