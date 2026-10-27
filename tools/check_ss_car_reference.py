#!/usr/bin/env python3
"""
check_ss_car_reference.py — TASK_34 part 3: does pedestrian actually match the fixed car reference?

Independent of the builder (reads only the written GeoJSONs). For a given preset (default: all three):

  1. pedestrian ends lying exactly (<= 0.01 m) on a car line — count, and as a share of all pedestrian ends
  2. free pedestrian ends within CAR_SNAP_TOL (8 m) of car that are NOT snapped onto it — should be 0;
     if any, their EPSG:25832 coordinates are printed (these are the ones to check by hand)
  3. union (car UNCHANGED as read from its file, ∪ pedestrian): crossings without a shared node (excluding
     bridge/tunnel unlink pairs), and the number of connected components of the union graph
  4. also runs the TASK_31/32 topology checks (check_space_syntax_roads.check) on the pedestrian file alone,
     for continuity with the existing report

Writes `weimar_ss_union_check.geojson` (car + pedestrian together, `network` property tells them apart) into
web/layers/data/ **for visual QA only** — layers.json does not reference it, so it never shows up in the
platform sidebar. Also writes data/processed/space_syntax/check34_report.json.

Run:  python tools/check_ss_car_reference.py [light|medium|strong ...]
"""
import json, sys
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape, mapping
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_space_syntax_roads import check as topology_check, work_boundary  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
PROC = ROOT / "data" / "processed" / "space_syntax"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
FROM = Transformer.from_crs(25832, 4326, always_xy=True).transform
CAR_SNAP_TOL = 8.0
MIN_SEG = {"light": 8, "medium": 15, "strong": 25}


def load(p):
    return [(transform(TO, shape(f["geometry"])), f["properties"])
            for f in json.load(open(p, encoding="utf-8"))["features"]]


def check_preset(preset, car):
    ped_path = DATA / f"weimar_ss_pedestrian_{preset}.geojson"
    if not ped_path.exists():
        return None
    ped = load(ped_path)
    ped_geoms = [g for g, _ in ped]

    # 1/2: pedestrian ends vs. car. Metric 1 ("ends lying on car") counts every line-endpoint occurrence — a
    # junction where several pedestrian edges meet is relevant too. Metric 2 ("free ends near car, not
    # snapped") is explicitly about FREE ends (part 3 of the task: "свободных концов") — a junction point where
    # 2+ pedestrian edges meet is not a free end even if it sits a few metres from car, so it is excluded there
    # via the graph degree of each endpoint coordinate.
    car_geoms = [g for g, _ in car]
    car_bridge = [p.get("bridge", False) for _, p in car]
    car_tree = STRtree(car_geoms)
    from collections import Counter
    deg = Counter()
    for g in ped_geoms:
        deg[(round(g.coords[0][0], 3), round(g.coords[0][1], 3))] += 1
        deg[(round(g.coords[-1][0], 3), round(g.coords[-1][1], 3))] += 1
    ends = [(g.coords[0], "start", i) for i, g in enumerate(ped_geoms)] + \
           [(g.coords[-1], "end", i) for i, g in enumerate(ped_geoms)]
    exact = 0
    unsnapped = []
    for xy, side, i in ends:
        p = Point(xy)
        near = car_tree.query(p.buffer(CAR_SNAP_TOL))
        if len(near) == 0:
            continue
        d = min(car_geoms[j].distance(p) for j in near)
        if d <= 0.01:
            exact += 1
        elif d <= CAR_SNAP_TOL and deg[(round(xy[0], 3), round(xy[1], 3))] == 1:
            unsnapped.append(dict(at=[round(p.x, 2), round(p.y, 2)], dist=round(d, 3), side=side, ssId=ped[i][1].get("ssId")))
    total_ends = len(ends)
    free_ends = sum(1 for v in deg.values() if v == 1)

    # 3: union crossings + components (car unchanged; a touch counts as "noded" if EITHER line has a vertex there)
    all_geoms = car_geoms + ped_geoms
    all_bridge = car_bridge + [p.get("bridge", False) for _, p in ped]
    tree = STRtree(all_geoms)
    bad, unlinks = [], 0
    for i, a in enumerate(all_geoms):
        for j in tree.query(a):
            if j <= i:
                continue
            b = all_geoms[j]
            x = a.intersection(b)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else None)
            if pts is None:
                bad.append(dict(i=i, j=j, kind="overlap")); continue
            # "noded" = at least one of the two lines has an actual VERTEX there (any point in its coordinate
            # list, not just an endpoint) — car itself never gets a vertex added by design (TASK_34), so a
            # pedestrian-vs-car crossing is only "noded" via a pedestrian vertex, which node_at_car_crossings /
            # insert_final_car_crossings are meant to guarantee.
            va = list(a.coords); vb = list(b.coords)
            for q in pts:
                noded = any(q.distance(Point(c)) < 0.02 for c in va) or \
                        any(q.distance(Point(c)) < 0.02 for c in vb)
                if noded:
                    continue
                if all_bridge[i] or all_bridge[j]:
                    unlinks += 1
                else:
                    bad.append(dict(i=i, j=j, at=[round(q.x, 1), round(q.y, 1)]))
    # Connectivity, not just exact-vertex matching: by design car NEVER gets a new vertex (the task explicitly
    # forbids it), so a large share of real pedestrian-to-car touches are a pedestrian vertex sitting on the
    # INTERIOR of a car edge (between two of car's own vertices) — that coordinate exists nowhere in car's own
    # vertex list, so exact-key or vertex-to-vertex proximity matching (or even a WGS84 round-trip, which shifts
    # coordinates by sub-mm float noise) cannot detect it: it has to be point-to-LINE proximity, and connecting
    # such a point transitively joins it to that whole car line's two endpoints (which are already one another's
    # neighbours via that line's own edges in G).
    k = lambda xy: (round(xy[0], 3), round(xy[1], 3))
    G = nx.Graph()
    for g in all_geoms:
        c = list(g.coords)
        for i in range(len(c) - 1):
            G.add_edge(k(c[i]), k(c[i + 1]))
    tol = 0.05   # generous enough for WGS84 round-trip float noise, tight enough not to falsely bridge real gaps
    bridged = 0
    for g in ped_geoms:
        for xy in g.coords:
            p = Point(xy)
            near = car_tree.query(p.buffer(tol))
            for j in near:
                cg = car_geoms[j]
                if cg.distance(p) <= tol:
                    G.add_edge(k(xy), k(cg.coords[0]))   # cg.coords[0] is already connected to cg.coords[-1] via G
                    bridged += 1
                    break
    union_components = nx.number_connected_components(G)

    # 4: TASK_31/32 topology check on pedestrian alone
    topo = topology_check(ped_path, MIN_SEG.get(preset, 15), work_boundary())

    result = dict(
        pedestrian_segments=len(ped), car_segments=len(car),
        ped_ends_total=total_ends, ped_ends_exactly_on_car=exact,
        ped_ends_exact_share=round(exact / total_ends, 4) if total_ends else None,
        ped_free_ends_total=free_ends,
        ped_free_ends_near_car_not_snapped=len(unsnapped), unsnapped_examples=unsnapped[:20],
        union_crossings_without_node=len(bad), union_crossings_examples=bad[:10],
        union_declared_unlinks=unlinks, union_components=union_components,
        pedestrian_topology=topo,
    )
    return result


def write_union_check(preset, car):
    ped_path = DATA / f"weimar_ss_pedestrian_{preset}.geojson"
    ped = json.load(open(ped_path, encoding="utf-8"))["features"]
    car_fc = json.load(open(DATA / "weimar_ss_car_strong.geojson", encoding="utf-8"))["features"]
    feats = []
    for f in car_fc:
        p = dict(f["properties"]); p["network"] = "car"
        feats.append({"type": "Feature", "properties": p, "geometry": f["geometry"]})
    for f in ped:
        p = dict(f["properties"]); p["network"] = "pedestrian"
        feats.append({"type": "Feature", "properties": p, "geometry": f["geometry"]})
    out = DATA / "weimar_ss_union_check.geojson"
    json.dump({"type": "FeatureCollection", "name": "weimar_ss_union_check", "features": feats},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return out


def main(*presets):
    presets = list(presets) or ["light", "medium", "strong"]
    car = load(DATA / "weimar_ss_car_strong.geojson")
    rep = {}
    for preset in presets:
        r = check_preset(preset, car)
        if r is None:
            continue
        rep[preset] = r
        short = {k: v for k, v in r.items() if k not in ("unsnapped_examples", "union_crossings_examples", "pedestrian_topology")}
        print(preset, json.dumps(short, ensure_ascii=False))
        if r["ped_free_ends_near_car_not_snapped"]:
            print("  UNSNAPPED:", r["unsnapped_examples"])
        if r["union_crossings_without_node"]:
            print("  CROSSINGS:", r["union_crossings_examples"])
    if "medium" in rep:
        p = write_union_check("medium", car)
        print("wrote", p, "(QA only, not in layers.json)")
    json.dump(rep, open(PROC / "check34_report.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
    print("->", PROC / "check34_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(*sys.argv[1:])
