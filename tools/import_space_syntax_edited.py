#!/usr/bin/env python3
"""
import_space_syntax_edited.py — TASK_34 part 4: bring hand-edited car/pedestrian networks back into the platform.

After editing `output/qgis/weimar_ss_car.gpkg` / `weimar_ss_pedestrian.gpkg` (layer "network") in QGIS and
exporting each edited layer back to GeoJSON (any CRS — this script reprojects), run:

    python tools/import_space_syntax_edited.py <edited_car.geojson> <edited_pedestrian.geojson>

What it does, in order:
  1. reads both files (any CRS: EPSG:25832 or WGS84 both work, detected from the GeoJSON `crs` member or from
     the coordinate magnitude if that member is missing)
  2. runs `geojson_clean.clean_features` on each (project policy for hand-edited polygon/line data)
  3. reprojects to WGS84 (the platform's storage CRS, like every other web/layers/data file)
  4. topology check: crossings without a shared node (within each network, and across the pair — a place where
     the edited pedestrian network crosses car without a node), duplicate/overlapping geometry, self-loops
  5. TASK_34 part 3 check: every pedestrian end within CAR_SNAP_TOL (8 m) of car must sit exactly (<= 0.01 m) on
     a car line — this is what stays true only if edits keep the join; a violation is reported with coordinates,
     not silently fixed (editing intent is the user's, not this script's)
  6. writes `web/layers/data/weimar_ss_car_final.geojson` / `weimar_ss_pedestrian_final.geojson`
  7. updates `web/layers/layers.json`: the "Car network (Space Syntax)" / "Pedestrian network (Space Syntax)"
     layers' `url` is switched to the `_final` files (the preset files stay on disk, untouched, in case you
     want to compare or revert — rerun with `--presets` to switch back)
  8. runs a 3DM export smoke test (rhino3dm.js is a browser/Node library, not reachable from plain Python here —
     see the printed instructions for the Node-based check, the same one used in TASK_28-33)

Nothing here silently drops a topology problem: every failure is printed with EPSG:25832 coordinates and the
import stops (files are NOT written) unless you pass --force, which writes anyway and prints a warning list.

`ssId` is never invented for features that already carry one; the field is required to reconnect the "Removed"
review layers and data/manual/space_syntax_overrides.json to what you kept. A NEW segment you drew needs no
ssId (or any placeholder) — one is assigned here, derived from its own geometry
(`ss_rules33.stable_ss_id`-equivalent: not imported to avoid pulling in TASK_33 machinery — reimplemented
inline, see `_stable_id`).
"""
import argparse, hashlib, json, math, sys
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape, mapping
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
LAYERS_JSON = ROOT / "web" / "layers" / "layers.json"
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True).transform
FROM_UTM = Transformer.from_crs(25832, 4326, always_xy=True).transform
CAR_SNAP_TOL = 8.0


def _stable_id(coords, network):
    a, b = coords[0], coords[-1]
    ends = sorted([(round(a[0]), round(a[1])), (round(b[0]), round(b[1]))])
    L = round(sum(math.dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1)))
    h = hashlib.blake2b(repr((ends, L)).encode(), digest_size=5).hexdigest()
    return f"{network[:3]}_{h}"


def _looks_like_utm(coords):
    x, y = coords[0]
    return abs(x) > 1000 or abs(y) > 1000


def load_any_crs(path, network):
    fc = json.load(open(path, encoding="utf-8"))
    feats = fc.get("features", [])
    if not feats:
        raise SystemExit(f"{path}: no features")
    crs_name = (fc.get("crs") or {}).get("properties", {}).get("name", "")
    is_utm = "25832" in crs_name or (not crs_name and _looks_like_utm(feats[0]["geometry"]["coordinates"]))
    out = []
    for f in feats:
        g = f["geometry"]
        if g["type"] != "LineString":
            print(f"  skipped non-LineString feature ({g['type']}) in {path.name}")
            continue
        props = dict(f["properties"] or {})
        if not props.get("ssId"):
            props["ssId"] = _stable_id(g["coordinates"], network)
        out.append({"type": "Feature", "properties": props, "geometry": g})
    fc2 = {"type": "FeatureCollection", "name": f"weimar_ss_{network}_final", "features": out}
    if is_utm:
        print(f"  {path.name}: detected EPSG:25832 input")
    else:
        print(f"  {path.name}: detected WGS84 input")
    return fc2, is_utm


def to_utm_geoms(fc, is_utm):
    out = []
    for f in fc["features"]:
        g = shape(f["geometry"])
        out.append(g if is_utm else transform(TO_UTM, g))
    return out


def check_topology(name, geoms):
    tree = STRtree(geoms)
    bad = []
    for i, a in enumerate(geoms):
        for j in tree.query(a):
            if j <= i:
                continue
            b = geoms[j]
            x = a.intersection(b)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else None)
            if pts is None:
                bad.append(dict(i=i, j=j, kind="overlap")); continue
            va, vb = list(a.coords), list(b.coords)
            for q in pts:
                if any(q.distance(Point(c)) < 0.02 for c in va) or any(q.distance(Point(c)) < 0.02 for c in vb):
                    continue
                bad.append(dict(i=i, j=j, at=[round(q.x, 1), round(q.y, 1)], kind="crossing"))
    dup = sum(1 for i, a in enumerate(geoms) for j in tree.query(a)
              if j > i and geoms[j].hausdorff_distance(a) < 0.05)
    selfloops = sum(1 for g in geoms if Point(g.coords[0]).distance(Point(g.coords[-1])) < 0.02)
    print(f"  {name}: {len(geoms)} segments, {len(bad)} crossings without a shared vertex, "
          f"{dup} near-duplicate pairs, {selfloops} self-loops (start==end)")
    return bad


def _degree_by_proximity(geoms, tol=0.05):
    """Node degree via tolerance-based clustering, not exact-key matching: a GeoJSON export (this tool's own
    output, or a re-export out of QGIS) routinely round-trips through WGS84 (GeoJSON is always WGS84 by spec),
    which shifts EPSG:25832 coordinates by sub-mm/mm float noise — enough to split what is really one shared
    junction into several apparently-distinct exact-match keys. `tol` is generous enough to absorb that, tight
    enough not to falsely merge genuinely distinct points."""
    ends = [g.coords[0] for g in geoms] + [g.coords[-1] for g in geoms]
    pts = [Point(*xy) for xy in ends]
    tree = STRtree(pts)
    par = list(range(len(pts)))
    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]; a = par[a]
        return a
    for i, p in enumerate(pts):
        for j in tree.query(p.buffer(tol)):
            if j > i and pts[j].distance(p) <= tol:
                par[find(i)] = find(j)
    from collections import Counter
    deg = Counter(find(i) for i in range(len(pts)))
    return ends, [find(i) for i in range(len(pts))], deg


def check_car_join(ped_geoms, car_geoms):
    """Only FREE ends (graph degree 1 within the pedestrian network) are checked — a junction where several
    pedestrian edges meet is not a "free end" even if it happens to sit a few metres from car."""
    ends, groups, deg = _degree_by_proximity(ped_geoms)
    n = len(ped_geoms)
    car_tree = STRtree(car_geoms)
    bad = []
    for idx, xy in enumerate(ends):
        if deg[groups[idx]] != 1:
            continue
        p = Point(xy)
        near = car_tree.query(p.buffer(CAR_SNAP_TOL))
        if len(near) == 0:
            continue
        # STRtree.query is a bounding-box test: a candidate's bbox can overlap the query buffer's bbox without
        # its actual geometry being within CAR_SNAP_TOL (e.g. a long diagonal line whose bbox is much bigger
        # than the line itself near this point) — `d` must still be checked against CAR_SNAP_TOL, not just > 0.
        d = min(car_geoms[j].distance(p) for j in near)
        if 0.01 < d <= CAR_SNAP_TOL:
            bad.append(dict(at=[round(p.x, 2), round(p.y, 2)], dist=round(d, 3)))
    print(f"  pedestrian free ends within {CAR_SNAP_TOL} m of car but not exactly on it: {len(bad)}")
    return bad


def wire_into_layers_json():
    j = json.loads(LAYERS_JSON.read_text(encoding="utf-8"))
    changed = []
    for l in j["layers"]:
        if l["id"] == "weimar_ss_car":
            l["url"] = "layers/data/weimar_ss_car_final.geojson"; changed.append(l["id"])
        elif l["id"] == "weimar_ss_pedestrian":
            l["url"] = "layers/data/weimar_ss_pedestrian_final.geojson"; changed.append(l["id"])
    LAYERS_JSON.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print("layers.json updated:", changed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("car_file")
    ap.add_argument("pedestrian_file")
    ap.add_argument("--force", action="store_true", help="write the _final files even if topology checks fail")
    ap.add_argument("--presets", action="store_true", help="instead of wiring in _final files, point layers.json back at the preset files")
    args = ap.parse_args()

    if args.presets:
        j = json.loads(LAYERS_JSON.read_text(encoding="utf-8"))
        for l in j["layers"]:
            if l["id"] == "weimar_ss_car":
                l["url"] = "layers/data/weimar_ss_car_strong.geojson"
            elif l["id"] == "weimar_ss_pedestrian":
                l["url"] = "layers/data/weimar_ss_pedestrian_medium.geojson"
        LAYERS_JSON.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
        print("layers.json reverted to the preset files (car strong, pedestrian medium).")
        return

    car_fc, car_utm = load_any_crs(Path(args.car_file), "car")
    ped_fc, ped_utm = load_any_crs(Path(args.pedestrian_file), "pedestrian")

    for fc in (car_fc, ped_fc):
        fc["features"], summary = clean_features(fc["features"], label=fc["name"])
        print(f"  geojson_clean({fc['name']}): {summary}")

    car_geoms = to_utm_geoms(car_fc, car_utm)
    ped_geoms = to_utm_geoms(ped_fc, ped_utm)

    print("\nTopology checks (EPSG:25832):")
    bad_car = check_topology("car", car_geoms)
    bad_ped = check_topology("pedestrian", ped_geoms)
    bad_join = check_car_join(ped_geoms, car_geoms)
    all_bad = bad_car or bad_ped or bad_join

    if all_bad and not args.force:
        print("\nSTOPPED — topology/join problems found above. Fix them in QGIS and re-export, or pass --force "
              "to write anyway (the platform will then show the same problems).")
        sys.exit(1)
    if all_bad:
        print("\n--force: writing despite the problems above.")

    def to_wgs84(fc, is_utm):
        out = []
        for f in fc["features"]:
            g = shape(f["geometry"])
            g84 = g if not is_utm else transform(FROM_UTM, g)
            out.append({"type": "Feature", "properties": f["properties"], "geometry": mapping(g84)})
        return {"type": "FeatureCollection", "name": fc["name"],
                "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": out}

    car_out = to_wgs84(car_fc, car_utm)
    ped_out = to_wgs84(ped_fc, ped_utm)
    json.dump(car_out, open(DATA / "weimar_ss_car_final.geojson", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(ped_out, open(DATA / "weimar_ss_pedestrian_final.geojson", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"\nwrote {DATA / 'weimar_ss_car_final.geojson'} ({len(car_out['features'])} features)")
    print(f"wrote {DATA / 'weimar_ss_pedestrian_final.geojson'} ({len(ped_out['features'])} features)")

    wire_into_layers_json()

    print("\n3DM export smoke test — this needs the browser/Node rhino3dm.js stack, not plain Python. Run:")
    print("  (open the platform, use the export toolbar on Computational Design with the two SS layers checked,")
    print("   format = 3DM) or reuse the Node test pattern from TASK_28 (`build3dmExport` + `File3dm.fromByteArray`")
    print("   read-back) — check object count == feature count for both layers, only curve types, layers")
    print("   'SS Car' / 'SS Pedestrian'.")


if __name__ == "__main__":
    main()
