#!/usr/bin/env python3
"""
prepare_ss_final.py — TASK_35: bring the user's hand-drawn Space Syntax networks (car + pedestrian,
drawn by hand in QGIS, folder "new fo ss" next to/inside the project) into the platform.

Her drawing is the source of truth. This script never deletes, simplifies, straightens or thins a
real line — it only:
  A. cleans technical junk within each layer (exact duplicates, zero-length segments, self-overlap)
     and nodes real topology: line ends snap onto the nearest node/vertex/line (undershoot/overshoot
     fixed), real crossings become real graph nodes (lines split there), except bridge/unlink pairs.
  B. makes car and pedestrian meet at IDENTICAL shared vertices: a pedestrian end near car snaps onto
     it (onto an existing car node if close, else projected onto the car line); the same vertex is
     spliced into car's own coordinate list (car objects are not split unless SPLIT_CAR_AT_JUNCTIONS);
     an at-grade pedestrian x car crossing splits pedestrian and gets the same vertex spliced into car.
  C. clips both layers to the Base border working plane (web/layers/base_border.json) — Space Syntax
     is scoped to the city centre only, per TASK_35. Unclipped versions are kept on disk.
  D. writes the platform's final GeoJSON files and wires them into web/layers/layers.json.

Rerun after every new edit in QGIS:  python tools/prepare_ss_final.py
Source files in "new fo ss" / "new for ss" are never modified; an unchanged copy + checksums lives in
data/raw/ss_manual_input/.
"""
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import substring
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
LAYERS_JSON = ROOT / "web" / "layers" / "layers.json"
BORDER_JSON = ROOT / "web" / "layers" / "base_border.json"
RAW_OUT = ROOT / "data" / "raw" / "ss_manual_input"
UNCLIPPED_OUT = ROOT / "data" / "processed" / "space_syntax_final_unclipped"
REVIEW_DIR = DATA  # review files live next to the platform data, like every other SS review layer

TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True).transform
FROM_UTM = Transformer.from_crs(25832, 4326, always_xy=True).transform

# --------------------------------------------------------------------------- parameters
SNAP_TOL = 1.5                  # Part A: end -> node/vertex/line, within one layer (m)
JUNCTION_TOL = 2.0               # Part B: pedestrian end -> car line (task allows up to 2 m)
CAR_NODE_TOL = 3.0               # Part B: snap onto an existing car vertex within this, else project
SPLIT_CAR_AT_JUNCTIONS = False   # Part B: True cuts car into separate objects at every ped join
UNSNAPPED_REPORT_TOL = 5.0       # Part B: report free ends within this of a foreign line, unjoined
MIN_CLIP_PIECE_LEN = 1.0         # Part C: drop clipped fragments shorter than this (m)
SNAP_REVIEW_TOL = 3.0            # Part A.5 / B: report moves bigger than this
VERTEX_EPS = 0.02                # "already sits on a vertex" tolerance (m)
UNLINK_TOL = 3.0                 # Part A.2: crossing within this of a *_unlinks.geojson point -> skip


# --------------------------------------------------------------------------- input discovery
def find_input_dir():
    candidates = []
    for base in (ROOT, ROOT.parent):
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            norm = d.name.lower().replace(" ", "").replace("_", "").replace("-", "")
            if "new" in norm and "ss" in norm:
                candidates.append(d)
    for d in candidates:
        files = list(d.iterdir())
        car = next((f for f in files if f.is_file() and f.stem.lower().startswith("car")
                    and f.suffix.lower() in (".geojson", ".json", ".shp", ".gpkg")), None)
        ped = next((f for f in files if f.is_file() and f.stem.lower().startswith("ped")
                    and f.suffix.lower() in (".geojson", ".json", ".shp", ".gpkg")), None)
        if car and ped:
            return d, car, ped
    raise SystemExit(
        "Could not find the folder with her hand-drawn networks (expected something like "
        "'new for ss' next to or inside the project, containing a car.* and a ped*.* file). "
        f"Looked in: {ROOT} and {ROOT.parent}")


def _gpkg_geom_to_shapely(blob):
    from shapely import wkb as shapely_wkb
    flags = blob[3]
    envelope_len = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 0x07]
    offset = 8 + envelope_len
    return shapely_wkb.loads(blob[offset:])


def load_features(path):
    """Returns (features, is_utm). Supports GeoJSON natively; Shapefile via pyshp; GeoPackage via
    sqlite3 + shapely.wkb (no native GIS libraries — Smart App Control friendly, per TASK_35)."""
    ext = path.suffix.lower()
    if ext in (".geojson", ".json"):
        fc = json.loads(path.read_text(encoding="utf-8"))
        crs_name = (fc.get("crs") or {}).get("properties", {}).get("name", "")
        is_utm = "25832" in crs_name
        return fc["features"], is_utm
    if ext == ".shp":
        try:
            import shapefile  # pyshp
        except ImportError:
            raise SystemExit(f"{path}: Shapefile input needs `pip install pyshp`, or export from QGIS "
                              f"as GeoJSON (EPSG:25832) instead.")
        sf = shapefile.Reader(str(path))
        fields = [f[0] for f in sf.fields[1:]]
        feats = []
        for sr in sf.shapeRecords():
            props = dict(zip(fields, sr.record))
            feats.append({"type": "Feature", "properties": props, "geometry": sr.shape.__geo_interface__})
        prj = path.with_suffix(".prj")
        is_utm = False
        if prj.exists():
            wkt = prj.read_text(encoding="utf-8", errors="ignore")
            is_utm = "25832" in wkt or "UTM zone 32N" in wkt
        return feats, is_utm
    if ext == ".gpkg":
        import sqlite3
        con = sqlite3.connect(str(path))
        cur = con.cursor()
        cur.execute("SELECT table_name, geometry_column_name, srs_id FROM gpkg_geometry_columns")
        table, geomcol, srs_id = cur.fetchone()
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        cur.execute(f"SELECT * FROM {table}")
        feats = []
        for row in cur.fetchall():
            row_d = dict(zip(cols, row))
            blob = row_d.pop(geomcol)
            geom = _gpkg_geom_to_shapely(blob)
            feats.append({"type": "Feature", "properties": row_d, "geometry": mapping(geom)})
        return feats, srs_id == 25832
    raise SystemExit(f"{path}: unsupported format ({ext}). Export from QGIS as GeoJSON, EPSG:25832.")


def explode(feats, network):
    out, dropped = [], 0
    for f in feats:
        g = f.get("geometry")
        if not g:
            dropped += 1
            continue
        props = dict(f.get("properties") or {})
        if g["type"] == "LineString":
            out.append({"props": props, "coords": [tuple(c[:2]) for c in g["coordinates"]]})
        elif g["type"] == "MultiLineString":
            for part in g["coordinates"]:
                out.append({"props": dict(props), "coords": [tuple(c[:2]) for c in part]})
        else:
            dropped += 1
    if dropped:
        print(f"  [{network}] dropped {dropped} non-(Multi)LineString feature(s)")
    return out


def to_utm(lines, is_utm):
    if is_utm:
        return lines
    for l in lines:
        l["coords"] = [TO_UTM(x, y) for x, y in l["coords"]]
    return lines


# --------------------------------------------------------------------------- Part A.4 — technical cleanup
def collapse_dupe_points(coords, eps=0.01):
    out = [coords[0]]
    for c in coords[1:]:
        if math.dist(c, out[-1]) > eps:
            out.append(c)
    return out


def clean_technical(lines, network, report):
    from shapely.ops import unary_union
    kept, seen = [], {}
    n_zero = n_dup = n_overlap_trimmed = n_overlap_flagged = 0
    for idx, l in enumerate(lines):
        c = collapse_dupe_points(l["coords"])
        if len(c) < 2:
            n_zero += 1
            continue
        line = LineString(c)
        merged = unary_union(line)
        if merged.geom_type == "LineString" and merged.length < line.length - 0.05:
            c = list(merged.coords)
            line = merged
            n_overlap_trimmed += 1
        elif merged.geom_type != "LineString":
            n_overlap_flagged += 1  # ambiguous self-touch; left untouched, only flagged
        sig = tuple((round(x, 2), round(y, 2)) for x, y in c)
        sig_key = min(sig, tuple(reversed(sig)))
        if sig_key in seen:
            n_dup += 1
            continue
        seen[sig_key] = idx
        l["coords"] = c
        kept.append(l)
    report[f"{network}_dropped_zero_length"] = n_zero
    report[f"{network}_dropped_exact_duplicate"] = n_dup
    report[f"{network}_self_overlap_trimmed"] = n_overlap_trimmed
    report[f"{network}_self_overlap_flagged_untouched"] = n_overlap_flagged
    print(f"  [{network}] technical cleanup: {n_zero} zero-length, {n_dup} exact duplicates dropped, "
          f"{n_overlap_trimmed} self-overlaps trimmed, {n_overlap_flagged} ambiguous self-touches flagged (left as-is)")
    return kept


# --------------------------------------------------------------------------- Part A.1/3 — within-layer snap
def snap_ends_within_layer(lines, tol, network, report):
    ends = [(i, k) for i in range(len(lines)) for k in (0, -1)]
    pts = [Point(lines[i]["coords"][k]) for i, k in ends]
    tree = STRtree(pts)
    parent = list(range(len(ends)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for a, p in enumerate(pts):
        for b in tree.query(p.buffer(tol)):
            b = int(b)
            if b > a and p.distance(pts[b]) <= tol:
                union(a, b)

    groups = defaultdict(list)
    for a in range(len(ends)):
        groups[find(a)].append(a)

    moves = []
    singles = []
    for g in groups.values():
        if len(g) == 1:
            singles.append(g[0])
            continue
        cx = sum(pts[a].x for a in g) / len(g)
        cy = sum(pts[a].y for a in g) / len(g)
        for a in g:
            i, k = ends[a]
            old = lines[i]["coords"][k]
            d = math.dist(old, (cx, cy))
            if d > 1e-6:
                lines[i]["coords"][k] = (cx, cy)
                if d > SNAP_REVIEW_TOL:
                    moves.append({"network": network, "phase": "A.1-cluster", "at": [cx, cy], "dist": round(d, 2)})

    geoms = [LineString(l["coords"]) for l in lines]
    ltree = STRtree(geoms)
    inserted = 0
    for a in singles:
        i, k = ends[a]
        p = Point(lines[i]["coords"][k])
        best, bd = None, tol
        for j in ltree.query(p.buffer(tol)):
            j = int(j)
            if j == i:
                continue
            d = geoms[j].distance(p)
            if d < bd:
                best, bd = j, d
        if best is None:
            continue
        c = lines[best]["coords"]
        line = geoms[best]
        s = line.project(p)
        q = line.interpolate(s)
        qxy = (q.x, q.y)
        old = lines[i]["coords"][k]
        d = math.dist(old, qxy)
        if any(math.dist(qxy, v) < VERTEX_EPS for v in c):
            lines[i]["coords"][k] = qxy
        else:
            acc = 0.0
            for idx2 in range(len(c) - 1):
                seg = math.dist(c[idx2], c[idx2 + 1])
                if acc + seg >= s - 1e-9:
                    c.insert(idx2 + 1, qxy)
                    break
                acc += seg
            lines[i]["coords"][k] = qxy
            inserted += 1
            geoms[best] = LineString(c)
        if d > SNAP_REVIEW_TOL:
            moves.append({"network": network, "phase": "A.3-end-to-line", "at": list(qxy), "dist": round(d, 2)})

    report[f"{network}_end_clusters"] = sum(1 for g in groups.values() if len(g) > 1)
    report[f"{network}_end_to_line_inserted"] = inserted
    report["snap_moves"].extend(moves)
    print(f"  [{network}] end snapping: {report[f'{network}_end_clusters']} clusters joined, "
          f"{inserted} undershoot/overshoot ends tied onto another line, {len(moves)} moves > {SNAP_REVIEW_TOL} m")
    return lines


# --------------------------------------------------------------------------- Part A.2 — real crossings -> nodes
def node_at_intersections(lines, network, unlink_pts, report):
    geoms = [LineString(l["coords"]) for l in lines]
    tree = STRtree(geoms)
    split_params = [set() for _ in lines]
    n_crossings = n_unlink_skipped = 0
    for i in range(len(lines)):
        a = geoms[i]
        bridge_i = bool(lines[i]["props"].get("bridge") or lines[i]["props"].get("unlink"))
        for j in tree.query(a):
            j = int(j)
            if j <= i:
                continue
            b = geoms[j]
            bridge_j = bool(lines[j]["props"].get("bridge") or lines[j]["props"].get("unlink"))
            x = a.intersection(b)
            if x.is_empty:
                continue
            if x.geom_type == "Point":
                pts = [x]
            elif x.geom_type == "MultiPoint":
                pts = list(x.geoms)
            else:
                continue  # collinear overlap: a technical-cleanup concern, not a crossing
            for pt in pts:
                if bridge_i or bridge_j:
                    n_unlink_skipped += 1
                    continue
                if any(pt.distance(Point(u)) <= UNLINK_TOL for u in unlink_pts):
                    n_unlink_skipped += 1
                    continue
                on_a = any(pt.distance(Point(v)) < VERTEX_EPS for v in a.coords)
                on_b = any(pt.distance(Point(v)) < VERTEX_EPS for v in b.coords)
                if on_a and on_b:
                    continue
                split_params[i].add(round(a.project(pt), 6))
                split_params[j].add(round(b.project(pt), 6))
                n_crossings += 1

    out = []
    for i, l in enumerate(lines):
        length = geoms[i].length
        params = sorted(p for p in split_params[i] if VERTEX_EPS < p < length - VERTEX_EPS)
        if not params:
            out.append(l)
            continue
        bounds = [0.0] + params + [length]
        for a0, b0 in zip(bounds[:-1], bounds[1:]):
            if b0 - a0 < 0.02:
                continue
            piece = substring(geoms[i], a0, b0)
            out.append({"props": dict(l["props"]), "coords": list(piece.coords)})

    report[f"{network}_crossings_noded"] = n_crossings
    report[f"{network}_unlink_or_bridge_skipped"] = n_unlink_skipped
    print(f"  [{network}] intersections: {n_crossings} real crossings split into graph nodes, "
          f"{n_unlink_skipped} skipped (bridge/unlink, different level)")
    return out


def load_unlink_points():
    pts = []
    for f in DATA.glob("weimar_ss_*_unlinks.geojson"):
        try:
            fc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for feat in fc.get("features", []):
            g = feat.get("geometry") or {}
            if g.get("type") == "Point":
                pts.append(TO_UTM(*g["coordinates"][:2]))
    return pts


# --------------------------------------------------------------------------- Part B — car/pedestrian joins
def join_networks(car_lines, ped_lines, report):
    car_geoms = [LineString(l["coords"]) for l in car_lines]
    car_tree = STRtree(car_geoms)
    node_pts = [Point(v) for l in car_lines for v in (l["coords"][0], l["coords"][-1])]
    node_tree = STRtree(node_pts)

    def snap_point_to_car(p):
        near_nodes = [int(j) for j in node_tree.query(p.buffer(CAR_NODE_TOL))
                      if node_pts[int(j)].distance(p) <= CAR_NODE_TOL]
        if near_nodes:
            best = min(near_nodes, key=lambda j: node_pts[j].distance(p))
            return node_pts[best].coords[0], None
        near_lines = [int(j) for j in car_tree.query(p.buffer(JUNCTION_TOL))
                      if car_geoms[int(j)].distance(p) <= JUNCTION_TOL]
        if not near_lines:
            return None, None
        best = min(near_lines, key=lambda j: car_geoms[j].distance(p))
        line = car_geoms[best]
        s = line.project(p)
        q = line.interpolate(s)
        return (q.x, q.y), best

    inserts_into_car = []  # (car_line_index, (x, y))
    unsnapped = []
    n_joined = 0
    for l in ped_lines:
        for k in (0, -1):
            p = Point(l["coords"][k])
            target, car_idx = snap_point_to_car(p)
            if target is None:
                near = car_tree.query(p.buffer(UNSNAPPED_REPORT_TOL))
                d = min((car_geoms[int(j)].distance(p) for j in near), default=None)
                if d is not None:
                    unsnapped.append({"at": [p.x, p.y], "dist": round(d, 2)})
                continue
            d = math.dist(l["coords"][k], target)
            if d > 1e-6:
                l["coords"][k] = target
                n_joined += 1
                if d > SNAP_REVIEW_TOL:
                    report["snap_moves"].append({"network": "pedestrian", "phase": "B-car-join",
                                                  "at": list(target), "dist": round(d, 2)})
            if car_idx is not None:
                inserts_into_car.append((car_idx, target))

    # at-grade pedestrian x car crossings mid-line: split pedestrian, splice vertex into car
    ped_geoms = [LineString(l["coords"]) for l in ped_lines]
    ped_tree = STRtree(ped_geoms)
    ped_split_params = [set() for _ in ped_lines]
    n_midline_crossings = 0
    for ci, cg in enumerate(car_geoms):
        bridge_c = bool(car_lines[ci]["props"].get("bridge") or car_lines[ci]["props"].get("unlink"))
        for pj in ped_tree.query(cg):
            pj = int(pj)
            bridge_p = bool(ped_lines[pj]["props"].get("bridge") or ped_lines[pj]["props"].get("unlink"))
            if bridge_c or bridge_p:
                continue
            pg = ped_geoms[pj]
            x = cg.intersection(pg)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            for pt in pts:
                on_ped_vertex = any(pt.distance(Point(v)) < VERTEX_EPS for v in pg.coords)
                inserts_into_car.append((ci, (pt.x, pt.y)))
                if on_ped_vertex:
                    continue
                ped_split_params[pj].add(round(pg.project(pt), 6))
                n_midline_crossings += 1

    new_ped = []
    for i, l in enumerate(ped_lines):
        length = ped_geoms[i].length
        params = sorted(p for p in ped_split_params[i] if VERTEX_EPS < p < length - VERTEX_EPS)
        if not params:
            new_ped.append(l)
            continue
        bounds = [0.0] + params + [length]
        for a0, b0 in zip(bounds[:-1], bounds[1:]):
            if b0 - a0 < 0.02:
                continue
            piece = substring(ped_geoms[i], a0, b0)
            new_ped.append({"props": dict(l["props"]), "coords": list(piece.coords)})
    ped_lines = new_ped

    # splice the same vertices into car — insert only, never move an existing car vertex
    by_line = defaultdict(list)
    for ci, xy in inserts_into_car:
        by_line[ci].append(xy)
    n_car_vertices_spliced = 0
    car_split_params = [set() for _ in car_lines]
    for ci, pts_ in by_line.items():
        c = car_lines[ci]["coords"]
        base_line = LineString(c)
        for xy in pts_:
            if any(math.dist(xy, v) < VERTEX_EPS for v in c):
                continue
            s = base_line.project(Point(xy))
            if SPLIT_CAR_AT_JUNCTIONS:
                car_split_params[ci].add(round(s, 6))
            acc = 0.0
            for idx2 in range(len(c) - 1):
                seg = math.dist(c[idx2], c[idx2 + 1])
                if acc + seg >= s - 1e-9:
                    c.insert(idx2 + 1, tuple(xy))
                    break
                acc += seg
            n_car_vertices_spliced += 1

    if SPLIT_CAR_AT_JUNCTIONS:
        new_car = []
        for i, l in enumerate(car_lines):
            geom = LineString(l["coords"])
            length = geom.length
            params = sorted(p for p in car_split_params[i] if VERTEX_EPS < p < length - VERTEX_EPS)
            if not params:
                new_car.append(l)
                continue
            bounds = [0.0] + params + [length]
            for a0, b0 in zip(bounds[:-1], bounds[1:]):
                if b0 - a0 < 0.02:
                    continue
                piece = substring(geom, a0, b0)
                new_car.append({"props": dict(l["props"]), "coords": list(piece.coords)})
        car_lines = new_car

    report["car_vertices_spliced"] = n_car_vertices_spliced
    report["pedestrian_free_ends_joined_to_car"] = n_joined
    report["pedestrian_car_midline_crossings"] = n_midline_crossings
    report["unsnapped_ends"] = unsnapped
    print(f"  Part B: {n_joined} pedestrian free ends joined onto car, {n_midline_crossings} mid-line "
          f"pedestrian x car crossings noded, {n_car_vertices_spliced} vertices spliced into car "
          f"(SPLIT_CAR_AT_JUNCTIONS={SPLIT_CAR_AT_JUNCTIONS}), {len(unsnapped)} free ends left within "
          f"{UNSNAPPED_REPORT_TOL} m of a foreign line but NOT joined")
    return car_lines, ped_lines


# --------------------------------------------------------------------------- Part C — clip to Base border
def load_border_polygon_utm():
    b = json.loads(BORDER_JSON.read_text(encoding="utf-8"))
    a, b2 = b["pointA"], b["pointB"]
    west, east = sorted([a["lng"], b2["lng"]])
    south, north = sorted([a["lat"], b2["lat"]])
    corners_wgs84 = [(west, south), (east, south), (east, north), (west, north)]
    corners_utm = [TO_UTM(x, y) for x, y in corners_wgs84]
    return Polygon(corners_utm)


def clip_lines(lines, poly, network, report):
    out = []
    dropped_fragments = 0
    for l in lines:
        line = LineString(l["coords"])
        if not line.intersects(poly):
            continue
        clipped = line.intersection(poly)
        if clipped.is_empty:
            continue
        if clipped.geom_type == "LineString":
            pieces = [clipped]
        elif clipped.geom_type == "MultiLineString":
            pieces = list(clipped.geoms)
        elif clipped.geom_type == "GeometryCollection":
            pieces = [g for g in clipped.geoms if g.geom_type == "LineString"]
        else:
            pieces = []
        for pc in pieces:
            if pc.length < MIN_CLIP_PIECE_LEN:
                dropped_fragments += 1
                continue
            props = dict(l["props"])
            on_edge = any(Point(pc.coords[k]).distance(poly.exterior) < 1e-3 for k in (0, -1))
            props["edge"] = bool(on_edge)
            out.append({"props": props, "coords": list(pc.coords)})
    report[f"{network}_clip_fragments_dropped_lt_1m"] = dropped_fragments
    print(f"  [{network}] clip to Base border: {len(lines)} -> {len(out)} pieces "
          f"({dropped_fragments} fragments < {MIN_CLIP_PIECE_LEN} m dropped)")
    return out


# --------------------------------------------------------------------------- ids, lengths, output
def assign_ids(lines, network):
    seen = set()
    for l in lines:
        coords = l["coords"]
        length = sum(math.dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        existing = l["props"].get("ssId")
        sid = str(existing) if existing else None
        if not sid or sid in seen:
            a, b = coords[0], coords[-1]
            ends = sorted([(round(a[0], 1), round(a[1], 1)), (round(b[0], 1), round(b[1], 1))])
            h = hashlib.blake2b(repr((ends, round(length, 1))).encode(), digest_size=5).hexdigest()
            base = f"{network[:3]}_{h}"
            sid, n = base, 1
            while sid in seen:
                n += 1
                sid = f"{base}_{n}"
        seen.add(sid)
        l["props"]["ssId"] = sid
        l["props"]["network"] = network
        l["props"]["length"] = round(length, 2)
    return lines


def to_geojson(lines, name):
    feats = []
    for l in lines:
        coords84 = [list(FROM_UTM(x, y)) for x, y in l["coords"]]
        feats.append({"type": "Feature", "properties": l["props"],
                      "geometry": {"type": "LineString", "coordinates": coords84}})
    return {"type": "FeatureCollection", "name": name,
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": feats}


def write_geojson(fc, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------- Part E — verification
def sha256_of(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def archive_raw_inputs(car_path, ped_path):
    RAW_OUT.mkdir(parents=True, exist_ok=True)
    checksums = {}
    src_dir = car_path.parent
    for f in src_dir.iterdir():
        if not f.is_file():
            continue
        before = sha256_of(f)
        dest = RAW_OUT / f.name
        if not dest.exists() or sha256_of(dest) != before:
            dest.write_bytes(f.read_bytes())
        checksums[f.name] = before
    (RAW_OUT / "checksums.json").write_text(json.dumps(checksums, indent=2, ensure_ascii=False), encoding="utf-8")
    return checksums


def verify_checksums_unchanged(car_path, ped_path, saved_checksums):
    bad = [f.name for f in (car_path, ped_path) if sha256_of(f) != saved_checksums.get(f.name)]
    return bad


def check_topology(lines, network):
    geoms = [LineString(l["coords"]) for l in lines]
    tree = STRtree(geoms)
    bad = 0
    for i, a in enumerate(geoms):
        for j in tree.query(a):
            j = int(j)
            if j <= i:
                continue
            b = geoms[j]
            bridge_i = bool(lines[i]["props"].get("bridge") or lines[i]["props"].get("unlink"))
            bridge_j = bool(lines[j]["props"].get("bridge") or lines[j]["props"].get("unlink"))
            if bridge_i or bridge_j:
                continue
            x = a.intersection(b)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            if not pts and x.geom_type not in ("Point", "MultiPoint"):
                bad += 1
                continue
            for pt in pts:
                on_a = any(pt.distance(Point(v)) < VERTEX_EPS for v in a.coords)
                on_b = any(pt.distance(Point(v)) < VERTEX_EPS for v in b.coords)
                if not (on_a and on_b):
                    bad += 1
    dup = sum(1 for i, a in enumerate(geoms) for j in tree.query(a)
              if int(j) > i and geoms[int(j)].hausdorff_distance(a) < 0.05)
    short = sum(1 for g in geoms if g.length < MIN_CLIP_PIECE_LEN)
    return bad, dup, short


def components_report(car_lines, ped_lines):
    import networkx as nx

    def key(xy):
        return (round(xy[0], 2), round(xy[1], 2))

    def graph_of(lines_list):
        G = nx.Graph()
        for l in lines_list:
            u, v = key(l["coords"][0]), key(l["coords"][-1])
            length = l["props"].get("length", 0)
            if G.has_edge(u, v):
                G[u][v]["length"] += length
            else:
                G.add_edge(u, v, length=length)
        return G

    car_g = graph_of(car_lines)
    ped_g = graph_of(ped_lines)
    union_g = nx.Graph()
    union_g.add_edges_from(car_g.edges(data=True))
    union_g.add_edges_from(ped_g.edges(data=True))

    def comp_info(G):
        comps = list(nx.connected_components(G))
        small = []
        for comp in comps:
            sub = G.subgraph(comp)
            total_len = sum(d.get("length", 0) for _, _, d in sub.edges(data=True))
            if len(comps) > 1 and total_len < 50:
                rep = next(iter(comp))
                small.append({"nodes": len(comp), "length_m": round(total_len, 1), "at": list(rep)})
        return len(comps), small

    n_car, small_car = comp_info(car_g)
    n_ped, small_ped = comp_info(ped_g)
    n_union, small_union = comp_info(union_g)
    return {
        "car_components": n_car, "car_small_islands": small_car,
        "pedestrian_components": n_ped, "pedestrian_small_islands": small_ped,
        "union_components": n_union, "union_small_islands": small_union,
    }, union_g


def union_check_geojson(union_g, car_lines, ped_lines):
    feats = []
    for l in car_lines + ped_lines:
        coords84 = [list(FROM_UTM(x, y)) for x, y in l["coords"]]
        feats.append({"type": "Feature", "properties": {"network": l["props"].get("network")},
                      "geometry": {"type": "LineString", "coordinates": coords84}})
    return {"type": "FeatureCollection", "name": "weimar_ss_union_check",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": feats}


def check_car_join(car_lines, ped_lines):
    car_geoms = [LineString(l["coords"]) for l in car_lines]
    car_tree = STRtree(car_geoms)
    shared_vertices = 0
    max_gap = 0.0
    not_on_car = 0
    ped_geoms = [LineString(l["coords"]) for l in ped_lines]

    def deg_of_ends(geoms):
        ends = [g.coords[0] for g in geoms] + [g.coords[-1] for g in geoms]
        c = defaultdict(int)
        for xy in ends:
            c[(round(xy[0], 2), round(xy[1], 2))] += 1
        return c

    deg = deg_of_ends(ped_geoms)
    for g in ped_geoms:
        for k in (0, -1):
            xy = g.coords[k]
            if deg[(round(xy[0], 2), round(xy[1], 2))] != 1:
                continue  # only free ends, like TASK_34's check
            p = Point(xy)
            near = car_tree.query(p.buffer(CAR_NODE_TOL + 0.01))
            if len(near) == 0:
                continue
            d = min(car_geoms[int(j)].distance(p) for j in near)
            if d <= VERTEX_EPS:
                shared_vertices += 1
                max_gap = max(max_gap, d)
            elif d <= JUNCTION_TOL:
                not_on_car += 1
    return shared_vertices, round(max_gap, 4), not_on_car


def check_inside_border(lines, poly, network):
    bad = 0
    for l in lines:
        for v in l["coords"]:
            if poly.exterior.distance(Point(v)) > 1e-6 and not poly.contains(Point(v)) and not poly.touches(Point(v)):
                if not poly.buffer(1e-6).contains(Point(v)):
                    bad += 1
    return bad


# --------------------------------------------------------------------------- main
def main():
    print(f"Parameters: SNAP_TOL={SNAP_TOL} m, JUNCTION_TOL={JUNCTION_TOL} m, CAR_NODE_TOL={CAR_NODE_TOL} m, "
          f"SPLIT_CAR_AT_JUNCTIONS={SPLIT_CAR_AT_JUNCTIONS}\n")

    src_dir, car_path, ped_path = find_input_dir()
    print(f"Input folder: {src_dir}\n  car:        {car_path.name}\n  pedestrian: {ped_path.name}\n")

    saved_checksums = archive_raw_inputs(car_path, ped_path)
    print(f"Archived unchanged copies + checksums -> {RAW_OUT}\n")

    report = {"snap_moves": []}

    car_feats, car_utm = load_features(car_path)
    ped_feats, ped_utm = load_features(ped_path)
    car_lines = to_utm(explode(car_feats, "car"), car_utm)
    ped_lines = to_utm(explode(ped_feats, "pedestrian"), ped_utm)
    n_car_in, n_ped_in = len(car_lines), len(ped_lines)
    len_car_in = sum(LineString(l["coords"]).length for l in car_lines)
    len_ped_in = sum(LineString(l["coords"]).length for l in ped_lines)

    print("Part A — technical cleanup + topology within each layer")
    car_lines = clean_technical(car_lines, "car", report)
    ped_lines = clean_technical(ped_lines, "pedestrian", report)

    unlink_pts = load_unlink_points()
    print(f"  loaded {len(unlink_pts)} unlink point(s) from existing *_unlinks.geojson files")

    car_lines = snap_ends_within_layer(car_lines, SNAP_TOL, "car", report)
    ped_lines = snap_ends_within_layer(ped_lines, SNAP_TOL, "pedestrian", report)
    car_lines = node_at_intersections(car_lines, "car", unlink_pts, report)
    ped_lines = node_at_intersections(ped_lines, "pedestrian", unlink_pts, report)

    print("\nPart B — shared car/pedestrian vertices")
    car_lines, ped_lines = join_networks(car_lines, ped_lines, report)

    print("\nPart C — clip to Base border")
    UNCLIPPED_OUT.mkdir(parents=True, exist_ok=True)
    car_unclipped = assign_ids([dict(l) for l in car_lines], "car")
    ped_unclipped = assign_ids([dict(l) for l in ped_lines], "pedestrian")
    write_geojson(to_geojson(car_unclipped, "weimar_ss_car_final_unclipped"),
                  UNCLIPPED_OUT / "weimar_ss_car_final_unclipped.geojson")
    write_geojson(to_geojson(ped_unclipped, "weimar_ss_pedestrian_final_unclipped"),
                  UNCLIPPED_OUT / "weimar_ss_pedestrian_final_unclipped.geojson")
    print(f"  unclipped versions saved to {UNCLIPPED_OUT}")

    poly = load_border_polygon_utm()
    car_lines = clip_lines(car_lines, poly, "car", report)
    ped_lines = clip_lines(ped_lines, poly, "pedestrian", report)

    car_lines = assign_ids(car_lines, "car")
    ped_lines = assign_ids(ped_lines, "pedestrian")

    car_out = to_geojson(car_lines, "weimar_ss_car_final")
    ped_out = to_geojson(ped_lines, "weimar_ss_pedestrian_final")
    write_geojson(car_out, DATA / "weimar_ss_car_final.geojson")
    write_geojson(ped_out, DATA / "weimar_ss_pedestrian_final.geojson")
    print(f"\nwrote {DATA / 'weimar_ss_car_final.geojson'} ({len(car_lines)} features)")
    print(f"wrote {DATA / 'weimar_ss_pedestrian_final.geojson'} ({len(ped_lines)} features)")

    print("\nPart E — verification")
    bad_car, dup_car, short_car = check_topology(car_lines, "car")
    bad_ped, dup_ped, short_ped = check_topology(ped_lines, "pedestrian")
    print(f"  car: {bad_car} crossings without a shared node, {dup_car} near-duplicate pairs, {short_car} segments < {MIN_CLIP_PIECE_LEN} m")
    print(f"  pedestrian: {bad_ped} crossings without a shared node, {dup_ped} near-duplicate pairs, {short_ped} segments < {MIN_CLIP_PIECE_LEN} m")

    shared_vertices, max_gap, not_on_car = check_car_join(car_lines, ped_lines)
    print(f"  car/pedestrian joins: {shared_vertices} shared vertices (max coordinate gap {max_gap} m), "
          f"{not_on_car} pedestrian free ends near car but NOT exactly on it")

    bad_inside_car = check_inside_border(car_lines, poly, "car")
    bad_inside_ped = check_inside_border(ped_lines, poly, "pedestrian")
    print(f"  inside Base border (tol 1e-6 m): car {bad_inside_car} violation(s), pedestrian {bad_inside_ped} violation(s)")

    comps, union_g = components_report(car_lines, ped_lines)
    print(f"  connected components: car={comps['car_components']}, pedestrian={comps['pedestrian_components']}, "
          f"union={comps['union_components']}")
    if comps["union_small_islands"]:
        print(f"  small isolated union components (<50 m, possibly missing links):")
        for s in comps["union_small_islands"]:
            print(f"    {s}")

    checksum_bad = verify_checksums_unchanged(car_path, ped_path, saved_checksums)
    print(f"  source files unchanged: {'OK' if not checksum_bad else 'CHANGED: ' + str(checksum_bad)}")

    # review files
    snap_review = {"type": "FeatureCollection", "name": "ss_snap_review",
                    "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                    "features": [{"type": "Feature", "properties": {k: v for k, v in m.items() if k != "at"},
                                   "geometry": {"type": "Point", "coordinates": list(FROM_UTM(*m["at"]))}}
                                  for m in report["snap_moves"]]}
    write_geojson(snap_review, REVIEW_DIR / "ss_snap_review.geojson")

    unsnapped_review = {"type": "FeatureCollection", "name": "ss_unsnapped_ends",
                         "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                         "features": [{"type": "Feature", "properties": {"dist": u["dist"]},
                                        "geometry": {"type": "Point", "coordinates": list(FROM_UTM(*u["at"]))}}
                                       for u in report["unsnapped_ends"]]}
    write_geojson(unsnapped_review, REVIEW_DIR / "ss_unsnapped_ends.geojson")

    union_check = union_check_geojson(union_g, car_lines, ped_lines)
    write_geojson(union_check, DATA / "weimar_ss_union_check.geojson")

    print(f"\nreview files: {REVIEW_DIR / 'ss_snap_review.geojson'} ({len(report['snap_moves'])} moves > {SNAP_REVIEW_TOL} m), "
          f"{REVIEW_DIR / 'ss_unsnapped_ends.geojson'} ({len(report['unsnapped_ends'])} ends), "
          f"{DATA / 'weimar_ss_union_check.geojson'} (QA only, not wired into layers.json)")

    wire_layers_json()
    print("\nlayers.json updated: weimar_ss_car / weimar_ss_pedestrian now point at the _final files; "
          "'Removed (review)' rows removed from the Space Syntax sidebar group (nothing meaningful is "
          "removed by this pipeline); preset files stay on disk, untouched.")

    summary = {
        "input": {"car": {"file": car_path.name, "features_in": n_car_in, "length_m_in": round(len_car_in, 1)},
                  "pedestrian": {"file": ped_path.name, "features_in": n_ped_in, "length_m_in": round(len_ped_in, 1)}},
        "output": {"car": {"features_out": len(car_lines), "length_m_out": round(sum(LineString(l["coords"]).length for l in car_lines), 1)},
                  "pedestrian": {"features_out": len(ped_lines), "length_m_out": round(sum(LineString(l["coords"]).length for l in ped_lines), 1)}},
        "topology": {"car": {"bad_crossings": bad_car, "near_dup": dup_car, "short": short_car},
                     "pedestrian": {"bad_crossings": bad_ped, "near_dup": dup_ped, "short": short_ped}},
        "car_pedestrian_join": {"shared_vertices": shared_vertices, "max_gap_m": max_gap, "not_exactly_on_car": not_on_car},
        "inside_border_violations": {"car": bad_inside_car, "pedestrian": bad_inside_ped},
        "components": comps,
        "source_checksums_unchanged": not checksum_bad,
        "snap_moves_gt_3m": len(report["snap_moves"]),
        "unsnapped_ends": len(report["unsnapped_ends"]),
        "params": {"SNAP_TOL": SNAP_TOL, "JUNCTION_TOL": JUNCTION_TOL, "CAR_NODE_TOL": CAR_NODE_TOL,
                   "SPLIT_CAR_AT_JUNCTIONS": SPLIT_CAR_AT_JUNCTIONS},
        "detail": {k: v for k, v in report.items() if k not in ("snap_moves", "unsnapped_ends")},
    }
    (ROOT / "output" / "task35_report.json").parent.mkdir(exist_ok=True)
    (ROOT / "output" / "task35_report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nfull report -> {ROOT / 'output' / 'task35_report.json'}")


def wire_layers_json():
    j = json.loads(LAYERS_JSON.read_text(encoding="utf-8"))
    for l in j["layers"]:
        if l["id"] == "weimar_ss_car":
            l["url"] = "layers/data/weimar_ss_car_final.geojson"
            l["description"] = ("Space Syntax car network, drawn by hand in QGIS (TASK_35) — her drawing is "
                                 "the source of truth, nothing simplified/straightened/thinned. Topology-noded "
                                 "(ends snapped, real crossings split into graph nodes), joined to pedestrian "
                                 "at identical shared vertices, clipped to Base border (city centre only). "
                                 "Presets from TASK_31-34 stay on disk, unused. Off by default.")
        elif l["id"] == "weimar_ss_pedestrian":
            l["url"] = "layers/data/weimar_ss_pedestrian_final.geojson"
            l["description"] = ("Space Syntax pedestrian network, drawn by hand in QGIS (TASK_35) — her drawing "
                                 "is the source of truth, nothing simplified/straightened/thinned. Topology-noded, "
                                 "every end near car snapped exactly onto it (car gets the same vertex, never "
                                 "split unless SPLIT_CAR_AT_JUNCTIONS), clipped to Base border (city centre only). "
                                 "Presets from TASK_31-34 stay on disk, unused. Off by default.")
    sb = j.get("sidebar", {}).get("computational", [])
    for g in sb:
        if g.get("name") == "Space Syntax":
            g["items"] = [it for it in g["items"] if not it["name"].lower().startswith("removed")]
    LAYERS_JSON.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
